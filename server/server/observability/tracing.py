# File location: /server/server/observability/tracing.py
import logging
import os

logger = logging.getLogger(__name__)

def init_tracing() -> None:
    if os.getenv('OTEL_ENABLE', '').lower() != 'true':
        logger.info('OTel tracing disabled (OTEL_ENABLE != true).')
        return

    try:
        from server.observability.telemetry import configure_tracer
        configure_tracer()

        try:
            from opentelemetry.instrumentation.django import DjangoInstrumentor
            DjangoInstrumentor().instrument(
                is_sql_commentor_enabled=True,
                request_hook=_request_hook,
                response_hook=_response_hook,
            )
            logger.info('Django OTel instrumentation enabled.')
        except Exception as err:
            logger.warning('Django instrumentation failed: %s', err)

        try:
            from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
            Psycopg2Instrumentor().instrument(enable_commenter=True, skip_dep_check=True)
            logger.info('PostgreSQL OTel instrumentation enabled.')
        except Exception as err:
            logger.warning('PostgreSQL instrumentation failed: %s', err)

        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor
            RequestsInstrumentor().instrument()
            logger.info('Requests OTel instrumentation enabled.')
        except Exception as err:
            logger.warning('Requests instrumentation failed: %s', err)

        try:
            from opentelemetry.instrumentation.logging import LoggingInstrumentor
            LoggingInstrumentor().instrument(set_logging_format=False)
        except Exception as err:
            logger.warning('Logging instrumentation failed: %s', err)

        logger.info('OTel tracing initialised for Django.')

    except ImportError as err:
        logger.error(
            'OTel tracing dependencies missing: %s. '
            'Install opentelemetry-distro and opentelemetry-instrumentation.',
            err,
        )
    except Exception as err:
        logger.error('OTel tracing init failed: %s', err)

def _request_hook(span, request) -> None:
    if span and span.is_recording():
        span.set_attribute('http.request.body.size', request.headers.get('content-length', 0))
        span.set_attribute('http.user_agent', request.headers.get('user-agent', 'unknown'))
        if hasattr(request, 'user') and request.user.is_authenticated:
            span.set_attribute('user.id', request.user.id)

def _response_hook(span, request, response) -> None:
    if span and span.is_recording() and hasattr(response, 'content'):
        span.set_attribute('http.response.body.size', len(response.content))
