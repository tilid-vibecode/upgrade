# File location: /server/server/observability/logging_config.py
import logging
import logging.config
import os

logger = logging.getLogger(__name__)

class OTelContextFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from opentelemetry.trace import get_current_span
            ctx = get_current_span().get_span_context()
            if ctx and ctx.is_valid:
                record.trace_id = f'{ctx.trace_id:032x}'  # type: ignore[attr-defined]
                record.span_id = f'{ctx.span_id:016x}'  # type: ignore[attr-defined]
            else:
                record.trace_id = ''  # type: ignore[attr-defined]
                record.span_id = ''  # type: ignore[attr-defined]
        except Exception:
            record.trace_id = ''  # type: ignore[attr-defined]
            record.span_id = ''  # type: ignore[attr-defined]

        record.service = os.getenv('OTEL_SERVICE_NAME', 'upg-server')  # type: ignore[attr-defined]
        record.environment = os.getenv('DJANGO_ENVIRONMENT', 'development')  # type: ignore[attr-defined]
        return True

def configure() -> None:
    if os.getenv('ENABLE_JSON_LOGGING', 'false').lower() != 'true':
        logging.basicConfig(
            level=os.getenv('LOG_LEVEL', 'INFO'),
            format='%(asctime)s %(levelname)s %(name)s %(message)s',
        )
        return

    try:
        from pythonjsonlogger.json import JsonFormatter  # noqa: F401
    except ImportError:
        logger.warning('python-json-logger not installed — falling back to text logs.')
        logging.basicConfig(
            level=os.getenv('LOG_LEVEL', 'INFO'),
            format='%(asctime)s %(levelname)s %(name)s %(message)s',
        )
        return

    use_sentry = bool(os.getenv('SENTRY_DSN'))

    handlers = {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
            'filters': ['otel'],
        },
    }
    if use_sentry:
        handlers['sentry'] = {
            'level': 'ERROR',
            'class': 'sentry_sdk.integrations.logging.EventHandler',
        }

    root_handlers = ['console'] + (['sentry'] if use_sentry else [])

    config = {
        'version': 1,
        'disable_existing_loggers': False,
        'filters': {
            'otel': {'()': f'{__name__}.OTelContextFilter'},
        },
        'formatters': {
            'json': {
                '()': 'pythonjsonlogger.json.JsonFormatter',
                'fmt': (
                    '%(asctime)s %(levelname)s %(name)s '
                    '%(message)s %(trace_id)s %(span_id)s %(service)s'
                ),
            },
        },
        'handlers': handlers,
        'root': {
            'handlers': root_handlers,
            'level': os.getenv('LOG_LEVEL', 'INFO'),
        },
        'loggers': {
            'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': True},
            'django.request': {
                'handlers': root_handlers,
                'level': 'WARNING',
                'propagate': False,
            },
            'opentelemetry': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
            'urllib3': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        },
    }

    logging.config.dictConfig(config)
    logger.info('JSON structured logging configured.')
