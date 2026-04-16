# File location: /server/server/observability/telemetry.py
import atexit
import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, ResourceAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

try:
    from opentelemetry_instrumentor_dramatiq import DramatiqInstrumentor
except ImportError:
    DramatiqInstrumentor = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_TRACER_PROVIDER = None

def configure_tracer(service_name: str | None = None) -> None:
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER is not None:
        logger.warning('Tracer already configured — skipping.')
        return

    svc = service_name or os.getenv('OTEL_SERVICE_NAME', 'upg-server')

    resource = Resource(attributes={
        SERVICE_NAME: svc,
        ResourceAttributes.SERVICE_VERSION: os.getenv('SERVICE_VERSION', '0.1.0'),
        ResourceAttributes.DEPLOYMENT_ENVIRONMENT: os.getenv('DJANGO_ENVIRONMENT', 'development'),
    })

    endpoint = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4318/v1/traces')
    auth_token = os.getenv('OTEL_AUTH_TOKEN')
    headers = {'x-api-key': auth_token} if auth_token else None

    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)

    processor = BatchSpanProcessor(
        exporter,
        schedule_delay_millis=int(os.getenv('OTEL_BSP_SCHEDULE_DELAY', '5000')),
        max_queue_size=int(os.getenv('OTEL_BSP_MAX_QUEUE_SIZE', '2048')),
        max_export_batch_size=int(os.getenv('OTEL_BSP_MAX_EXPORT_BATCH_SIZE', '512')),
        export_timeout_millis=int(os.getenv('OTEL_BSP_EXPORT_TIMEOUT', '30000')),
    )

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _TRACER_PROVIDER = provider

    logger.info('OTel tracer configured: service=%s endpoint=%s', svc, endpoint)
    atexit.register(shutdown_tracer_provider)

def shutdown_tracer_provider() -> None:
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER:
        try:
            _TRACER_PROVIDER.shutdown()
        except Exception as err:
            logger.error('Error shutting down tracer: %s', err)
        _TRACER_PROVIDER = None

def instrument_django() -> None:
    DjangoInstrumentor().instrument()
    logger.info('Django instrumentation enabled.')

def instrument_fastapi(app) -> None:
    FastAPIInstrumentor.instrument_app(app)
    logger.info('FastAPI instrumentation enabled.')

def instrument_dramatiq() -> None:
    if DramatiqInstrumentor is None:
        logger.warning('opentelemetry-instrumentor-dramatiq not installed — skipping.')
        return
    DramatiqInstrumentor().instrument()
    logger.info('Dramatiq instrumentation enabled.')
