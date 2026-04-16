from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0005_roleprofile_blueprint_run'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='roleprofile',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='roleprofile',
            constraint=models.UniqueConstraint(
                condition=models.Q(blueprint_run__isnull=False),
                fields=('workspace', 'blueprint_run', 'name', 'seniority'),
                name='org_ctx_roleprofile_run_unique',
            ),
        ),
        migrations.AddConstraint(
            model_name='roleprofile',
            constraint=models.UniqueConstraint(
                condition=models.Q(blueprint_run__isnull=True),
                fields=('workspace', 'name', 'seniority'),
                name='org_ctx_roleprofile_published_unique',
            ),
        ),
    ]
