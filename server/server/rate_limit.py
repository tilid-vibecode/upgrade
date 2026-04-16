# File location: /server/server/rate_limit.py
import logging
import os

from fastapi import Depends, Request, Response
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter
from redis.asyncio import Redis as AsyncRedis

logger = logging.getLogger(__name__)

RATE_LIMIT_WINDOW_SECONDS = int(os.getenv('RATE_LIMIT_WINDOW_SECONDS', '60'))
RATE_LIMIT_GLOBAL_RPM = int(os.getenv('RATE_LIMIT_GLOBAL_RPM', '120'))
RATE_LIMIT_SENSITIVE_RPM = int(os.getenv('RATE_LIMIT_SENSITIVE_RPM', '20'))
RATE_LIMIT_PREFIX = os.getenv('RATE_LIMIT_PREFIX', 'upg:rl')

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',')[0].strip()
    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else 'unknown'

def _get_route_template(request: Request) -> str:
    route = request.scope.get('route')
    return getattr(route, 'path', None) or request.url.path

async def rate_limit_identifier(request: Request) -> str:
    user = getattr(request.state, 'user', None)
    user_uuid = getattr(user, 'uuid', None)

    if user_uuid:
        principal = f'user:{user_uuid}'
    else:
        principal = f'ip:{_get_client_ip(request)}'

    return f'{principal}:{request.method}:{_get_route_template(request)}'

async def _apply_rate_limit(
    *, times: int, seconds: int, request: Request, response: Response,
) -> None:
    if getattr(FastAPILimiter, 'redis', None) is None:
        return
    limiter = RateLimiter(times=times, seconds=seconds)
    await limiter(request=request, response=response)

async def global_rate_limit(request: Request, response: Response) -> None:
    await _apply_rate_limit(
        times=RATE_LIMIT_GLOBAL_RPM,
        seconds=RATE_LIMIT_WINDOW_SECONDS,
        request=request,
        response=response,
    )

async def sensitive_rate_limit(request: Request, response: Response) -> None:
    await _apply_rate_limit(
        times=RATE_LIMIT_SENSITIVE_RPM,
        seconds=RATE_LIMIT_WINDOW_SECONDS,
        request=request,
        response=response,
    )

GLOBAL_RPM = Depends(global_rate_limit)
SENSITIVE_RPM = Depends(sensitive_rate_limit)

async def initialize_fastapi_rate_limiter(redis_client: AsyncRedis) -> None:
    await FastAPILimiter.init(
        redis=redis_client,
        prefix=RATE_LIMIT_PREFIX,
        identifier=rate_limit_identifier,
    )
    logger.info(
        'Rate limiter ready (global=%s/%ss, sensitive=%s/%ss).',
        RATE_LIMIT_GLOBAL_RPM, RATE_LIMIT_WINDOW_SECONDS,
        RATE_LIMIT_SENSITIVE_RPM, RATE_LIMIT_WINDOW_SECONDS,
    )
