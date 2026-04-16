from django.core.management.base import BaseCommand, CommandError


TASK_MAP = {
    'flush': 'flush_raw_logs_from_redis',
    'hourly': 'aggregate_hourly_usage',
    'daily': 'rollup_daily_usage',
    'cleanup': 'cleanup_old_usage_data',
}


class Command(BaseCommand):
    help = 'Run an LLM usage tracking task synchronously.'

    def add_arguments(self, parser):
        parser.add_argument(
            'task_name',
            type=str,
            choices=list(TASK_MAP.keys()),
            help='Which task to run: ' + ', '.join(TASK_MAP.keys()),
        )

    def handle(self, *args, **options):
        task_name = options['task_name']
        func_name = TASK_MAP[task_name]

        from llm_usage import tasks
        func = getattr(tasks, func_name, None)

        if func is None:
            raise CommandError(f'Task function {func_name} not found in llm_usage.tasks')

        self.stdout.write(f'Running {func_name}...')
        func()
        self.stdout.write(self.style.SUCCESS(f'{func_name} completed.'))
