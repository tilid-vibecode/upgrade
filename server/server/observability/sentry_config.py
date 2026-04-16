# File location: /server/server/observability/sentry_config.py
import logging
import os

logger = logging.getLogger(__name__)

def init_sentry() -> None:
    dsn = os.getenv('SENTRY_DSN')
    if not dsn:
        logger.info('Sentry DSN not set — skipping.')
        return

    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    env = os.getenv('SENTRY_ENVIRONMENT', os.getenv('DJANGO_ENVIRONMENT', 'development'))
    release = os.getenv('SENTRY_RELEASE')
    traces_rate = float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '1.0'))
    profiles_rate = float(os.getenv('SENTRY_PROFILES_SAMPLE_RATE', '1.0'))
    debug = os.getenv('SENTRY_DEBUG', 'false').lower() == 'true'

    integrations = [
        DjangoIntegration(
            transaction_style='url',
            middleware_spans=True,
            signals_spans=True,
            cache_spans=True,
        ),
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ]

    otel_enabled = os.getenv('OTEL_ENABLE', '').lower() == 'true'

    kwargs = dict(
        dsn=dsn,
        environment=env,
        release=release,
        integrations=integrations,
        traces_sample_rate=traces_rate,
        profiles_sample_rate=profiles_rate,
        send_default_pii=False,
        enable_tracing=True,
        attach_stacktrace=True,
        max_breadcrumbs=100,
        debug=debug,
    )

    if otel_enabled:
        kwargs['instrumenter'] = 'otel'
        kwargs['propagate_traces'] = True

    try:
        sentry_sdk.init(**kwargs)
        logger.info(
            'Sentry initialised: env=%s otel=%s', env, otel_enabled,
        )
    except Exception as err:
        logger.warning('Sentry init failed: %s', err)
