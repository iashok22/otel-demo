# How it works

This walks through exactly how each OTel signal (traces, metrics, logs) gets
generated and published in this codebase, end to end: app code → SDK →
collector → backend.

## 1. Bootstrap — `app/telemetry.py`

`init_telemetry()` (`telemetry.py:108-114`) runs once at import time
(`main.py:17`) and sets up all three providers, each exporting over
OTLP/gRPC to the collector at `OTEL_EXPORTER_OTLP_ENDPOINT`
(`telemetry.py:31`, defaults to `http://otel-collector:4317`). The app
never talks to Jaeger/Prometheus/Loki directly — only to the collector.

- **Traces** — `_setup_tracing()` (`telemetry.py:47-52`): a `TracerProvider`
  with an `OTLPSpanExporter`, wrapped in a `BatchSpanProcessor` (spans are
  buffered and flushed in batches rather than sent one at a time).
- **Metrics** — `_setup_metrics()` (`telemetry.py:55-65`): an
  `OTLPMetricExporter` feeding a `PeriodicExportingMetricReader` that
  exports every 5s, attached to a `MeterProvider`. This function also calls
  `SystemMetricsInstrumentor().instrument()`, which emits CPU/memory
  metrics with zero app code.
- **Logs** — `_setup_logging()` (`telemetry.py:68-79`): a `LoggerProvider`
  with an `OTLPLogExporter` via `BatchLogRecordProcessor`, wired into
  Python's stdlib `logging` module through an OTel `LoggingHandler`.
  `LoggingInstrumentor().instrument(set_logging_format=True)` auto-injects
  `trace_id`/`span_id` into every log record as structured metadata. The
  custom `_TraceContextTextFilter` (`telemetry.py:82-100`) additionally
  stamps those ids into the log message *text*, because Grafana's
  Loki→Jaeger derived-field link matches against line text, not structured
  metadata.

## 2. Traces — `app/main.py`

- **Auto-instrumentation** (`main.py:29-31`):
  - `FastAPIInstrumentor.instrument_app(app)` wraps every route handler in
    a request span automatically.
  - `SQLAlchemyInstrumentor().instrument(engine=engine)` wraps every DB
    query in a child span.
  - `RequestsInstrumentor().instrument()` would wrap any outbound HTTP
    calls made via the `requests` library (unused today, but demoable).
- **Manual child spans** via `tracer.start_as_current_span(...)`:
  `create_item.validate` (`main.py:71`), `list_items.query` (`main.py:98`),
  `chaos.simulated_work` (`main.py:136`). Inside these, `span.set_attribute`
  adds context (e.g. `item.price`, `chaos.delay_seconds`) and
  `span.set_status(Status(StatusCode.ERROR, ...))` marks a span as failed
  (e.g. `main.py:75`, `142`).

## 3. Metrics — `app/main.py`

Instruments are created once at module load (`main.py:34-58`) via
`meter.create_counter` / `create_histogram` / `create_up_down_counter`,
then recorded inline in each request handler:

| Instrument | Kind | Recorded at |
|---|---|---|
| `items.created` | counter | `main.py:83` (success), `main.py:87` (rejected) |
| `items.deleted` | counter | `main.py:121` |
| `demo.request.duration` | histogram (ms) | `main.py:91-93`, `147-149`, `156-158` |
| `chaos.errors` | counter | `main.py:144` |
| `demo.active_requests` | up-down counter (gauge-like) | paired `.add(1)`/`.add(-1)` around each handler, e.g. `main.py:68`/`90`, `134`/`146`/`155` |

Plus zero-code CPU/memory metrics from `SystemMetricsInstrumentor`
(`telemetry.py:63`) — `process_runtime_cpython_cpu_utilization_ratio`,
`process_runtime_cpython_memory_bytes`, `system_cpu_utilization_ratio`.

## 4. Logs — `app/main.py`

Plain stdlib calls — `logger.info/warning/error(...)` — at `main.py:84`,
`101`, `109`, `122`, `145`, `154`. Nothing in these call sites talks to
OTel directly; because the OTel `LoggingHandler` is attached to the root
logger in `_setup_logging()`, every call is automatically captured,
trace-correlated, and shipped via OTLP.

## 5. Collector — `otel-collector-config.yaml`

Receives all three signals over OTLP (grpc/http) and fans them out:

```
receivers:  otlp (grpc :4317, http :4318)
processors: batch, resource
exporters:
  traces  -> otlp/jaeger   (jaeger:4317)
  metrics -> prometheus    (0.0.0.0:8889, scraped by Prometheus)
  logs    -> otlphttp/loki (http://loki:3100/otlp)
```

This is the "vendor-neutral pipeline" piece — swap an exporter here and
you retarget a whole signal to a different backend without touching app
code.

## 6. Backends

- **Jaeger** stores and serves traces (UI at `:16686`).
- **Prometheus** scrapes the collector's `:8889` endpoint every 5s
  (`prometheus.yml`) and stores metrics as time series.
- **Loki** ingests logs directly over OTLP (`loki-config.yaml`).
- **Grafana** is pre-provisioned with all three as datasources
  (`grafana/provisioning/datasources/datasources.yml`, with explicit
  `uid`s so the Jaeger↔Loki trace/log correlation config can reference
  them) plus a pre-built dashboard
  (`grafana/provisioning/dashboards/json/otel-demo.json`).

## 7. Demo web UI — `app/static/index.html`

A static page mounted at `/` (`main.py`, `app.mount("/", StaticFiles(...))`,
added after all API routes so it never shadows them). It's plain
`fetch()` calls against the existing endpoints — no new backend logic —
so every click here generates the exact same traces/metrics/logs
described above.
