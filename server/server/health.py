# File location: /server/server/health.py
import logging
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

health_router = APIRouter(prefix='/health', tags=['Health'])

@health_router.get('/live')
async def liveness_probe() -> Dict[str, str]:
    return {'status': 'alive'}

@health_router.get('/ready')
async def readiness_probe() -> JSONResponse:
    checks: List[Dict[str, Any]] = []
    all_healthy = True

    try:
        from server.redis_connection import get_redis_client
        redis_client = await get_redis_client()
        await redis_client.ping()
        checks.append({'service': 'redis', 'healthy': True})
    except Exception as err:
        checks.append({'service': 'redis', 'healthy': False, 'error': str(err)})
        all_healthy = False

    try:
        from server.storage import processing_client, persistent_client
        processing_client()
        persistent_client()
        checks.append({'service': 'storage', 'healthy': True})
    except Exception as err:
        checks.append({'service': 'storage', 'healthy': False, 'error': str(err)})
        all_healthy = False

    status_code = 200 if all_healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={
            'status': 'ready' if all_healthy else 'not_ready',
            'checks': checks,
        },
    )

@health_router.get('/detailed')
async def detailed_health_check() -> JSONResponse:
    checks: List[Dict[str, Any]] = []
    critical_healthy = True
    all_healthy = True

    # ── Redis (critical) ─────────────────────────────────────────
    try:
        from server.redis_connection import get_redis_client
        redis_client = await get_redis_client()
        await redis_client.ping()
        checks.append({'service': 'redis', 'healthy': True, 'critical': True})
    except Exception as err:
        checks.append({'service': 'redis', 'healthy': False, 'critical': True, 'error': str(err)})
        critical_healthy = False
        all_healthy = False

    # ── Storage: processing (critical) ───────────────────────────
    try:
        from server.storage import processing_client
        client = processing_client()
        await client.ensure_bucket()
        checks.append({'service': 'storage_processing', 'healthy': True, 'critical': True})
    except Exception as err:
        checks.append({'service': 'storage_processing', 'healthy': False, 'critical': True, 'error': str(err)})
        critical_healthy = False
        all_healthy = False

    # ── Storage: persistent (critical) ───────────────────────────
    try:
        from server.storage import persistent_client
        client = persistent_client()
        await client.ensure_bucket()
        checks.append({'service': 'storage_persistent', 'healthy': True, 'critical': True})
    except Exception as err:
        checks.append({'service': 'storage_persistent', 'healthy': False, 'critical': True, 'error': str(err)})
        critical_healthy = False
        all_healthy = False

    # ── Qdrant (non-critical) ────────────────────────────────────
    try:
        from server.qdrant_manager import get_qdrant_manager
        qdrant = await get_qdrant_manager()
        health = await qdrant.health_check()
        health['critical'] = False
        checks.append(health)
        if not health.get('healthy'):
            all_healthy = False
    except Exception as err:
        checks.append({'service': 'qdrant', 'healthy': False, 'critical': False, 'error': str(err)})
        all_healthy = False

    # ── Embedding manager (non-critical) ─────────────────────────
    try:
        from server.embedding_manager import get_embedding_manager
        embedding = await get_embedding_manager()
        health = await embedding.health_check()
        health['critical'] = False
        checks.append(health)
        if not health.get('healthy'):
            all_healthy = False
    except Exception as err:
        checks.append({'service': 'embedding', 'healthy': False, 'critical': False, 'error': str(err)})
        all_healthy = False

    if critical_healthy and all_healthy:
        status = 'healthy'
        code = 200
    elif critical_healthy:
        status = 'degraded'
        code = 200
    else:
        status = 'unhealthy'
        code = 503

    return JSONResponse(
        status_code=code,
        content={
            'status': status,
            'critical_healthy': critical_healthy,
            'all_healthy': all_healthy,
            'checks': checks,
        },
    )
