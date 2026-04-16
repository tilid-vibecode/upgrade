from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0003_workspacesource_notes'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workspacesource',
            name='status',
            field=models.CharField(
                choices=[
                    ('attached', 'Attached'),
                    ('parsing', 'Parsing'),
                    ('parsed', 'Parsed'),
                    ('failed', 'Failed'),
                ],
                default='attached',
                max_length=16,
            ),
        ),
    ]
