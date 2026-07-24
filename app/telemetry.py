"""
Central OpenTelemetry bootstrap for the demo app.

Wires up all three signals (traces, metrics, logs) over OTLP/gRPC to the
otel-collector sidecar defined in docker-compose.yml. Everything here is
intentionally explicit (no "magic" env-var-only auto-config) so it's easy to
point at during a demo and explain what each piece does.
"""
import logging
import os

from opentelemetry import metrics, trace

try:  # public path on newer exporter releases
    from opentelemetry.exporter.otlp.proto.grpc.log_exporter import OTLPLogExporter
except ImportError:  # pragma: no cover - fallback for older releases
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTEL_COLLECTOR_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
SERVICE_NAME_VALUE = os.getenv("OTEL_SERVICE_NAME", "otel-demo-api")
SERVICE_VERSION_VALUE = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
ENVIRONMENT = os.getenv("DEPLOY_ENV", "local")


def _resource() -> Resource:
    return Resource.create(
        {
            SERVICE_NAME: SERVICE_NAME_VALUE,
            SERVICE_VERSION: SERVICE_VERSION_VALUE,
            "deployment.environment": ENVIRONMENT,
        }
    )


def _setup_tracing(resource: Resource) -> trace.Tracer:
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=OTEL_COLLECTOR_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(SERVICE_NAME_VALUE)


def _setup_metrics(resource: Resource) -> metrics.Meter:
    exporter = OTLPMetricExporter(endpoint=OTEL_COLLECTOR_ENDPOINT, insecure=True)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    # Process-level CPU/memory metrics (psutil-backed), for the Grafana
    # "resource usage" panels — no app code needed to emit these.
    SystemMetricsInstrumentor().instrument()

    return metrics.get_meter(SERVICE_NAME_VALUE)


def _setup_logging(resource: Resource) -> None:
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=OTEL_COLLECTOR_ENDPOINT, insecure=True)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    handler.addFilter(_TraceContextTextFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])

    # Injects trace_id/span_id into every log record emitted through `logging`,
    # so a log line can be pivoted to its originating trace in Grafana.
    LoggingInstrumentor().instrument(set_logging_format=True)


class _TraceContextTextFilter(logging.Filter):
    """
    The OTel LoggingHandler already attaches trace_id/span_id to exported log
    records as structured metadata, but Grafana's Loki->Jaeger derived-field
    link matches against the log line *text*. This mirrors the same ids into
    the message body so that link works, and so a log line is still
    self-describing when read as plain text (e.g. via `docker logs`).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.msg = (
                f"{record.getMessage()} "
                f"trace_id={format(span_context.trace_id, '032x')} "
                f"span_id={format(span_context.span_id, '016x')}"
            )
            record.args = ()
        return True


class Telemetry:
    tracer: trace.Tracer
    meter: metrics.Meter


def init_telemetry() -> Telemetry:
    resource = _resource()
    t = Telemetry()
    t.tracer = _setup_tracing(resource)
    t.meter = _setup_metrics(resource)
    _setup_logging(resource)
    return t
