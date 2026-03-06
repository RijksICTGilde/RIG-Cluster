"""OpenTelemetry tracing setup for OPI Operations Manager.

Provides distributed tracing with automatic instrumentation for FastAPI,
httpx, aiohttp, asyncpg, and log correlation. Exports traces via OTLP gRPC
to a SigNoz OTel Collector.
"""

import logging

from fastapi import FastAPI

from opi.core.config import VERSION, settings

logger = logging.getLogger(__name__)

_tracer_provider = None


def setup_tracing(app: FastAPI) -> None:
    """Initialize OpenTelemetry tracing if OTEL_ENABLED is True.

    Configures a TracerProvider with OTLP gRPC exporter and instruments
    FastAPI, httpx, aiohttp, asyncpg, and logging for trace correlation.
    """
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

    # Build resource attributes
    resource = Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": VERSION,
            "deployment.environment": settings.ENVIRONMENT,
        }
    )

    # Configure sampler
    sampler = TraceIdRatioBased(float(settings.OTEL_TRACES_SAMPLER_ARG))

    # Create tracer provider
    _tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    # OTLP gRPC exporter
    exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
    _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    # Set as global tracer provider
    trace.set_tracer_provider(_tracer_provider)

    # Instrument FastAPI (exclude health/metrics endpoints)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,healthz,readyz,metrics")

    # Instrument outbound HTTP clients
    HTTPXClientInstrumentor().instrument()
    AioHttpClientInstrumentor().instrument()

    # Instrument database
    AsyncPGInstrumentor().instrument()

    # Inject trace_id/span_id into log records
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
