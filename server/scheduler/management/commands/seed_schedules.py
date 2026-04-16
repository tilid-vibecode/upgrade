from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create or update system-level scheduled tasks'

    def handle(self, *args, **options):
        from scheduler.seed import seed_system_tasks

        created, updated = seed_system_tasks()
        self.stdout.write(self.style.SUCCESS(
            f'Done: {created} created, {updated} updated.'
        ))
