# File location: /server/scheduler/seed.py

import logging

from .models import ScheduledTask

logger = logging.getLogger('scheduler')



SYSTEM_TASKS = []


def seed_system_tasks() -> tuple[int, int]:
    created = 0
    updated = 0
    deactivated = 0

    _SCHEDULE_FIELDS = {
        'schedule_type', 'cron_expression', 'interval_seconds',
        'run_at', 'user_timezone',
    }

    for defn in SYSTEM_TASKS:
        name = defn['name']

        defaults = {
            'description': defn.get('description', ''),
            'task_path': defn['task_path'],
            'schedule_type': defn['schedule_type'],
            'cron_expression': defn.get('cron_expression', ''),
            'interval_seconds': defn.get('interval_seconds'),
            'queue': defn.get('queue', 'default'),
            'misfire_grace_seconds': defn.get('misfire_grace_seconds', 300),
            'is_system': True,
        }

        try:
            existing = ScheduledTask.objects.get(name=name)
        except ScheduledTask.DoesNotExist:
            existing = None

        if existing is None:
            obj = ScheduledTask(**defaults, name=name, is_active=True)
            obj.save()
            created += 1
            logger.info('Created system task: %s', name)
        else:
            schedule_changed = any(
                str(getattr(existing, field, None)) != str(defaults.get(field))
                for field in _SCHEDULE_FIELDS
                if field in defaults
            )

            for field, value in defaults.items():
                setattr(existing, field, value)

            if schedule_changed or not existing.next_run_at:
                existing.next_run_at = existing.compute_next_run()
                if schedule_changed:
                    logger.info(
                        'Schedule changed for %s, recomputed next_run_at=%s',
                        name, existing.next_run_at,
                    )

            existing.save()
            updated += 1
            logger.info('Updated system task: %s', name)

    active_system_names = {task['name'] for task in SYSTEM_TASKS}
    stale_system_tasks = ScheduledTask.objects.filter(is_system=True)
    if active_system_names:
        stale_system_tasks = stale_system_tasks.exclude(name__in=active_system_names)

    deactivated += stale_system_tasks.update(
        is_active=False,
        auto_paused=False,
    )
    if deactivated:
        logger.info(
            'Deactivated %d stale system schedule(s) not present in this branch.',
            deactivated,
        )

    return created, updated
