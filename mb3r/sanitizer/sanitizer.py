#!/usr/bin/env python3

import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


INPUT_PATH = Path(os.environ.get("SANITIZER_INPUT_PATH", "/mb3r/out/artifacts/latest-snapshot.json"))
OUTPUT_PATH = Path(os.environ.get("SANITIZER_OUTPUT_PATH", "/mb3r/out/artifacts/latest-snapshot-sanitized.json"))
CONTRACT_PATH = Path(
    os.environ.get("SANITIZER_CONTRACT_PATH", "/etc/mb3r/sanitizer/endpoint-dependencies.json")
)
POLL_INTERVAL = float(os.environ.get("SANITIZER_POLL_INTERVAL", "2"))


def load_contract():
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    dependencies = payload.get("endpoint_dependencies", payload)
    normalized = {}
    for endpoint_id, services in (dependencies or {}).items():
        if isinstance(services, list):
            normalized[str(endpoint_id)] = [str(service) for service in services]
    return normalized


def canonical_hash(payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def sanitize_snapshot(snapshot, contract):
    sanitized = copy.deepcopy(snapshot)
    model = sanitized.get("model") or {}
    services = list(model.get("services") or [])
    service_ids = {
        str(service.get("id"))
        for service in services
        if isinstance(service, dict) and service.get("id")
    }

    filtered_edges = []
    for edge in model.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        if edge.get("from") not in service_ids or edge.get("to") not in service_ids:
            continue
        filtered_edges.append(edge)

    filtered_endpoints = []
    for endpoint in model.get("endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = endpoint.get("id")
        entry_service = endpoint.get("entry_service")
        if not endpoint_id or not entry_service or entry_service not in service_ids:
            continue

        required_services = contract.get(str(endpoint_id), [])
        if any(service not in service_ids for service in required_services):
            continue

        filtered_endpoints.append(endpoint)

    model["services"] = services
    model["edges"] = filtered_edges
    model["endpoints"] = filtered_endpoints

    if isinstance(sanitized.get("counts"), dict):
        sanitized["counts"]["services"] = len(services)
        sanitized["counts"]["edges"] = len(filtered_edges)
        sanitized["counts"]["endpoints"] = len(filtered_endpoints)

    sanitized["topology_version"] = canonical_hash(model)
    return sanitized


def main():
    contract = load_contract()
    last_input_hash = ""
    last_output_hash = ""

    while True:
        try:
            raw_payload = INPUT_PATH.read_text(encoding="utf-8")
            input_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
            if input_hash != last_input_hash:
                snapshot = json.loads(raw_payload)
                sanitized = sanitize_snapshot(snapshot, contract)
                output_hash = canonical_hash(sanitized)
                if output_hash != last_output_hash or not OUTPUT_PATH.exists():
                    write_json(OUTPUT_PATH, sanitized)
                    last_output_hash = output_hash
                last_input_hash = input_hash
        except (OSError, json.JSONDecodeError):
            pass

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
