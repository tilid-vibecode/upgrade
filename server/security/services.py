# File location: /server/security/services.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from redis.asyncio import Redis as AsyncRedis

from .constants import (
    CHAT_VIOLATION_THRESHOLD,
    BLOCK_DURATION_MAP,
    MAX_BLOCK_LEVEL,
    REDIS_CHAT_VIOLATIONS,
    REDIS_CHAT_BLOCKED,
    REDIS_USER_BLOCKED,
    REDIS_USER_BLOCK_LEVEL,
    REDIS_USER_BLOCK_EXPIRES,
    WARNING_MESSAGE,
    BLOCK_MESSAGE_TEMPLATE,
    USER_BLOCKED_MESSAGE_TEMPLATE,
    PERMANENT_BLOCK_MESSAGE,
)

logger = logging.getLogger(__name__)


@dataclass
class BlockStatus:
    is_blocked: bool
    reason: str = ''
    message: str = ''
    expires_at: Optional[str] = None


@dataclass
class ViolationResult:
    chat_violations: int
    blocked: bool
    block_level: int
    message: str


class SecurityService:
    def __init__(self, redis: AsyncRedis):
        self.redis = redis

    async def is_blocked(
        self,
        user_uuid: str,
        discussion_uuid: str,
    ) -> BlockStatus:
        user_key = REDIS_USER_BLOCKED.format(user_uuid=user_uuid)
        user_blocked = await self.redis.get(user_key)

        if user_blocked:
            expires_key = REDIS_USER_BLOCK_EXPIRES.format(user_uuid=user_uuid)
            expires_at = await self.redis.get(expires_key)

            level_key = REDIS_USER_BLOCK_LEVEL.format(user_uuid=user_uuid)
            level = await self.redis.get(level_key)
            level_int = int(level) if level else 0

            if level_int >= MAX_BLOCK_LEVEL:
                return BlockStatus(
                    is_blocked=True,
                    reason='user',
                    message=PERMANENT_BLOCK_MESSAGE,
                    expires_at=None,
                )

            return BlockStatus(
                is_blocked=True,
                reason='user',
                message=USER_BLOCKED_MESSAGE_TEMPLATE.format(
                    duration_text=self._format_expiry(expires_at),
                ),
                expires_at=expires_at,
            )

        chat_key = REDIS_CHAT_BLOCKED.format(discussion_uuid=discussion_uuid)
        chat_blocked = await self.redis.get(chat_key)

        if chat_blocked:
            ttl = await self.redis.ttl(chat_key)
            if ttl and ttl > 0:
                expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
                expires_str = expires.isoformat()
            else:
                expires_str = None

            return BlockStatus(
                is_blocked=True,
                reason='chat',
                message=BLOCK_MESSAGE_TEMPLATE.format(
                    duration_text=self._format_expiry(expires_str),
                ),
                expires_at=expires_str,
            )

        return BlockStatus(is_blocked=False)

    async def record_violation(
        self,
        user_uuid: str,
        discussion_uuid: str,
        org_uuid: str,
        message_text: str,
        detected_intent: str = 'off_topic',
    ) -> ViolationResult:
        chat_viol_key = REDIS_CHAT_VIOLATIONS.format(
            discussion_uuid=discussion_uuid,
        )
        chat_violations = await self.redis.incr(chat_viol_key)

        logger.info(
            'Off-topic violation #%d in chat %s by user %s',
            chat_violations, discussion_uuid, user_uuid,
        )

        if chat_violations < CHAT_VIOLATION_THRESHOLD:
            self._queue_violation_log(
                user_uuid=user_uuid,
                org_uuid=org_uuid,
                discussion_uuid=discussion_uuid,
                message_text=message_text,
                detected_intent=detected_intent,
                violation_number=chat_violations,
                resulted_in_block=False,
                block_level_applied=None,
            )
            return ViolationResult(
                chat_violations=chat_violations,
                blocked=False,
                block_level=0,
                message=WARNING_MESSAGE,
            )

        block_level = await self._apply_block(
            user_uuid=user_uuid,
            discussion_uuid=discussion_uuid,
        )

        self._queue_violation_log(
            user_uuid=user_uuid,
            org_uuid=org_uuid,
            discussion_uuid=discussion_uuid,
            message_text=message_text,
            detected_intent=detected_intent,
            violation_number=chat_violations,
            resulted_in_block=True,
            block_level_applied=block_level,
        )

        if block_level >= MAX_BLOCK_LEVEL:
            message = PERMANENT_BLOCK_MESSAGE
        else:
            duration_seconds = BLOCK_DURATION_MAP.get(block_level)
            if duration_seconds:
                expires = datetime.now(timezone.utc) + timedelta(
                    seconds=duration_seconds,
                )
                duration_text = self._format_expiry(expires.isoformat())
            else:
                duration_text = 'You are permanently restricted.'

            message = USER_BLOCKED_MESSAGE_TEMPLATE.format(
                duration_text=duration_text,
            )

        return ViolationResult(
            chat_violations=chat_violations,
            blocked=True,
            block_level=block_level,
            message=message,
        )

    async def _apply_block(
        self,
        user_uuid: str,
        discussion_uuid: str,
    ) -> int:
        level_key = REDIS_USER_BLOCK_LEVEL.format(user_uuid=user_uuid)
        raw_level = await self.redis.get(level_key)
        current_level = int(raw_level) if raw_level else 0

        new_level = min(current_level + 1, MAX_BLOCK_LEVEL)
        await self.redis.set(level_key, str(new_level))

        duration_seconds = BLOCK_DURATION_MAP.get(new_level)
        user_block_key = REDIS_USER_BLOCKED.format(user_uuid=user_uuid)
        expires_key = REDIS_USER_BLOCK_EXPIRES.format(user_uuid=user_uuid)

        if duration_seconds:
            await self.redis.set(user_block_key, '1', ex=duration_seconds)
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            ).isoformat()
            await self.redis.set(expires_key, expires_at, ex=duration_seconds)
        else:
            await self.redis.set(user_block_key, '1')
            await self.redis.set(expires_key, 'permanent')

        logger.warning(
            'Applied block: user=%s, chat=%s, level=%d, duration=%s',
            user_uuid,
            discussion_uuid,
            new_level,
            f'{duration_seconds}s' if duration_seconds else 'permanent',
        )

        return new_level

    def _queue_violation_log(
        self,
        user_uuid: str,
        org_uuid: str,
        discussion_uuid: str,
        message_text: str,
        detected_intent: str,
        violation_number: int,
        resulted_in_block: bool,
        block_level_applied: int | None,
    ) -> None:
        try:
            from .tasks import log_security_violation

            log_security_violation.send(
                user_uuid=user_uuid,
                org_uuid=org_uuid,
                discussion_uuid=discussion_uuid,
                message_text=message_text[:2000],
                detected_intent=detected_intent,
                violation_number=violation_number,
                resulted_in_block=resulted_in_block,
                block_level_applied=block_level_applied,
            )
            logger.info(
                'Queued violation log: user=%s, chat=%s, violation=#%d',
                user_uuid, discussion_uuid, violation_number,
            )
        except Exception as exc:
            logger.error(
                'Failed to queue violation log: %s (broker type: %s)',
                exc,
                type(getattr(log_security_violation, 'broker', None)).__name__
                if 'log_security_violation' in dir()
                else 'unknown',
                exc_info=True,
            )

    @staticmethod
    def _format_expiry(expires_at: str | None) -> str:
        if not expires_at or expires_at == 'permanent':
            return 'This restriction is permanent.'

        try:
            dt = datetime.fromisoformat(expires_at)
            now = datetime.now(timezone.utc)
            remaining = dt - now

            if remaining.total_seconds() <= 0:
                return 'Your restriction has expired.'

            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60

            if hours > 24:
                days = hours // 24
                return f'Your restriction expires in {days} day(s).'
            elif hours > 0:
                return f'Your restriction expires in {hours}h {minutes}m.'
            else:
                return f'Your restriction expires in {minutes} minute(s).'
        except (ValueError, TypeError):
            return 'You are temporarily restricted.'
