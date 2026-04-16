import json
import logging
import os
from datetime import datetime, timedelta, timezone

import dramatiq
import redis

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Q, Sum

from server.redis_connection import construct_redis_url
from .constants import (
    STREAM_KEY,
    CONSUMER_GROUP,
    CONSUMER_NAME_PREFIX,
    FLUSH_BATCH_SIZE,
    RAW_LOG_RETENTION_DAYS,
    HOURLY_AGG_RETENTION_DAYS,
)

logger = logging.getLogger(__name__)


def _get_worker_id() -> str:
    return f'{CONSUMER_NAME_PREFIX}_{os.getpid()}_{os.getenv("HOSTNAME", "local")}'


def _get_sync_redis() -> redis.Redis:
    url = construct_redis_url(django_settings.REDIS_CONFIG)
    return redis.from_url(url, decode_responses=True)


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes')
    return bool(value)


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_trim_stream(r) -> None:
    try:
        groups = r.xinfo_groups(STREAM_KEY)
    except redis.exceptions.ResponseError:
        logger.info('Redis stream does not exist, skipping trim')
        return

    group_exists = any(g['name'] == CONSUMER_GROUP for g in groups)
    if not group_exists:
        logger.info('Consumer group does not exist yet, skipping stream trim')
        return

    pending_summary = r.xpending(STREAM_KEY, CONSUMER_GROUP)
    num_pending = pending_summary['pending']

    if num_pending > 0:
        oldest_pending_id = pending_summary['min']
        trimmed = r.xtrim(STREAM_KEY, minid=oldest_pending_id)
        logger.info(
            f'Trimmed {trimmed} acknowledged entries from Redis stream '
            f'(oldest pending: {oldest_pending_id})'
        )
    else:
        group_info = next(g for g in groups if g['name'] == CONSUMER_GROUP)
        last_delivered = group_info['last-delivered-id']

        if last_delivered == '0-0':
            logger.info('Consumer group has not delivered any entries yet, skipping trim')
            return

        parts = last_delivered.split('-')
        trim_boundary = f'{parts[0]}-{int(parts[1]) + 1}'

        trimmed = r.xtrim(STREAM_KEY, minid=trim_boundary)
        logger.info(
            f'Trimmed {trimmed} entries from Redis stream '
            f'(all acknowledged up to {last_delivered})'
        )


@dramatiq.actor(max_retries=3, min_backoff=5_000, max_backoff=60_000)
def flush_raw_logs_from_redis():
    import time

    r = _get_sync_redis()
    consumer_name = _get_worker_id()

    try:
        r.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id='0', mkstream=True)
    except redis.exceptions.ResponseError as e:
        if 'BUSYGROUP' not in str(e):
            raise

    _process_pending(r, consumer_name)

    max_seconds = int(os.getenv('FLUSH_MAX_SECONDS', '25'))
    total_flushed = 0
    deadline = time.monotonic() + max_seconds

    while time.monotonic() < deadline:
        entries = r.xreadgroup(
            CONSUMER_GROUP,
            consumer_name,
            {STREAM_KEY: '>'},
            count=FLUSH_BATCH_SIZE,
            block=2000,
        )

        if not entries:
            break

        stream_entries = entries[0][1]
        _flush_entries(r, stream_entries)
        total_flushed += len(stream_entries)

        if len(stream_entries) < FLUSH_BATCH_SIZE:
            break

    if total_flushed:
        logger.info('Flush complete: %d entries in %.1fs.', total_flushed, max_seconds - (deadline - time.monotonic()))


def _process_pending(r, consumer_name: str) -> None:
    try:
        result = r.xautoclaim(
            STREAM_KEY,
            CONSUMER_GROUP,
            consumer_name,
            min_idle_time=5 * 60 * 1000,
            start_id='0-0',
            count=FLUSH_BATCH_SIZE,
        )
        if result and len(result) >= 2 and result[1]:
            _flush_entries(r, result[1])
    except redis.exceptions.ResponseError:
        pass


def _flush_entries(r, stream_entries: list) -> None:
    from .models import LLMRawLog

    rows = []
    ack_ids = []

    for entry_id, data in stream_entries:
        try:
            tool_names_raw = data.get('tool_names', '[]')
            try:
                tool_names = json.loads(tool_names_raw)
            except (json.JSONDecodeError, TypeError):
                tool_names = []

            discussion_uuid = data.get('discussion_uuid') or None
            if discussion_uuid in ('', 'None', 'null'):
                discussion_uuid = None

            user_uuid = data.get('user_uuid') or None
            if user_uuid in ('', 'None', 'null'):
                user_uuid = None

            rows.append(LLMRawLog(
                stream_entry_id=entry_id,
                organization_uuid=data['organization_uuid'],
                user_uuid=user_uuid,
                discussion_uuid=discussion_uuid,
                is_org_member=_parse_bool(data.get('is_org_member', True)),
                provider=data['provider'],
                model=data['model'],
                prompt_tokens=_parse_int(data.get('prompt_tokens')),
                completion_tokens=_parse_int(data.get('completion_tokens')),
                total_tokens=_parse_int(data.get('total_tokens')),
                is_successful=_parse_bool(data.get('is_successful', False)),
                error_type=data.get('error_type', ''),
                call_type=data.get('call_type', 'completion'),
                tool_names=tool_names,
                caller_function=data.get('caller_function', ''),
                iteration=_parse_int(data.get('iteration')),
                attempt=_parse_int(data.get('attempt')),
                estimated_cost_micro=_parse_int(data.get('estimated_cost_micro')),
                provider_request_id=data.get('provider_request_id', ''),
                called_at=datetime.fromisoformat(data['called_at']),
            ))
            ack_ids.append(entry_id)

        except Exception as exc:
            logger.error(f'Failed to parse stream entry {entry_id}: {exc}', exc_info=True)
            ack_ids.append(entry_id)

    if rows:
        LLMRawLog.objects.bulk_create(rows, ignore_conflicts=True)
        logger.info(f'Flushed {len(rows)} raw LLM logs to Postgres')

    if ack_ids:
        r.xack(STREAM_KEY, CONSUMER_GROUP, *ack_ids)

@dramatiq.actor(max_retries=2, min_backoff=10_000)
def aggregate_hourly_usage():
    from .models import (
        LLMRawLog,
        HourlyUsageAggregate,
    )

    now = datetime.now(timezone.utc)
    hour_end = now.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)

    raw_qs = LLMRawLog.objects.filter(
        called_at__gte=hour_start,
        called_at__lt=hour_end,
    )

    if not raw_qs.exists():
        logger.info(f'No raw logs for {hour_start:%Y-%m-%d %H:00}, skipping aggregation')
        return

    org_aggs = raw_qs.values(
        'organization_uuid', 'provider', 'model',
    ).annotate(
        total_calls=Count('id'),
        successful_calls=Count('id', filter=Q(is_successful=True)),
        failed_calls=Count('id', filter=Q(is_successful=False)),
        tool_call_turns=Count('id', filter=Q(call_type='tool_call')),
        tool_followup_turns=Count('id', filter=Q(call_type='tool_followup')),
        sum_prompt=Sum('prompt_tokens'),
        sum_completion=Sum('completion_tokens'),
        sum_total=Sum('total_tokens'),
        sum_cost=Sum('estimated_cost_micro'),
        unique_users=Count('user_uuid', distinct=True),
        unique_discussions=Count('discussion_uuid', distinct=True),
    )

    for agg in org_aggs:
        HourlyUsageAggregate.objects.update_or_create(
            hour=hour_start,
            organization_uuid=agg['organization_uuid'],
            provider=agg['provider'],
            model=agg['model'],
            defaults={
                'total_calls': agg['total_calls'],
                'successful_calls': agg['successful_calls'],
                'failed_calls': agg['failed_calls'],
                'tool_call_turns': agg['tool_call_turns'],
                'tool_followup_turns': agg['tool_followup_turns'],
                'prompt_tokens': agg['sum_prompt'] or 0,
                'completion_tokens': agg['sum_completion'] or 0,
                'total_tokens': agg['sum_total'] or 0,
                'estimated_cost_micro': agg['sum_cost'] or 0,
                'unique_users': agg['unique_users'],
                'unique_discussions': agg['unique_discussions'],
            },
        )

    user_aggs = (
        raw_qs
        .exclude(user_uuid__isnull=True)
        .values('user_uuid', 'organization_uuid', 'provider', 'model')
        .annotate(
            cnt=Count('id'),
            sum_total=Sum('total_tokens'),
            sum_prompt=Sum('prompt_tokens'),
            sum_completion=Sum('completion_tokens'),
            sum_cost=Sum('estimated_cost_micro'),
        )
    )

    for ua in user_aggs:
        key = {
            'user_uuid': ua['user_uuid'],
            'organization_uuid': ua['organization_uuid'],
            'provider': ua['provider'],
            'model': ua['model'],
        }
        with transaction.atomic():
            old_vals, new_vals = _write_user_hourly_contribution(ua, hour_start)
            key['_old_contribution'] = old_vals
            key['_new_contribution'] = new_vals
            _recompute_user_summary(key)

    discussion_aggs = (
        raw_qs
        .exclude(discussion_uuid__isnull=True)
        .values('discussion_uuid', 'organization_uuid', 'provider', 'model')
        .annotate(
            cnt=Count('id'),
            sum_total=Sum('total_tokens'),
            sum_prompt=Sum('prompt_tokens'),
            sum_completion=Sum('completion_tokens'),
            sum_cost=Sum('estimated_cost_micro'),
            tool_turns=Count('id', filter=Q(call_type='tool_call')),
        )
    )

    for da in discussion_aggs:
        key = {
            'discussion_uuid': da['discussion_uuid'],
            'organization_uuid': da['organization_uuid'],
            'provider': da['provider'],
            'model': da['model'],
        }
        with transaction.atomic():
            old_vals, new_vals = _write_discussion_hourly_contribution(da, hour_start)
            key['_old_contribution'] = old_vals
            key['_new_contribution'] = new_vals
            _recompute_discussion_summary(key)

    logger.info(f'Hourly aggregation complete for {hour_start:%Y-%m-%d %H:00}')


def _write_user_hourly_contribution(ua: dict, hour_start: datetime) -> tuple:
    from .models import UserHourlyContribution

    _fields = (
        'total_calls', 'total_tokens', 'prompt_tokens',
        'completion_tokens', 'estimated_cost_micro',
    )
    _zero = {f: 0 for f in _fields}

    new_vals = {
        'total_calls': ua['cnt'],
        'total_tokens': ua['sum_total'] or 0,
        'prompt_tokens': ua['sum_prompt'] or 0,
        'completion_tokens': ua['sum_completion'] or 0,
        'estimated_cost_micro': ua['sum_cost'] or 0,
        'hour': hour_start,
    }

    lookup = dict(
        hour=hour_start,
        user_uuid=ua['user_uuid'],
        organization_uuid=ua['organization_uuid'],
        provider=ua['provider'],
        model=ua['model'],
    )

    obj, created = UserHourlyContribution.objects.get_or_create(
        **lookup,
        defaults={f: new_vals[f] for f in _fields},
    )

    if created:
        return _zero, new_vals

    old_vals = {f: getattr(obj, f) or 0 for f in _fields}

    for f in _fields:
        setattr(obj, f, new_vals[f])
    obj.save(update_fields=list(_fields) + ['updated_at'])

    return old_vals, new_vals


def _recompute_user_summary(key: dict) -> None:
    from .models import UserUsageSummary

    old_vals, new_vals = key['_old_contribution'], key['_new_contribution']

    delta_calls = (new_vals['total_calls'] or 0) - (old_vals['total_calls'] or 0)
    delta_total = (new_vals['total_tokens'] or 0) - (old_vals['total_tokens'] or 0)
    delta_prompt = (new_vals['prompt_tokens'] or 0) - (old_vals['prompt_tokens'] or 0)
    delta_completion = (new_vals['completion_tokens'] or 0) - (old_vals['completion_tokens'] or 0)
    delta_cost = (new_vals['estimated_cost_micro'] or 0) - (old_vals['estimated_cost_micro'] or 0)

    updated = UserUsageSummary.objects.filter(
        user_uuid=key['user_uuid'],
        organization_uuid=key['organization_uuid'],
        provider=key['provider'],
        model=key['model'],
    ).update(
        total_calls=F('total_calls') + delta_calls,
        total_tokens=F('total_tokens') + delta_total,
        prompt_tokens=F('prompt_tokens') + delta_prompt,
        completion_tokens=F('completion_tokens') + delta_completion,
        estimated_cost_micro=F('estimated_cost_micro') + delta_cost,
        last_call_at=new_vals['hour'],
    )

    if not updated:
        try:
            with transaction.atomic():
                UserUsageSummary.objects.create(
                    user_uuid=key['user_uuid'],
                    organization_uuid=key['organization_uuid'],
                    provider=key['provider'],
                    model=key['model'],
                    total_calls=new_vals['total_calls'] or 0,
                    total_tokens=new_vals['total_tokens'] or 0,
                    prompt_tokens=new_vals['prompt_tokens'] or 0,
                    completion_tokens=new_vals['completion_tokens'] or 0,
                    estimated_cost_micro=new_vals['estimated_cost_micro'] or 0,
                    last_call_at=new_vals['hour'],
                )
        except IntegrityError:
            UserUsageSummary.objects.filter(
                user_uuid=key['user_uuid'],
                organization_uuid=key['organization_uuid'],
                provider=key['provider'],
                model=key['model'],
            ).update(
                total_calls=F('total_calls') + delta_calls,
                total_tokens=F('total_tokens') + delta_total,
                prompt_tokens=F('prompt_tokens') + delta_prompt,
                completion_tokens=F('completion_tokens') + delta_completion,
                estimated_cost_micro=F('estimated_cost_micro') + delta_cost,
                last_call_at=new_vals['hour'],
            )


def _write_discussion_hourly_contribution(da: dict, hour_start: datetime) -> tuple:
    from .models import DiscussionHourlyContribution

    _fields = (
        'total_calls', 'total_tokens', 'prompt_tokens',
        'completion_tokens', 'estimated_cost_micro', 'tool_call_turns',
    )
    _zero = {f: 0 for f in _fields}

    new_vals = {
        'total_calls': da['cnt'],
        'total_tokens': da['sum_total'] or 0,
        'prompt_tokens': da['sum_prompt'] or 0,
        'completion_tokens': da['sum_completion'] or 0,
        'estimated_cost_micro': da['sum_cost'] or 0,
        'tool_call_turns': da['tool_turns'] or 0,
        'hour': hour_start,
    }

    lookup = dict(
        hour=hour_start,
        discussion_uuid=da['discussion_uuid'],
        provider=da['provider'],
        model=da['model'],
    )

    obj, created = DiscussionHourlyContribution.objects.get_or_create(
        **lookup,
        defaults={
            'organization_uuid': da['organization_uuid'],
            **{f: new_vals[f] for f in _fields},
        },
    )

    if created:
        return _zero, new_vals

    old_vals = {f: getattr(obj, f) or 0 for f in _fields}

    for f in _fields:
        setattr(obj, f, new_vals[f])
    obj.save(update_fields=list(_fields) + ['updated_at'])

    return old_vals, new_vals


def _recompute_discussion_summary(key: dict) -> None:
    from .models import DiscussionUsageSummary

    old_vals, new_vals = key['_old_contribution'], key['_new_contribution']

    _metric_fields = (
        'total_calls', 'total_tokens', 'prompt_tokens',
        'completion_tokens', 'estimated_cost_micro', 'tool_call_turns',
    )
    deltas = {
        f: (new_vals.get(f) or 0) - (old_vals.get(f) or 0)
        for f in _metric_fields
    }

    updated = DiscussionUsageSummary.objects.filter(
        discussion_uuid=key['discussion_uuid'],
        provider=key['provider'],
        model=key['model'],
    ).update(**{f: F(f) + deltas[f] for f in _metric_fields})

    if not updated:
        try:
            with transaction.atomic():
                DiscussionUsageSummary.objects.create(
                    discussion_uuid=key['discussion_uuid'],
                    organization_uuid=key['organization_uuid'],
                    provider=key['provider'],
                    model=key['model'],
                    **{f: new_vals.get(f) or 0 for f in _metric_fields},
                )
        except IntegrityError:
            DiscussionUsageSummary.objects.filter(
                discussion_uuid=key['discussion_uuid'],
                provider=key['provider'],
                model=key['model'],
            ).update(**{f: F(f) + deltas[f] for f in _metric_fields})

@dramatiq.actor(max_retries=2, min_backoff=10_000)
def rollup_daily_usage():
    from .models import LLMRawLog, HourlyUsageAggregate, DailyUsageAggregate

    now = datetime.now(timezone.utc)
    target_day = (now - timedelta(days=1)).date()

    day_start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    hourly_qs = HourlyUsageAggregate.objects.filter(
        hour__gte=day_start, hour__lt=day_end,
    )

    if not hourly_qs.exists():
        logger.info('No hourly aggregates for %s, skipping daily rollup', target_day)
        return

    daily_aggs = hourly_qs.values(
        'organization_uuid', 'provider', 'model',
    ).annotate(
        sum_calls=Sum('total_calls'),
        sum_success=Sum('successful_calls'),
        sum_failed=Sum('failed_calls'),
        sum_tool_call=Sum('tool_call_turns'),
        sum_tool_followup=Sum('tool_followup_turns'),
        sum_prompt=Sum('prompt_tokens'),
        sum_completion=Sum('completion_tokens'),
        sum_total=Sum('total_tokens'),
        sum_cost=Sum('estimated_cost_micro'),
    )

    raw_distincts = {
        (r['organization_uuid'], r['provider'], r['model']): r
        for r in (
            LLMRawLog.objects
            .filter(called_at__gte=day_start, called_at__lt=day_end)
            .values('organization_uuid', 'provider', 'model')
            .annotate(
                unique_users=Count('user_uuid', distinct=True),
                unique_discussions=Count('discussion_uuid', distinct=True),
            )
        )
    }

    for agg in daily_aggs:
        org_uuid = agg['organization_uuid']
        prov = agg['provider']
        mdl = agg['model']

        distincts = raw_distincts.get((org_uuid, prov, mdl), {})

        DailyUsageAggregate.objects.update_or_create(
            day=target_day,
            organization_uuid=org_uuid,
            provider=prov,
            model=mdl,
            defaults={
                'total_calls': agg['sum_calls'] or 0,
                'successful_calls': agg['sum_success'] or 0,
                'failed_calls': agg['sum_failed'] or 0,
                'tool_call_turns': agg['sum_tool_call'] or 0,
                'tool_followup_turns': agg['sum_tool_followup'] or 0,
                'prompt_tokens': agg['sum_prompt'] or 0,
                'completion_tokens': agg['sum_completion'] or 0,
                'total_tokens': agg['sum_total'] or 0,
                'estimated_cost_micro': agg['sum_cost'] or 0,
                'unique_users': distincts.get('unique_users', 0),
                'unique_discussions': distincts.get('unique_discussions', 0),
            },
        )

    logger.info('Daily rollup complete for %s', target_day)


@dramatiq.actor(max_retries=1)
def cleanup_old_usage_data():
    from .models import (
        LLMRawLog, HourlyUsageAggregate,
        UserHourlyContribution, DiscussionHourlyContribution,
    )

    now = datetime.now(timezone.utc)

    raw_cutoff = now - timedelta(days=RAW_LOG_RETENTION_DAYS)
    raw_deleted, _ = LLMRawLog.objects.filter(called_at__lt=raw_cutoff).delete()
    logger.info('Cleaned up %d raw logs older than %s', raw_deleted, raw_cutoff.strftime('%Y-%m-%d'))

    hourly_cutoff = now - timedelta(days=HOURLY_AGG_RETENTION_DAYS)
    hourly_deleted, _ = HourlyUsageAggregate.objects.filter(hour__lt=hourly_cutoff).delete()
    logger.info('Cleaned up %d hourly aggregates older than %s', hourly_deleted, hourly_cutoff.strftime('%Y-%m-%d'))

    contrib_cutoff = now - timedelta(days=HOURLY_AGG_RETENTION_DAYS)

    for model_cls, label in [
        (UserHourlyContribution, 'user contributions'),
        (DiscussionHourlyContribution, 'discussion contributions'),
    ]:
        total_deleted = 0
        while True:
            batch_ids = list(
                model_cls.objects
                .filter(hour__lt=contrib_cutoff)
                .values_list('pk', flat=True)[:5000]
            )
            if not batch_ids:
                break
            deleted, _ = model_cls.objects.filter(pk__in=batch_ids).delete()
            total_deleted += deleted

        if total_deleted:
            logger.info('Compacted %d %s older than %s', total_deleted, label, contrib_cutoff.strftime('%Y-%m-%d'))

    try:
        r = _get_sync_redis()
        _safe_trim_stream(r)
    except Exception as exc:
        logger.warning('Failed to trim Redis stream: %s', exc)
