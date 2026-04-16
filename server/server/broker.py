# File location: /server/server/broker.py
from __future__ import annotations

import importlib
import logging
import os
import socket
from typing import Any, Dict, List, Optional

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
django.setup()

from django.conf import settings as django_settings
import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AgeLimit, Callbacks, Pipelines, Retries, TimeLimit
import redis

from server.redis_connection import construct_redis_url
from server.worker_setup import ResourceInitMiddleware
from scheduler.middleware import InflightReleaseMiddleware

logger = logging.getLogger(__name__)

def _build_redis_client() -> redis.StrictRedis:
    config: Dict[str, Any] = django_settings.REDIS_CONFIG
    url = construct_redis_url(config)
    pool_max = int(config.get('POOL_MAX', 50))
    sock_timeout = config.get('TIMEOUT')

    pool = redis.BlockingConnectionPool.from_url(
        url,
        max_connections=pool_max,
        timeout=30,
        retry_on_timeout=True,
        socket_timeout=sock_timeout,
        socket_connect_timeout=sock_timeout,
        socket_keepalive=True,
        health_check_interval=int(config.get('HEALTH_CHECK_INTERVAL', 30)),
    )

    env = getattr(django_settings, 'ENVIRONMENT', os.getenv('DJANGO_ENVIRONMENT', 'development'))
    client_name = f'dramatiq:{env}:{socket.gethostname()}'
    client = redis.StrictRedis(connection_pool=pool, client_name=client_name)

    client.ping()
    logger.info('Dramatiq Redis client ready (pool_max=%s).', pool_max)
    return client

def _namespace() -> str:
    env = getattr(django_settings, 'ENVIRONMENT', os.getenv('DJANGO_ENVIRONMENT', 'development'))
    return f'dramatiq:{env}'

def _build_middleware(client: redis.StrictRedis) -> List[dramatiq.middleware.Middleware]:
    middlewares: List[dramatiq.middleware.Middleware] = [
        AgeLimit(),
        TimeLimit(),
        Callbacks(),
        Pipelines(),
        Retries(
            max_retries=django_settings.DRAMATIQ_MAX_RETRIES,
            min_backoff=django_settings.DRAMATIQ_MIN_BACKOFF_MS,
            max_backoff=django_settings.DRAMATIQ_MAX_BACKOFF_MS,
        ),
        ResourceInitMiddleware(),
    ]

    if django_settings.DRAMATIQ_USE_RESULTS:
        try:
            from dramatiq.results import Results
            from dramatiq.results.backends.redis import RedisBackend
            ns = f'{_namespace()}_results'
            middlewares.append(Results(backend=RedisBackend(client=client, namespace=ns)))
            logger.info('Dramatiq Results enabled (namespace=%s).', ns)
        except Exception as err:
            logger.error('Failed to enable Dramatiq Results: %s', err, exc_info=True)

    # Last in chain → after_process_message fires first (Dramatiq
    # runs after-hooks in reverse order).  This ensures the in-flight
    # slot is freed the instant the actor finishes, before any other
    # after-hook runs.
    middlewares.append(InflightReleaseMiddleware(redis_client=client))

    return middlewares

def _build_broker(client: redis.StrictRedis) -> RedisBroker:
    ns = _namespace()
    b = RedisBroker(
        client=client,
        namespace=ns,
        heartbeat_timeout=django_settings.DRAMATIQ_HEARTBEAT_TIMEOUT_MS,
        dead_message_ttl=django_settings.DRAMATIQ_DEAD_TTL_MS,
        maintenance_chance=django_settings.DRAMATIQ_MAINTENANCE_CHANCE,
        middleware=_build_middleware(client),
    )
    logger.info('Dramatiq RedisBroker ready (namespace=%s).', ns)
    return b

_client = _build_redis_client()
broker: RedisBroker = _build_broker(_client)
dramatiq.set_broker(broker)

__all__ = ['broker']

def _import_task_modules() -> None:
    env_mods = [m.strip() for m in os.getenv('DRAMATIQ_IMPORTS', '').split(',') if m.strip()]
    if env_mods:
        for mod in env_mods:
            importlib.import_module(mod)
            logger.info('Imported tasks module: %s', mod)
        return

    settings_mods: List[str] = getattr(django_settings, 'DRAMATIQ_TASK_MODULES', []) or []
    if settings_mods:
        for mod in settings_mods:
            importlib.import_module(mod)
            logger.info('Imported tasks module: %s', mod)
        return

    skip = {'django', 'rest_framework', 'knox'}
    for app_label in getattr(django_settings, 'INSTALLED_APPS', []):
        if any(app_label.startswith(s) for s in skip):
            continue
        module = f'{app_label}.tasks'
        try:
            importlib.import_module(module)
            logger.info('Auto-imported tasks: %s', module)
        except ModuleNotFoundError as err:
            if err.name == module:
                continue
            raise

_import_task_modules()
