import importlib
import logging
import os
import signal
import socket
import time

import redis
from django.conf import settings as django_settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from server.redis_connection import construct_redis_url

from .models import ExecutionStatus, ScheduleType, ScheduledTask, TaskExecution

logger = logging.getLogger('scheduler')

TICK_SECONDS = int(os.getenv('SCHEDULER_TICK_SECONDS', '5'))
LEADER_TTL_SECONDS = int(os.getenv('SCHEDULER_LEADER_TTL', '30'))
LEADER_KEY = 'mula:scheduler:leader'
INFLIGHT_KEY_PREFIX = 'mula:scheduler:inflight'
AUTO_PAUSE_THRESHOLD = int(os.getenv('SCHEDULER_AUTO_PAUSE_FAILURES', '5'))
HEARTBEAT_PATH = '/tmp/scheduler-heartbeat'


class LeaderLock:
    _LUA_RENEW = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
    else
        return 0
    end
    """

    _LUA_RELEASE = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    else
        return 0
    end
    """

    def __init__(self, redis_client: redis.StrictRedis):
        self._redis = redis_client
        self._identity = f'{socket.gethostname()}:{os.getpid()}'

    @property
    def identity(self) -> str:
        return self._identity

    def acquire_or_renew(self) -> bool:
        acquired = self._redis.set(
            LEADER_KEY,
            self._identity,
            nx=True,
            ex=LEADER_TTL_SECONDS,
        )
        if acquired:
            return True

        result = self._redis.eval(
            self._LUA_RENEW,
            1,
            LEADER_KEY,
            self._identity,
            str(LEADER_TTL_SECONDS),
        )
        return result not in (0, None)

    def release(self) -> None:
        result = self._redis.eval(
            self._LUA_RELEASE,
            1,
            LEADER_KEY,
            self._identity,
        )
        if result:
            logger.info('Released leader lock.')


def _resolve_actor(task_path: str):
    module_path, _, func_name = task_path.rpartition('.')
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


class SchedulerService:
    _INFLIGHT_SAFETY_FLOOR = 15

    def __init__(self):
        self._running = True
        self._redis = self._build_redis_client()
        self._leader = LeaderLock(self._redis)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            'Scheduler starting (identity=%s, tick=%ds, leader_ttl=%ds).',
            self._leader.identity,
            TICK_SECONDS,
            LEADER_TTL_SECONDS,
        )

        while self._running:
            try:
                self._write_heartbeat()

                if self._leader.acquire_or_renew():
                    self._tick()
                else:
                    logger.debug('Not leader, standing by.')
            except Exception as exc:
                logger.error('Scheduler tick failed: %s', exc, exc_info=True)
            finally:
                close_old_connections()

            time.sleep(TICK_SECONDS)

        self._leader.release()
        logger.info('Scheduler stopped.')

    def _tick(self) -> None:
        now = timezone.now()
        due_ids = list(
            ScheduledTask.objects
            .filter(is_active=True, next_run_at__lte=now)
            .values_list('id', flat=True)
        )

        if not due_ids:
            return

        logger.info('Found %d due task(s).', len(due_ids))

        for task_id in due_ids:
            if not self._leader.acquire_or_renew():
                logger.warning(
                    'Lost leadership mid-tick after %d/%d tasks, aborting.',
                    due_ids.index(task_id),
                    len(due_ids),
                )
                return

            self._claim_and_dispatch(task_id, now)

    def _claim_and_dispatch(self, task_id: int, now) -> None:
        task = None

        try:
            with transaction.atomic():
                try:
                    task = (
                        ScheduledTask.objects
                        .select_for_update(skip_locked=True)
                        .get(id=task_id, is_active=True, next_run_at__lte=now)
                    )
                except ScheduledTask.DoesNotExist:
                    return

                if task.next_run_at and task.misfire_grace_seconds:
                    overdue = (now - task.next_run_at).total_seconds()
                    if overdue > task.misfire_grace_seconds:
                        logger.warning(
                            'Skipping %s — overdue by %ds (grace=%ds).',
                            task.name,
                            overdue,
                            task.misfire_grace_seconds,
                        )
                        self._record_execution(task, ExecutionStatus.SKIPPED, now)
                        self._advance_schedule(task, now)
                        return

                if not self._acquire_inflight_slot(task):
                    logger.info(
                        'Skipping %s — %d instance(s) already in-flight (max_instances=%d).',
                        task.name,
                        self._count_inflight(task),
                        task.max_instances,
                    )
                    self._record_execution(task, ExecutionStatus.SKIPPED, now)
                    self._advance_schedule(task, now)
                    return

                self._advance_schedule(task, now)

        except Exception as exc:
            logger.error('Failed to claim task id=%s: %s', task_id, exc, exc_info=True)
            return

        try:
            actor = _resolve_actor(task.task_path)
        except (ImportError, AttributeError) as exc:
            logger.error('Cannot resolve actor %s: %s', task.task_path, exc)
            self._release_inflight_slot(task)
            self._record_execution(task, ExecutionStatus.FAILED, now, error=str(exc))
            self._record_dispatch_failure(task, str(exc))
            return

        try:
            kwargs = task.task_kwargs or {}
            message = actor.message_with_options(
                kwargs=kwargs,
                scheduler_inflight_key=self._inflight_key(task),
            )
            if task.queue:
                message = message.copy(queue_name=task.queue)
            actor.broker.enqueue(message)

            message_id = message.message_id or ''
            logger.info(
                'Dispatched %s → %s (queue=%s, msg=%s).',
                task.name,
                task.task_path,
                message.queue_name,
                message_id,
            )
        except Exception as exc:
            logger.error('Failed to dispatch %s: %s', task.name, exc, exc_info=True)
            self._release_inflight_slot(task)
            self._record_execution(task, ExecutionStatus.FAILED, now, error=str(exc))
            self._record_dispatch_failure(task, str(exc))
            return

        self._promote_inflight_slot(task, str(message_id))
        self._record_execution(
            task,
            ExecutionStatus.DISPATCHED,
            now,
            dramatiq_message_id=str(message_id),
        )
        self._record_dispatch_success(task)

    @staticmethod
    def _record_dispatch_failure(task: ScheduledTask, error: str) -> None:
        try:
            new_count = task.consecutive_failures + 1
            task.consecutive_failures = new_count

            if new_count >= AUTO_PAUSE_THRESHOLD:
                task.is_active = False
                task.auto_paused = True
                task.save(update_fields=['consecutive_failures', 'is_active', 'auto_paused', 'updated_at'])
                logger.error(
                    'Auto-paused task %s after %d consecutive failures. Last error: %s.',
                    task.name,
                    new_count,
                    error,
                )
            else:
                task.save(update_fields=['consecutive_failures', 'updated_at'])
                logger.warning(
                    'Task %s failed (%d/%d before auto-pause): %s',
                    task.name,
                    new_count,
                    AUTO_PAUSE_THRESHOLD,
                    error,
                )
        except Exception as exc:
            logger.error('Failed to record dispatch failure for %s: %s', task.name, exc)

    @staticmethod
    def _record_dispatch_success(task: ScheduledTask) -> None:
        if task.consecutive_failures > 0 or task.auto_paused:
            try:
                task.consecutive_failures = 0
                task.auto_paused = False
                task.save(update_fields=['consecutive_failures', 'auto_paused', 'updated_at'])
            except Exception as exc:
                logger.error('Failed to reset failure counter for %s: %s', task.name, exc)

    @staticmethod
    def _inflight_key(task: ScheduledTask) -> str:
        return f'{INFLIGHT_KEY_PREFIX}:{task.name}'

    def _inflight_ttl(self, task: ScheduledTask) -> int:
        if task.schedule_type == ScheduleType.INTERVAL and task.interval_seconds:
            ttl = task.interval_seconds * 6
        else:
            ttl = task.misfire_grace_seconds or 300
        return max(ttl, self._INFLIGHT_SAFETY_FLOOR)

    def _count_inflight(self, task: ScheduledTask) -> int:
        key = self._inflight_key(task)
        now_ts = time.time()
        pipe = self._redis.pipeline(transaction=False)
        pipe.zremrangebyscore(key, '-inf', now_ts)
        pipe.zcard(key)
        _, count = pipe.execute()
        return count

    def _acquire_inflight_slot(self, task: ScheduledTask) -> bool:
        key = self._inflight_key(task)
        now_ts = time.time()
        expire_at = now_ts + self._inflight_ttl(task)
        placeholder = f'_pending:{self._leader.identity}:{now_ts}'

        lua = """
        redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", ARGV[1])
        local count = redis.call("ZCARD", KEYS[1])
        if count < tonumber(ARGV[2]) then
            redis.call("ZADD", KEYS[1], ARGV[3], ARGV[4])
            return 1
        end
        return 0
        """
        result = self._redis.eval(
            lua,
            1,
            key,
            str(now_ts),
            str(task.max_instances),
            str(expire_at),
            placeholder,
        )

        task._inflight_placeholder = placeholder
        task._inflight_expire_at = expire_at

        if result == 1:
            self._redis.expire(key, self._inflight_ttl(task) * 2)

        return result == 1

    def _release_inflight_slot(self, task: ScheduledTask) -> None:
        placeholder = getattr(task, '_inflight_placeholder', None)
        if placeholder:
            self._redis.zrem(self._inflight_key(task), placeholder)

    def _promote_inflight_slot(self, task: ScheduledTask, message_id: str) -> None:
        key = self._inflight_key(task)
        placeholder = getattr(task, '_inflight_placeholder', None)
        expire_at = getattr(task, '_inflight_expire_at', time.time() + self._INFLIGHT_SAFETY_FLOOR)

        if placeholder:
            pipe = self._redis.pipeline(transaction=True)
            pipe.zrem(key, placeholder)
            pipe.zadd(key, {message_id: expire_at})
            pipe.execute()

    def _advance_schedule(self, task: ScheduledTask, now) -> None:
        next_run = task.compute_next_run(after=now)

        if next_run is None:
            task.is_active = False
            task.next_run_at = None
            task.last_run_at = now
            task.save(update_fields=['is_active', 'next_run_at', 'last_run_at', 'updated_at'])
            logger.info('Deactivated one-shot task: %s', task.name)
        else:
            task.next_run_at = next_run
            task.last_run_at = now
            task.save(update_fields=['next_run_at', 'last_run_at', 'updated_at'])

    @staticmethod
    def _record_execution(
        task: ScheduledTask,
        status: str,
        dispatched_at,
        error: str = '',
        dramatiq_message_id: str = '',
    ) -> None:
        try:
            TaskExecution.objects.create(
                task=task,
                status=status,
                dispatched_at=dispatched_at,
                error=error,
                dramatiq_message_id=dramatiq_message_id,
            )
        except Exception as exc:
            logger.error('Failed to record execution for %s: %s', task.name, exc)

    @staticmethod
    def _write_heartbeat() -> None:
        try:
            with open(HEARTBEAT_PATH, 'w') as f:
                f.write(str(time.time()))
        except OSError:
            pass

    def _handle_signal(self, signum, frame):
        signame = signal.Signals(signum).name
        logger.info('Received %s — shutting down gracefully.', signame)
        self._running = False

    @staticmethod
    def _build_redis_client() -> redis.StrictRedis:
        config = django_settings.REDIS_CONFIG
        url = construct_redis_url(config)
        return redis.StrictRedis.from_url(
            url,
            decode_responses=True,
            socket_timeout=int(config.get('TIMEOUT') or 5),
            socket_connect_timeout=int(config.get('TIMEOUT') or 5),
            retry_on_timeout=True,
        )
