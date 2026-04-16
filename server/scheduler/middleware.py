import logging

import dramatiq
import redis as sync_redis

logger = logging.getLogger('scheduler')


class InflightReleaseMiddleware(dramatiq.Middleware):

    def __init__(self, redis_client: sync_redis.StrictRedis):
        self._redis = redis_client

    def after_process_message(self, broker, message, *, result=None, exception=None):
        self._release_slot(message)

    def after_skip_message(self, broker, message):
        self._release_slot(message)

    def _release_slot(self, message: dramatiq.Message) -> None:
        inflight_key = message.options.get('scheduler_inflight_key')
        if not inflight_key:
            return

        try:
            removed = self._redis.zrem(inflight_key, message.message_id)
            if removed:
                logger.debug(
                    'Released in-flight slot: key=%s member=%s',
                    inflight_key, message.message_id,
                )
        except Exception as exc:
            logger.warning(
                'Failed to release in-flight slot (key=%s, msg=%s): %s',
                inflight_key, message.message_id, exc,
            )



def release_inflight(
    task_name: str,
    message_id: str,
    redis_client: sync_redis.StrictRedis | None = None,
) -> bool:
    from .service import INFLIGHT_KEY_PREFIX

    if redis_client is None:
        broker = dramatiq.get_broker()
        redis_client = getattr(broker, 'client', None)
        if redis_client is None:
            logger.warning(
                'Cannot release in-flight slot for %s — no Redis client available.',
                task_name,
            )
            return False

    key = f'{INFLIGHT_KEY_PREFIX}:{task_name}'
    try:
        return bool(redis_client.zrem(key, message_id))
    except Exception as exc:
        logger.warning(
            'Failed to manually release in-flight slot (%s / %s): %s',
            task_name, message_id, exc,
        )
        return False
