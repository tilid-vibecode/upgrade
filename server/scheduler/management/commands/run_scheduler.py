from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the task scheduler (leader-elected, single-instance)'

    def handle(self, *args, **options):
        from scheduler.seed import seed_system_tasks
        created, updated = seed_system_tasks()
        self.stdout.write(
            f'System tasks: {created} created, {updated} updated.'
        )

        import server.broker  # noqa: F401

        from scheduler.service import SchedulerService
        service = SchedulerService()

        self.stdout.write(self.style.SUCCESS('Scheduler starting...'))
        service.run()
        self.stdout.write(self.style.WARNING('Scheduler stopped.'))
