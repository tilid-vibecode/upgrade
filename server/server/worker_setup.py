# File location: /server/server/worker_setup.py
import asyncio
import logging
import os

import dramatiq

logger = logging.getLogger(__name__)

_otel_enabled = (
    os.getenv('OTEL_SDK_DISABLED', 'false').lower() != 'true'
    and os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT', '')
)

if _otel_enabled:
    try:
        from server.observability.telemetry import configure_tracer, instrument_dramatiq
        configure_tracer(service_name='upg-dramatiq')
        instrument_dramatiq()
    except Exception as err:
        logger.warning('Dramatiq tracing not configured: %s', err)
else:
    logger.info('OTEL tracing disabled for worker (no collector endpoint configured).')


class ResourceInitMiddleware(dramatiq.Middleware):
    def after_worker_boot(self, broker, worker):
        logger.info('Initialising worker resources...')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._init_async_resources())
            self._init_sync_resources()
            logger.info('All worker resources initialised.')
        except Exception as err:
            logger.critical('Failed to initialise worker resources: %s', err, exc_info=True)
            raise
        finally:
            loop.close()

    async def _init_async_resources(self):
        from server.storage import initialize_storage

        await initialize_storage()
        logger.info('Storage clients initialised for worker.')

    def _init_sync_resources(self):
        try:
            from server.qdrant_manager import initialize_qdrant_manager_sync
            initialize_qdrant_manager_sync()
            logger.info('Qdrant client initialised (sync).')
        except Exception as err:
            logger.warning('Qdrant init failed in worker (non-fatal): %s', err)

        try:
            from server.embedding_manager import initialize_embedding_manager_sync
            initialize_embedding_manager_sync()
            logger.info('Embedding manager initialised (sync).')
        except Exception as err:
            logger.warning('Embedding manager init failed in worker (non-fatal): %s', err)
