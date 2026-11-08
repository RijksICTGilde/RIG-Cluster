"""OpenTelemetry tracing setup for OPI Operations Manager.

Provides distributed tracing with automatic instrumentation for FastAPI,
httpx, aiohttp, asyncpg, and log correlation. Exports traces via OTLP gRPC
to a Jaeger instance.
"""

import logging
from typing import TYPE_CHECKING

from opi.core.config import VERSION, settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_tracer_provider = None


def setup_tracing(app: FastAPI) -> None:
    """Does nothing when OTEL_ENABLED is False."""
    global _tracer_provider

    if not settings.OTEL_ENABLED:
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED=false)")
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": VERSION,
            "deployment.environment": settings.ENVIRONMENT,
        }
    )

    sampler = TraceIdRatioBased(float(settings.OTEL_TRACES_SAMPLER_ARG))

    _tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
    _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(_tracer_provider)

    # Instrument FastAPI (exclude health/metrics endpoints)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,healthz,readyz,metrics")

    HTTPXClientInstrumentor().instrument()
    AioHttpClientInstrumentor().instrument()

    AsyncPGInstrumentor().instrument()

    if settings.OTEL_LOG_CORRELATION:
        LoggingInstrumentor().instrument(set_logging_format=True)

    logger.info(
        "OpenTelemetry tracing initialized (service=%s, endpoint=%s, sampling=%s)",
        settings.OTEL_SERVICE_NAME,
        settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        settings.OTEL_TRACES_SAMPLER_ARG,
    )


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the tracer provider."""
    global _tracer_provider

    if _tracer_provider is None:
        return

    logger.info("Shutting down OpenTelemetry tracing")
    _tracer_provider.shutdown()
    _tracer_provider = None
