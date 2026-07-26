# OTel Demo: FastAPI + full observability stack

A minimal CRUD API instrumented end-to-end with OpenTelemetry (traces, metrics,
logs), designed for demoing OTel concepts rather than for production use.

For a line-by-line walkthrough of how each signal is generated and
published (which file, which function, which SDK call), see
[HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Architecture

```
FastAPI app --OTLP/gRPC--> otel-collector --> Jaeger     (traces)
                                          --> Prometheus  (metrics, via scrape)
                                          --> Loki        (logs, via OTLP)
                                                              |
                                                          Grafana (dashboards +
                                                          Explore, trace<->log
                                                          correlation)
```

- **App** (`app/`): FastAPI CRUD service for `Item` records, backed by
  SQLite (kept intentionally simple — swap `DATABASE_URL` for Postgres/MySQL
  if you want to demo a real DB span too). Every request produces a trace
  (auto-instrumented FastAPI +
  SQLAlchemy spans, plus hand-written child spans), custom metrics
  (counters, a histogram, an up-down counter, plus psutil-backed
  process/system CPU and memory metrics), and structured logs with
  `trace_id`/`span_id` injected automatically. A small static demo UI
  (`app/static/index.html`) is served at `/` for click-through demos
  without curl.
- **otel-collector**: receives all 3 signals over OTLP and fans them out —
  this is the piece worth pointing at when explaining "vendor-neutral
  pipeline" and "receiver/processor/exporter" concepts.
- **Jaeger**: trace storage + UI.
- **Prometheus**: metrics storage, scraping the collector's Prometheus
  exporter endpoint.
- **Loki**: log storage, ingesting directly over OTLP.
- **Grafana**: pre-provisioned with all three datasources, including
  trace-to-log and log-to-trace linking.

## Running it

```bash
cd otel-demo
docker compose up --build
```

Wait ~10s for Loki to become healthy, then:

- API: http://localhost:8000/docs (Swagger UI)
- Jaeger UI: http://localhost:16686
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (anonymous admin access, no login needed)

## Accessing the UIs

| UI | URL | What it's for |
|---|---|---|
| **Demo web UI** | http://localhost:8000/ | Click-through page to create/list/get/delete items and fire `/chaos` requests, with links to all the observability UIs — no curl needed |
| Swagger / OpenAPI docs | http://localhost:8000/docs | Try the API interactively (`POST /items`, `GET /items`, `GET /chaos`, etc.) without curl |
| ReDoc (alt API docs) | http://localhost:8000/redoc | Read-only, more readable rendering of the same OpenAPI spec |
| Jaeger UI | http://localhost:16686 | Browse traces — select service `otel-demo-api`, inspect span trees, timings, and attributes |
| Prometheus UI | http://localhost:9090 | Run raw PromQL queries against the metrics, check scrape target health under Status → Targets |
| Grafana | http://localhost:3000 | Pre-built dashboard, Explore (Loki/Prometheus/Jaeger ad-hoc queries), trace↔log correlation. Anonymous access is enabled — no login needed |
| ↳ Grafana dashboard | http://localhost:3000/d/otel-demo/otel-demo | The pre-provisioned dashboard: traffic, errors, latency, active requests, CPU/memory, logs |
| ↳ Grafana Explore | http://localhost:3000/explore | Ad-hoc LogQL/PromQL queries and one-off trace lookups (pick a datasource in the top-left dropdown) |

Loki itself has no standalone UI — it's queried entirely through Grafana (dashboard/Explore) or its raw HTTP API (`http://localhost:3100/loki/api/v1/query_range`).

## Generating demo traffic

```bash
# Create a few items (traces spanning HTTP -> validation span -> DB insert span)
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8000/items \
    -H 'Content-Type: application/json' \
    -d "{\"name\": \"widget-$i\", \"price\": $i.50}" | jq .
done

# List and fetch
curl -s http://localhost:8000/items | jq .
curl -s http://localhost:8000/items/1 | jq .

# 404 example (error span + warning log)
curl -s http://localhost:8000/items/999

# Chaos endpoint: variable latency + a configurable error rate (env var
# CHAOS_ERROR_RATE, default 10%), great for showing latency histograms,
# error rate panels, and error traces/logs.
for i in $(seq 1 30); do curl -s http://localhost:8000/chaos > /dev/null; done
```

## Demo script

1. **Traces (Jaeger)** — open http://localhost:16686, select service
   `otel-demo-api`, find a `POST /items` trace. Show the span tree:
   FastAPI request span → `create_item.validate` child span → SQLAlchemy
   `INSERT` span. Point out span attributes (`item.name`, `item.price`) and
   how a `/chaos` error request has a red, `ERROR`-status span with the
   `chaos.outcome` attribute set to `error`.

2. **Metrics (pre-built Grafana dashboard)** — open
   http://localhost:3000/d/otel-demo/otel-demo. Point out request rate,
   error rate %, the live `demo_active_requests` gauge climbing while
   traffic runs, and the p50/p95/p99 latency panel showing `/chaos`
   sitting much higher than `/items`. Scroll to the "Resource Usage" row to
   show live process/system CPU utilization and process memory (RSS/VMS)
   panels, backed by `SystemMetricsInstrumentor` — no app code needed to
   emit these.

3. **Logs (same dashboard, Logs row)** — scroll to the bottom Logs panel
   (Loki), find a chaos failure line, expand it, and click the "View
   Trace" link that appears next to the `trace_id` field — jumps straight
   into the matching Jaeger trace. Demonstrates logs↔traces correlation.

4. **Vendor-neutral pipeline** — open `otel-collector-config.yaml` and
   walk through the receiver (OTLP in) → processors (batching, resource
   enrichment) → exporters (Jaeger, Prometheus, Loki) to make the point
   that the app never talks to Jaeger/Prometheus/Loki directly — only to
   the collector, which can be repointed at any backend without touching
   app code.

## Key files

| File | Purpose |
|---|---|
| `app/telemetry.py` | OTel SDK bootstrap: tracer/meter/logger providers, OTLP exporters |
| `app/main.py` | CRUD endpoints + custom spans/metrics + `/chaos` demo endpoint |
| `app/static/index.html` | Click-through demo web UI (create/list/delete items, fire chaos) |
| `otel-collector-config.yaml` | Collector receiver/processor/exporter pipeline |
| `grafana/provisioning/datasources/datasources.yml` | Prometheus/Loki/Jaeger datasources + correlation |
| `grafana/provisioning/dashboards/json/otel-demo.json` | Pre-built dashboard: traffic, errors, latency, active requests, CPU/memory, logs |

## Cleanup

```bash
docker compose down -v
```
