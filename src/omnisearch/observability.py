import logging
from threading import Lock

import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_lock = Lock()
_configured = False


def configure_observability(service_name: str = "omnisearch", endpoint: str | None = None) -> None:
    global _configured
    with _lock:
        if _configured:
            return
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if endpoint:
            exporter_endpoint = (
                endpoint
                if endpoint.rstrip("/").endswith("/v1/traces")
                else endpoint.rstrip("/") + "/v1/traces"
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=exporter_endpoint))
            )
        trace.set_tracer_provider(provider)
        _configured = True


tracer = trace.get_tracer("omnisearch")
meter = metrics.get_meter("omnisearch")
search_requests = meter.create_counter("omnisearch.search.requests")
search_failures = meter.create_counter("omnisearch.search.failures")
documents_fetched = meter.create_counter("omnisearch.documents.fetched")
