# MB3R Overlay For OpenTelemetry Demo

This overlay keeps the upstream Astronomy Shop demo intact and adds an MB3R evaluation path on top of it:

- the existing OpenTelemetry Collector fans out traces to Bering
- Bering emits rolling discovery snapshots under [`mb3r/out/artifacts`](./out/artifacts)
- Sheaft watches the latest snapshot, computes resilience posture, and writes report history under [`mb3r/out/history`](./out/history)
- a tiny bridge exporter mirrors the current Sheaft report under [`mb3r/out/reports`](./out/reports) and exposes Prometheus metrics for Grafana
- the existing Grafana instance gets an additional MB3R dashboard under the existing `/grafana/` route

## Start

From the repository root:

```powershell
docker compose --env-file .env --env-file .env.override `
  -f docker-compose.yml `
  -f mb3r/docker-compose.mb3r.yml `
  up -d
```

Primary UI entry points:

- Demo UI: `http://localhost:8080`
- Grafana: `http://localhost:8080/grafana/`
- MB3R dashboard: `http://localhost:8080/grafana/d/mb3r-resilience/mb3r-resilience-posture`

Additional host ports:

- Bering: `http://localhost:14318`
- Sheaft: `http://localhost:19080`
- MB3R exporter: `http://localhost:19113`

## Stop

```powershell
docker compose --env-file .env --env-file .env.override `
  -f docker-compose.yml `
  -f mb3r/docker-compose.mb3r.yml `
  down --remove-orphans
```

## Raw Outputs

Useful host-visible files:

- Latest Bering snapshot: [`mb3r/out/artifacts/latest-snapshot.json`](./out/artifacts/latest-snapshot.json)
- Rolling Bering snapshots: [`mb3r/out/artifacts/snapshots`](./out/artifacts/snapshots)
- Current mirrored Sheaft status: [`mb3r/out/reports/status.json`](./out/reports/status.json)
- Current mirrored Sheaft report: [`mb3r/out/reports/current-report.json`](./out/reports/current-report.json)
- Sheaft report history: [`mb3r/out/history`](./out/history)
- Healthy baseline captured by the fault demo: [`mb3r/out/baseline/healthy-report.json`](./out/baseline/healthy-report.json)
- Healthy baseline metrics captured by the fault demo: [`mb3r/out/baseline/healthy-metrics.json`](./out/baseline/healthy-metrics.json)

Useful runtime endpoints:

- Bering health: `http://localhost:14318/healthz`
- Bering metrics: `http://localhost:14318/metrics`
- Sheaft status: `http://localhost:19080/status`
- Sheaft current report: `http://localhost:19080/current-report`
- Bridge exporter metrics: `http://localhost:19113/metrics`

## Collector Integration

The existing collector stays in place. The overlay adds one extra merge file at [`mb3r/config/otel-collector/otelcol-config.mb3r.yml`](./config/otel-collector/otelcol-config.mb3r.yml) that:

- keeps the existing Jaeger and spanmetrics trace exporters
- adds `otlphttp/bering` for traces only
- scrapes Bering and the MB3R bridge exporter through the collector’s Prometheus receiver

No demo metrics or logs are exported to Bering.

## Astronomy Shop Semantics

The Sheaft predicate contract in [`mb3r/config/sheaft/predicate-contract.yaml`](./config/sheaft/predicate-contract.yaml) models immediate user-visible success for these frontend API journeys:

- `frontend:GET /api/products` depends on `frontend` and `product-catalog`
- `frontend:GET /api/recommendations` depends on `frontend` and `recommendation`
- `frontend:GET /api/cart` depends on `frontend` and `cart`
- `frontend:POST /api/checkout` depends on `frontend`, `checkout`, `cart`, `payment`, and `shipping`

The intent is to keep Kafka and other async edges out of the immediate-response posture decision for this milestone.

## Dashboard

The MB3R dashboard is provisioned additively into the existing Grafana instance and shows:

- current gate decision
- current weighted posture score
- Sheaft report freshness
- Bering snapshot freshness
- weighted aggregate posture by analysis profile
- per-endpoint posture by profile
- short explanatory context for how to interpret the results

The bridge exporter is intentionally Astronomy Shop aware:

- it keeps the four target frontend journeys as the MB3R metric surface
- it zero-fills missing expected journeys so Grafana shows a visible posture drop when a required path falls out of the current discovery window
- it uses Sheaft’s cross-profile weighted aggregate as the overall `mb3r_posture_score`

### How To Read The Dashboard

Read the panels in this order:

1. `Gate Decision`
   Current Sheaft gate result for the latest report.
   Severity order is `pass -> warn -> fail -> error`.

2. `Overall Posture`
   The headline MB3R score from `0` to `1`.
   Higher is better.
   In this demo, roughly `0.75+` is a healthy-looking baseline.

3. `Sheaft Report Age` and `Bering Snapshot Age`
   Freshness indicators.
   Lower is better.
   These panels use green for fresh data and red for stale data.

4. `Simulated Availability by Profile`
   `steady-state` is the baseline profile.
   The other profiles are deliberate fault-model projections, so they are expected to be lower even when the shop is healthy.

5. `Journey Availability by Profile`
   These are the four target frontend journeys:
   `GET /api/products`, `GET /api/recommendations`, `GET /api/cart`, and `POST /api/checkout`.
   Higher is better.
   `0` means that journey is currently missing or broken in the active trace-derived model and should be treated as bad, not good.

Color rules:

- posture and availability panels use green for high values and red for low values
- age panels use green for low values and red for high values

## Smoke Check

Run:

```powershell
powershell -ExecutionPolicy Bypass -File mb3r/scripts/smoke.ps1
```

The smoke flow:

- generates deterministic traffic for the four target frontend journeys
- waits for Bering readiness
- waits for a host-visible latest Bering snapshot
- waits for Sheaft readiness and a current report
- confirms Prometheus can query a healthy-enough MB3R posture score
- confirms the Grafana MB3R dashboard is provisioned

## Controlled Failure Demo

Default failure demo:

```powershell
powershell -ExecutionPolicy Bypass -File mb3r/scripts/fault-demo.ps1
```

Default restore path:

```powershell
powershell -ExecutionPolicy Bypass -File mb3r/scripts/fault-demo.ps1 -Action restore
```

The default scenario stops the `recommendation` service, resets the Bering discovery window, then keeps the other storefront journeys hot with the `core` traffic mix. This lets the failed recommendations journey fall out of the current discovery window while the rest of the shop stays active, which produces a visible drop on the MB3R dashboard without collapsing the whole demo.

The restore path starts the service again, resets the Bering window, and runs the standard smoke flow so the stack returns to a healthy baseline.

## What The Dashboard Means

The dashboard is a resilience posture view, not a raw liveness screen.

- Bering reflects what the collector is currently discovering from live traces.
- Sheaft evaluates the discovered topology against explicit endpoint semantics and deterministic analysis profiles.
- The bridge exporter turns the current report and status into stable Prometheus metrics that fit the existing demo Grafana path and keeps the four target frontend journeys visible even when one drops out of the current trace-derived topology.

## What It Does Not Mean

- It is not a production-grade SLO or incident management system.
- It is not a direct substitute for raw HTTP health checks or service dashboards.
- It does not make async Kafka flows first-class success conditions for the four immediate-response user journeys in this milestone.
- Sheaft `serve` remains upstream technical-preview behavior rather than a long-term stable operational contract.

## Known Limitations

- The MB3R image pins follow the published `mb3r-stack` candidate compatibility pair: Bering `v0.1.0` and Sheaft `v0.1.1`.
- The Sheaft service mode and the bridge exporter are demo-oriented, not hardened for production.
- The Sheaft predicate contract is intentionally specific to this OpenTelemetry Demo milestone and assumes the frontend API route identities shown above.
- The Bering runtime is tuned for demo responsiveness with a `45s` rolling window rather than production-scale history retention.
- The controlled fault demo intentionally resets the Bering discovery window after stopping or restoring the service so that the next snapshot reflects the current live traffic mix instead of stale pre-fault topology.
- If you change traffic patterns or route names, revisit the default `recommendation` plus `core` scenario and the exporter endpoint config.

## Next Kubernetes Milestone

The next milestone after this Docker overlay would be:

- move the compose overlay concepts into a Helm or Kubernetes profile
- keep the collector fan-out and Grafana additions additive
- preserve the same host-visible artifacts and Sheaft current-report semantics behind cluster services
- promote the demo scripts into cluster-ready smoke and fault scenarios
