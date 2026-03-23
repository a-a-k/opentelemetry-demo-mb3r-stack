#!/usr/bin/env python3

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LISTEN = os.environ.get("EXPORTER_LISTEN", "0.0.0.0:9113")
SHEAFT_BASE_URL = os.environ.get("SHEAFT_BASE_URL", "http://sheaft:8080").rstrip("/")
POLL_INTERVAL = float(os.environ.get("EXPORTER_POLL_INTERVAL", "5"))
OUTPUT_DIR = Path(os.environ.get("EXPORTER_OUTPUT_DIR", "/tmp"))
EXPORTER_ENDPOINT_CONFIG = os.environ.get("EXPORTER_ENDPOINT_CONFIG", "")
EXPORTER_POLICY_CONFIG = os.environ.get("EXPORTER_POLICY_CONFIG", "")
EXPORTER_PRIMARY_PROFILE = os.environ.get("EXPORTER_PRIMARY_PROFILE", "steady-state")
EXPORTER_SNAPSHOT_PATH = Path(os.environ.get("EXPORTER_SNAPSHOT_PATH", "/mb3r/out/artifacts/latest-snapshot.json"))
EXPORTER_SNAPSHOT_HISTORY_DIR = Path(
    os.environ.get("EXPORTER_SNAPSHOT_HISTORY_DIR", "/mb3r/out/artifacts/snapshots")
)
EXPORTER_DEPENDENCY_CONFIG = os.environ.get("EXPORTER_DEPENDENCY_CONFIG", "")


DECISION_CODES = {
    "pass": 0,
    "report": 0,
    "warn": 1,
    "review": 1,
    "fail": 2,
    "error": 3,
}

DEFAULT_EXPECTED_ENDPOINT_WEIGHTS = {
    "frontend:GET /api/products": 0.25,
    "frontend:GET /api/recommendations": 0.25,
    "frontend:GET /api/cart": 0.25,
    "frontend:POST /api/checkout": 0.25,
}

DEFAULT_POLICY = {
    "mode": "warn",
    "endpoint_threshold": 0.35,
    "aggregate_threshold": 0.60,
    "cross_profile_aggregate_threshold": 0.70,
    "profile_aggregate_thresholds": {
        "steady-state": 0.88,
        "single-service-fault": 0.45,
        "correlated-service-fault": 0.55,
    },
}


def now_epoch() -> float:
    return time.time()


def parse_timestamp(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def json_get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_weights(weights):
    normalized = {}
    total = 0.0
    for endpoint_id, raw_weight in (weights or {}).items():
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        normalized[str(endpoint_id)] = weight
        total += weight

    if not normalized or total <= 0:
        normalized = dict(DEFAULT_EXPECTED_ENDPOINT_WEIGHTS)
        total = sum(normalized.values())

    return {endpoint_id: weight / total for endpoint_id, weight in normalized.items()}


def load_expected_endpoint_weights():
    if EXPORTER_ENDPOINT_CONFIG:
        try:
            payload = json.loads(Path(EXPORTER_ENDPOINT_CONFIG).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                weights = payload.get("endpoint_weights") or payload.get("expected_endpoints") or payload
                return normalize_weights(weights)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    return normalize_weights(DEFAULT_EXPECTED_ENDPOINT_WEIGHTS)


def load_relevant_services():
    relevant = set()
    if EXPORTER_DEPENDENCY_CONFIG:
        try:
            payload = json.loads(Path(EXPORTER_DEPENDENCY_CONFIG).read_text(encoding="utf-8"))
            dependencies = payload.get("endpoint_dependencies", payload)
            if isinstance(dependencies, dict):
                for endpoint_id, services in dependencies.items():
                    if str(endpoint_id) not in DEFAULT_EXPECTED_ENDPOINT_WEIGHTS:
                        continue
                    if isinstance(services, list):
                        for service in services:
                            if service:
                                relevant.add(str(service))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return relevant


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def load_policy():
    policy = dict(DEFAULT_POLICY)
    policy["profile_aggregate_thresholds"] = dict(DEFAULT_POLICY["profile_aggregate_thresholds"])

    if EXPORTER_POLICY_CONFIG:
        try:
            payload = json.loads(Path(EXPORTER_POLICY_CONFIG).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                policy.update({key: value for key, value in payload.items() if key != "profile_aggregate_thresholds"})
                thresholds = payload.get("profile_aggregate_thresholds")
                if isinstance(thresholds, dict):
                    policy["profile_aggregate_thresholds"].update(
                        {str(key): float(value) for key, value in thresholds.items()}
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    for field in ("endpoint_threshold", "aggregate_threshold", "cross_profile_aggregate_threshold"):
        try:
            policy[field] = float(policy[field])
        except (TypeError, ValueError, KeyError):
            policy[field] = DEFAULT_POLICY[field]

    return policy


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.ready = False
        self.last_error = ""
        self.last_success_ts = 0.0
        self.status = {}
        self.report = {}
        self.snapshot_context = {}

    def update_success(self, status, report, snapshot_context):
        with self.lock:
            self.ready = True
            self.last_error = ""
            self.last_success_ts = now_epoch()
            self.status = status
            self.report = report
            self.snapshot_context = snapshot_context

    def update_error(self, err: Exception):
        with self.lock:
            self.last_error = str(err)

    def snapshot(self):
        with self.lock:
            return {
                "ready": self.ready,
                "last_error": self.last_error,
                "last_success_ts": self.last_success_ts,
                "status": self.status,
                "report": self.report,
                "snapshot_context": self.snapshot_context,
            }


STATE = State()
EXPECTED_ENDPOINT_WEIGHTS = load_expected_endpoint_weights()
POLICY = load_policy()
RELEVANT_SERVICES = load_relevant_services()


def item_ids(items):
    ids = set()
    for item in items or []:
        value = None
        if isinstance(item, dict):
            value = item.get("id") or item.get("endpoint_id")
        elif item:
            value = item
        if value:
            ids.add(str(value))
    return ids


def load_snapshot_context():
    context = {
        "current_snapshot_id": "",
        "previous_snapshot_id": "",
        "current_missing_services": [],
        "current_missing_endpoints": [],
        "recent_removed_services": [],
        "recent_removed_endpoints": [],
    }

    try:
        current = json.loads(EXPORTER_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return context

    context["current_snapshot_id"] = str(current.get("snapshot_id") or "")
    current_services = item_ids((current.get("model") or {}).get("services"))
    current_endpoints = item_ids((current.get("model") or {}).get("endpoints"))
    if RELEVANT_SERVICES:
        context["current_missing_services"] = sorted(RELEVANT_SERVICES - current_services)
    context["current_missing_endpoints"] = sorted(
        endpoint_id for endpoint_id in EXPECTED_ENDPOINT_WEIGHTS if endpoint_id not in current_endpoints
    )

    previous = None
    try:
        files = sorted(EXPORTER_SNAPSHOT_HISTORY_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        files = []

    for path in files:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        candidate_id = str(candidate.get("snapshot_id") or "")
        if candidate_id and candidate_id != context["current_snapshot_id"]:
            previous = candidate
            break

    if not previous:
        return context

    context["previous_snapshot_id"] = str(previous.get("snapshot_id") or "")
    previous_services = item_ids((previous.get("model") or {}).get("services"))
    previous_endpoints = item_ids((previous.get("model") or {}).get("endpoints"))

    missing_services = previous_services - current_services
    if RELEVANT_SERVICES:
        missing_services = {service for service in missing_services if service in RELEVANT_SERVICES}

    missing_endpoints = {
        endpoint_id
        for endpoint_id in (previous_endpoints - current_endpoints)
        if endpoint_id in EXPECTED_ENDPOINT_WEIGHTS
    }

    context["recent_removed_services"] = sorted(missing_services)
    context["recent_removed_endpoints"] = sorted(missing_endpoints)
    return context


def normalized_profiles(report_payload):
    profiles = report_payload.get("profiles") or []
    if profiles:
        return profiles
    endpoint_results = report_payload.get("endpoint_results") or []
    if not endpoint_results:
        return []
    return [
        {
            "name": "default",
            "simulation": {
                "weighted_aggregate": report_payload.get("summary", {}).get("weighted_overall_availability", 0.0),
                "unweighted_aggregate": report_payload.get("summary", {}).get("overall_availability", 0.0),
            },
            "endpoint_results": endpoint_results,
            "decision": report_payload.get("policy_evaluation", {}).get("decision", "report"),
            "endpoints_below_threshold": 0,
        }
    ]


def modeled_profiles(report_payload):
    profiles = normalized_profiles(report_payload)
    modeled = []
    for profile in profiles:
        profile_name = profile.get("name", "default")
        raw_results = profile.get("endpoint_results") or []
        by_endpoint = {}
        for endpoint in raw_results:
            endpoint_id = endpoint.get("endpoint_id")
            if endpoint_id:
                by_endpoint[endpoint_id] = endpoint

        endpoint_results = []
        weighted_aggregate = 0.0
        unweighted_sum = 0.0
        endpoints_below_threshold = 0

        for endpoint_id, weight in EXPECTED_ENDPOINT_WEIGHTS.items():
            source = by_endpoint.get(endpoint_id, {})
            availability = float(source.get("availability", 0.0) or 0.0)
            threshold = float(source.get("threshold", POLICY["endpoint_threshold"]) or POLICY["endpoint_threshold"])
            endpoint_results.append(
                {
                    "endpoint_id": endpoint_id,
                    "availability": availability,
                    "threshold": threshold,
                    "weight": weight,
                }
            )
            weighted_aggregate += availability * weight
            unweighted_sum += availability
            if threshold and availability < threshold:
                endpoints_below_threshold += 1

        unweighted_aggregate = unweighted_sum / len(endpoint_results) if endpoint_results else 0.0
        modeled.append(
            {
                "name": profile_name,
                "simulation": {
                    "weighted_aggregate": weighted_aggregate,
                    "unweighted_aggregate": unweighted_aggregate,
                },
                "endpoint_results": endpoint_results,
                "decision": profile.get("decision", report_payload.get("policy_evaluation", {}).get("decision", "report")),
                "endpoints_below_threshold": endpoints_below_threshold,
            }
        )

    return modeled


def modeled_decision(profiles, posture, source_decision):
    if not profiles:
        return source_decision

    aggregate_threshold = POLICY["aggregate_threshold"]
    cross_profile_threshold = POLICY["cross_profile_aggregate_threshold"]
    profile_thresholds = POLICY["profile_aggregate_thresholds"]
    mode = str(POLICY.get("mode", "warn")).lower()

    severe_breach = posture < aggregate_threshold
    for profile in profiles:
        weighted = float(profile.get("simulation", {}).get("weighted_aggregate", 0.0) or 0.0)
        if weighted < aggregate_threshold:
            severe_breach = True
            break
        if any(
            float(endpoint.get("availability", 0.0) or 0.0)
            < float(endpoint.get("threshold", POLICY["endpoint_threshold"]) or POLICY["endpoint_threshold"])
            for endpoint in profile.get("endpoint_results", [])
        ):
            severe_breach = True
            break

    if severe_breach:
        return "fail"

    if posture < cross_profile_threshold:
        return "warn" if mode == "warn" else "fail"

    for profile in profiles:
        profile_name = profile.get("name", "default")
        weighted = float(profile.get("simulation", {}).get("weighted_aggregate", 0.0) or 0.0)
        threshold = float(profile_thresholds.get(profile_name, aggregate_threshold))
        if weighted < threshold:
            return "warn" if mode == "warn" else "fail"

    return "pass"


def raw_profile_aggregate(profile):
    simulation = profile.get("simulation", {})
    aggregate = profile.get("aggregate", {})
    weighted = simulation.get("weighted_aggregate")
    if weighted is None:
        weighted = aggregate.get("availability")
    unweighted = simulation.get("unweighted_aggregate")
    if unweighted is None:
        unweighted = weighted if weighted is not None else 0.0
    return {
        "weighted": float(weighted or 0.0),
        "unweighted": float(unweighted or 0.0),
    }


def expected_journey_coverage(profile):
    raw_results = profile.get("endpoint_results") or []
    observed = {
        endpoint.get("endpoint_id")
        for endpoint in raw_results
        if endpoint.get("endpoint_id") in EXPECTED_ENDPOINT_WEIGHTS
    }
    total = len(EXPECTED_ENDPOINT_WEIGHTS)
    ratio = (len(observed) / total) if total else 0.0
    return {
        "present": len(observed),
        "total": total,
        "ratio": ratio,
    }


def primary_profile(profiles):
    for profile in profiles:
        if profile.get("name") == EXPORTER_PRIMARY_PROFILE:
            return profile
    return profiles[0] if profiles else None


def metrics_payload():
    state = STATE.snapshot()
    ready = 1 if state["ready"] else 0
    status = state["status"] or {}
    report = state["report"] or {}
    source_decision = (
        status.get("decision")
        or report.get("policy_evaluation", {}).get("decision")
        or ("error" if state["last_error"] else "report")
    )

    raw_profiles = normalized_profiles(report)
    profiles = modeled_profiles(report)
    summary = report.get("summary", {})
    posture = None
    if profiles:
        posture = sum(profile["simulation"]["weighted_aggregate"] for profile in profiles) / len(profiles)
    else:
        posture = summary.get("weighted_overall_availability")
        if posture in (None, 0):
            posture = summary.get("overall_availability", 0.0)
    if posture is None:
        posture = 0.0
    decision = modeled_decision(profiles, posture, source_decision)
    decision_code = DECISION_CODES.get(decision, 4)
    snapshot_context = state.get("snapshot_context") or {}

    generated_at = status.get("generated_at") or report.get("generated_at")
    generated_ts = parse_timestamp(generated_at)
    report_age = max(0.0, now_epoch() - generated_ts) if generated_ts else 0.0

    lines = [
        "# HELP mb3r_up 1 when the bridge exporter has a current Sheaft report.",
        "# TYPE mb3r_up gauge",
        f"mb3r_up {ready}",
        "# HELP mb3r_gate_decision_code Numeric code for the current gate decision.",
        "# TYPE mb3r_gate_decision_code gauge",
        f'mb3r_gate_decision_code{{decision="{escape_label(decision)}"}} {decision_code}',
        "# HELP mb3r_gate_decision_info Info metric for the current gate decision.",
        "# TYPE mb3r_gate_decision_info gauge",
        f'mb3r_gate_decision_info{{decision="{escape_label(decision)}"}} 1',
        "# HELP mb3r_source_gate_decision_code Numeric code for the raw Sheaft gate decision.",
        "# TYPE mb3r_source_gate_decision_code gauge",
        f'mb3r_source_gate_decision_code{{decision="{escape_label(source_decision)}"}} {DECISION_CODES.get(source_decision, 4)}',
        "# HELP mb3r_posture_score Current weighted overall posture score.",
        "# TYPE mb3r_posture_score gauge",
        f"mb3r_posture_score {posture}",
        "# HELP mb3r_report_age_seconds Age of the current Sheaft report in seconds.",
        "# TYPE mb3r_report_age_seconds gauge",
        f"mb3r_report_age_seconds {report_age}",
        "# HELP mb3r_last_report_timestamp_seconds Unix timestamp of the current Sheaft report.",
        "# TYPE mb3r_last_report_timestamp_seconds gauge",
        f"mb3r_last_report_timestamp_seconds {generated_ts or 0}",
        "# HELP mb3r_last_success_timestamp_seconds Unix timestamp of the last successful poll.",
        "# TYPE mb3r_last_success_timestamp_seconds gauge",
        f"mb3r_last_success_timestamp_seconds {state['last_success_ts']}",
        "# HELP mb3r_snapshot_missing_relevant_services_count Count of posture-relevant services missing from the current model.",
        "# TYPE mb3r_snapshot_missing_relevant_services_count gauge",
        f"mb3r_snapshot_missing_relevant_services_count {len(snapshot_context.get('current_missing_services', []))}",
        "# HELP mb3r_snapshot_missing_target_endpoints_count Count of target journeys missing from the current model.",
        "# TYPE mb3r_snapshot_missing_target_endpoints_count gauge",
        f"mb3r_snapshot_missing_target_endpoints_count {len(snapshot_context.get('current_missing_endpoints', []))}",
    ]

    current_missing_services = snapshot_context.get("current_missing_services", [])
    current_missing_endpoints = snapshot_context.get("current_missing_endpoints", [])
    if current_missing_services or current_missing_endpoints:
        lines.extend(
            [
                "# HELP mb3r_missing_model_item_info Info metric for services and journeys currently missing from the model.",
                "# TYPE mb3r_missing_model_item_info gauge",
            ]
        )
        for service in current_missing_services:
            lines.append(
                f'mb3r_missing_model_item_info{{kind="service",name="{escape_label(service)}"}} 1'
            )
        for endpoint_id in current_missing_endpoints:
            lines.append(
                f'mb3r_missing_model_item_info{{kind="journey",name="{escape_label(endpoint_id)}"}} 1'
            )

    missing_services = snapshot_context.get("recent_removed_services", [])
    if missing_services:
        lines.extend(
            [
                "# HELP mb3r_snapshot_missing_relevant_service_info Info metric for posture-relevant services missing vs previous snapshot.",
                "# TYPE mb3r_snapshot_missing_relevant_service_info gauge",
            ]
        )
        for service in missing_services:
            lines.append(f'mb3r_snapshot_missing_relevant_service_info{{service="{escape_label(service)}"}} 1')

    missing_endpoints = snapshot_context.get("recent_removed_endpoints", [])
    if missing_endpoints:
        lines.extend(
            [
                "# HELP mb3r_snapshot_missing_target_endpoint_info Info metric for target journeys missing vs previous snapshot.",
                "# TYPE mb3r_snapshot_missing_target_endpoint_info gauge",
            ]
        )
        for endpoint_id in missing_endpoints:
            lines.append(f'mb3r_snapshot_missing_target_endpoint_info{{endpoint="{escape_label(endpoint_id)}"}} 1')

    for profile in profiles:
        profile_name = profile.get("name", "default")
        simulation = profile.get("simulation", {})
        lines.extend(
            [
                f'mb3r_profile_aggregate_availability{{profile="{escape_label(profile_name)}",aggregate="weighted"}} {simulation.get("weighted_aggregate", 0.0)}',
                f'mb3r_profile_aggregate_availability{{profile="{escape_label(profile_name)}",aggregate="unweighted"}} {simulation.get("unweighted_aggregate", 0.0)}',
                f'mb3r_endpoints_below_threshold{{profile="{escape_label(profile_name)}"}} {profile.get("endpoints_below_threshold", 0)}',
            ]
        )
        for endpoint in profile.get("endpoint_results", []):
            endpoint_id = endpoint.get("endpoint_id", "")
            lines.extend(
                [
                    f'mb3r_endpoint_availability{{profile="{escape_label(profile_name)}",endpoint="{escape_label(endpoint_id)}"}} {endpoint.get("availability", 0.0)}',
                    f'mb3r_endpoint_threshold{{profile="{escape_label(profile_name)}",endpoint="{escape_label(endpoint_id)}"}} {endpoint.get("threshold", 0.0)}',
                ]
            )

    raw_profiles_by_name = {profile.get("name", "default"): profile for profile in raw_profiles}
    for profile_name, raw_profile in raw_profiles_by_name.items():
        aggregates = raw_profile_aggregate(raw_profile)
        coverage = expected_journey_coverage(raw_profile)
        lines.extend(
            [
                f'mb3r_profile_simulated_availability{{profile="{escape_label(profile_name)}",aggregate="weighted"}} {aggregates["weighted"]}',
                f'mb3r_profile_simulated_availability{{profile="{escape_label(profile_name)}",aggregate="unweighted"}} {aggregates["unweighted"]}',
                f'mb3r_expected_journey_coverage_ratio{{profile="{escape_label(profile_name)}"}} {coverage["ratio"]}',
                f'mb3r_expected_journey_coverage_count{{profile="{escape_label(profile_name)}"}} {coverage["present"]}',
                f'mb3r_expected_journey_total{{profile="{escape_label(profile_name)}"}} {coverage["total"]}',
            ]
        )

    if state["last_error"]:
        lines.extend(
            [
                "# HELP mb3r_last_error_info Last poll error; value is always 1 when present.",
                "# TYPE mb3r_last_error_info gauge",
                f'mb3r_last_error_info{{message="{escape_label(state["last_error"])}"}} 1',
            ]
        )

    return "\n".join(lines) + "\n"


def poll_loop():
    while True:
        try:
            status = json_get(f"{SHEAFT_BASE_URL}/status")
            report = json_get(f"{SHEAFT_BASE_URL}/current-report")
            snapshot_context = load_snapshot_context()
            write_json(OUTPUT_DIR / "status.json", status)
            write_json(OUTPUT_DIR / "current-report.json", report)
            write_json(OUTPUT_DIR / "snapshot-context.json", snapshot_context)
            STATE.update_success(status, report, snapshot_context)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as err:
            STATE.update_error(err)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = metrics_payload().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/healthz":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/readyz":
            state = STATE.snapshot()
            body = json.dumps({"ready": state["ready"], "last_error": state["last_error"]}).encode("utf-8")
            self.send_response(200 if state["ready"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    host, port = LISTEN.rsplit(":", 1)
    thread = threading.Thread(target=poll_loop, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
