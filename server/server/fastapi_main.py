# File location: /server/server/fastapi_main.py
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

import django
from django.apps import apps as django_apps

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
if not django_apps.ready:
    django.setup()

from django.conf import settings as dj_settings
from django.db import connection

import server.broker  # noqa: F401  — side-effect import

logger = logging.getLogger('fastapi')

_MAX_REDIS_RETRIES = int(os.getenv('MAX_REDIS_STARTUP_RETRIES', '3'))
_REDIS_RETRY_DELAY = int(os.getenv('REDIS_RETRY_DELAY_SECONDS', '5'))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('FastAPI startup sequence beginning.')

    # ── Redis (required) ─────────────────────────────────────────────
    from server.redis_connection import (
        initialize_redis_client,
        get_redis_client,
        close_redis_client,
    )

    for attempt in range(1, _MAX_REDIS_RETRIES + 1):
        try:
            await initialize_redis_client()
            logger.info('Redis client initialised.')
            break
        except Exception as err:
            logger.error(
                'Redis init attempt %s/%s failed: %s',
                attempt, _MAX_REDIS_RETRIES, err,
            )
            if attempt < _MAX_REDIS_RETRIES:
                await asyncio.sleep(_REDIS_RETRY_DELAY)
            else:
                logger.critical('Redis is required — exiting.')
                sys.exit(1)

    # ── Rate limiter ─────────────────────────────────────────────────
    try:
        from server.rate_limit import initialize_fastapi_rate_limiter

        redis_client = await get_redis_client()
        await initialize_fastapi_rate_limiter(redis_client)
    except Exception as err:
        logger.error('Rate limiter init failed (non-fatal): %s', err, exc_info=True)

    # ── Storage (processing + persistent + static) — required ──────────
    from server.storage import initialize_storage

    try:
        await initialize_storage()
    except Exception as err:
        logger.critical('Storage init failed — exiting: %s', err, exc_info=True)
        sys.exit(1)

    # ── Qdrant (non-fatal) ───────────────────────────────────────────
    try:
        from server.qdrant_manager import initialize_qdrant_manager

        await initialize_qdrant_manager()
    except Exception as err:
        logger.warning('Qdrant init failed (non-fatal): %s', err)

    # ── Embedding manager (non-fatal) ────────────────────────────────
    try:
        from server.embedding_manager import initialize_embedding_manager

        await initialize_embedding_manager()
    except Exception as err:
        logger.warning('Embedding manager init failed (non-fatal): %s', err)

    # ── Database prewarm ─────────────────────────────────────────────
    try:
        await asyncio.to_thread(lambda: connection.cursor().execute('SELECT 1'))
        logger.info('DB prewarm OK.')
    except Exception as err:
        logger.warning('DB prewarm failed: %s', err)

    logger.info('FastAPI startup sequence completed.')

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info('FastAPI shutdown sequence beginning.')

    try:
        from fastapi_limiter import FastAPILimiter

        await FastAPILimiter.close()
    except Exception as err:
        logger.warning('Error closing rate limiter: %s', err)

    try:
        from server.storage import close_storage

        await close_storage()
    except Exception as err:
        logger.warning('Error closing storage: %s', err)

    try:
        from server.qdrant_manager import close_qdrant_manager

        await close_qdrant_manager()
    except Exception as err:
        logger.warning('Error closing Qdrant: %s', err)

    try:
        await close_redis_client()
    except Exception as err:
        logger.warning('Error closing Redis: %s', err)

    logger.info('FastAPI shutdown complete.')


app = FastAPI(
    title='Upgrade',
    description='AI-powered product development platform',
    version='0.1.0',
    debug=dj_settings.DEBUG,
    lifespan=lifespan,
)

try:
    from server.observability.telemetry import instrument_fastapi

    instrument_fastapi(app)
except Exception as err:
    logger.warning('FastAPI tracing not configured: %s', err)

allow_origins = list(getattr(dj_settings, 'CORS_ALLOWED_ORIGINS', []) or [])
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

_hosts = getattr(dj_settings, 'ALLOWED_HOSTS', [])
if _hosts and _hosts != ['*']:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_hosts))

from server.health import health_router  # noqa: E402
from company_intake.fastapi_views import company_intake_router
from company_intake.prototype_fastapi_views import prototype_workspace_router
from media_storage.fastapi_views import media_router
from media_storage.prototype_fastapi_views import prototype_media_router
from org_context.prototype_fastapi_views import prototype_org_context_router, prototype_planning_context_router
from skill_blueprint.prototype_fastapi_views import prototype_skill_blueprint_router
from employee_assessment.prototype_fastapi_views import prototype_employee_assessment_router
from evidence_matrix.prototype_fastapi_views import prototype_evidence_matrix_router
from development_plans.prototype_fastapi_views import prototype_development_plans_router

# ── Main API router (everything under /api/v1) ──────────────────────────
api_router = APIRouter()

api_router.include_router(health_router)
api_router.include_router(company_intake_router)
api_router.include_router(prototype_workspace_router)
api_router.include_router(media_router)
api_router.include_router(prototype_media_router)
api_router.include_router(prototype_org_context_router)
api_router.include_router(prototype_planning_context_router)
api_router.include_router(prototype_skill_blueprint_router)
api_router.include_router(prototype_employee_assessment_router)
api_router.include_router(prototype_evidence_matrix_router)
api_router.include_router(prototype_development_plans_router)

app.include_router(api_router, prefix='/api/v1')

logger.info('FastAPI application configured.')
