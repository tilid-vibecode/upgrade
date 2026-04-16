# File location: /server/server/redis_connection.py
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Dict, Any
from urllib.parse import quote

from django.conf import settings as django_settings
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub as AsyncPubSub
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_clients: Dict[asyncio.AbstractEventLoop, AsyncRedis] = {}
_init_locks: Dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_locks_guard = threading.Lock()

def _get_init_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    """Get a per-event-loop init lock.

    AsyncIO primitives are not safe to share across event loops.
    This avoids "bound to a different event loop" errors when multiple
    loops exist in one process (e.g. Dramatiq workers with threads).
    """
    with _locks_guard:
        lock = _init_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _init_locks[loop] = lock
        return lock

def _bracket_if_ipv6(host: str) -> str:
    if ':' in host and not host.startswith('[') and host.count(':') > 1:
        return f'[{host}]'
    return host

def construct_redis_url(config: Dict[str, Any]) -> str:
    scheme = 'rediss' if config.get('SSL', False) else 'redis'

    username = config.get('USERNAME')
    password = config.get('PASSWORD')
    if username or password:
        u = quote(username or '', safe='')
        p = quote(password or '', safe='')
        auth = f'{u}:{p}@'
    else:
        auth = ''

    host = _bracket_if_ipv6(str(config.get('HOST', 'localhost')).strip())
    port = int(config.get('PORT', 6379))
    db = int(config.get('DB', 0))

    return f'{scheme}://{auth}{host}:{port}/{db}'

async def _create_client() -> AsyncRedis:
    config: Dict[str, Any] = django_settings.REDIS_CONFIG
    redis_url = construct_redis_url(config)

    client = AsyncRedis.from_url(
        redis_url,
        decode_responses=config.get('DECODE_RESPONSES', True),
        max_connections=config.get('POOL_MAX'),
        socket_timeout=config.get('TIMEOUT'),
        socket_connect_timeout=config.get('TIMEOUT'),
        health_check_interval=config.get('HEALTH_CHECK_INTERVAL', 30),
    )
    await client.ping()
    logger.info('Connected to Redis (%s).', redis_url)
    return client

async def initialize_redis_client() -> None:
    await get_redis_client()
    logger.info('Redis client initialised for loop %s.', id(asyncio.get_running_loop()))

async def get_redis_client() -> AsyncRedis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client:
        return client

    async with _get_init_lock(loop):
        if loop not in _clients:
            try:
                _clients[loop] = await _create_client()
            except Exception:
                _clients.pop(loop, None)
                raise
    return _clients[loop]


async def close_redis_client_for_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Close and forget the Redis client associated with a specific event loop.

    Use this when you create short-lived event loops (e.g. per-task) and want to
    avoid leaking AsyncRedis connection pools in the global cache.
    """
    if loop is None:
        loop = asyncio.get_running_loop()

    client = _clients.pop(loop, None)
    _init_locks.pop(loop, None)

    if client is None:
        return

    try:
        await client.aclose()
        logger.info('Closed Redis pool for loop %s.', id(loop))
    except Exception as err:
        logger.error('Error closing Redis pool for loop %s: %s', id(loop), err)

async def close_redis_client() -> None:
    if not _clients:
        logger.info('Redis clients already closed or never created.')
        return

    logger.info('Closing %d Redis client(s).', len(_clients))
    for loop, client in list(_clients.items()):
        try:
            await client.aclose()
            logger.info('Closed Redis pool for loop %s.', id(loop))
        except Exception as err:
            logger.error('Error closing Redis pool for loop %s: %s', id(loop), err)
    _clients.clear()

@asynccontextmanager
async def managed_redis_pubsub(redis_client: AsyncRedis, channel_name: str) -> AsyncGenerator[AsyncPubSub, None]:
    """Create a pub/sub subscription with a dedicated connection that has no socket timeout.

    Pub/sub connections are long-lived and sit idle waiting for messages,
    so the regular socket_timeout would kill them prematurely.
    """
    pubsub_client: Optional[AsyncRedis] = None
    pubsub: Optional[AsyncPubSub] = None
    try:
        # Create a dedicated Redis client for pub/sub — no socket_timeout,
        # no health_check_interval (can't PING a subscribed connection).
        config = django_settings.REDIS_CONFIG
        redis_url = construct_redis_url(config)
        pubsub_client = AsyncRedis.from_url(
            redis_url,
            decode_responses=config.get('DECODE_RESPONSES', True),
            max_connections=2,
            socket_timeout=None,
            socket_connect_timeout=config.get('TIMEOUT'),
            health_check_interval=0,
        )

        pubsub = pubsub_client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel_name)
        logger.debug(f'Subscribed to Redis channel: {channel_name!r}')
        yield pubsub
    except asyncio.CancelledError:
        logger.info(f'PubSub for channel {channel_name!r} was cancelled.')
        raise
    except RedisError as e:
        logger.error(f'Redis error on channel {channel_name!r}: {e}', exc_info=True)
        raise
    except Exception as e:
        logger.error(f'Unexpected error in managed_redis_pubsub for channel {channel_name!r}: {e}', exc_info=True)
        raise
    finally:
        if pubsub:
            try:
                await pubsub.aclose()
            except Exception as e_close:
                logger.error(f'Error closing pubsub for channel {channel_name!r}: {e_close}', exc_info=True)
        if pubsub_client:
            try:
                await pubsub_client.aclose()
            except Exception as e_close:
                logger.error(f'Error closing pubsub client for channel {channel_name!r}: {e_close}', exc_info=True)
