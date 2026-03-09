"""OpenTelemetry SDK initialization for alerting-service."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_OTLP_DEFAULT_ENDPOINT = "http://localhost:4317"


def init_tracing(
    service_name: str,
    version: str = "unknown",
    endpoint: str | None = None,
) -> trace.Tracer:
    """Initialise OTel TracerProvider with OTLP gRPC export.

    Args:
        service_name: Logical service name (used as OTel resource attribute).
        version: Service version string (used as OTel resource attribute).
        endpoint: OTLP collector endpoint. Defaults to http://localhost:4317.

    Returns:
        A Tracer instance bound to the configured provider.
    """
    endpoint = endpoint or _OTLP_DEFAULT_ENDPOINT

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
        }
    )

    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    logger.info("[%s] OTel tracing initialised — OTLP endpoint: %s", service_name, endpoint)
    return trace.get_tracer(service_name, version)
