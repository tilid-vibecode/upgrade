from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0016_catalogresolutionreviewitem_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='skill',
            name='resolution_status',
            field=models.CharField(
                choices=[
                    ('resolved', 'Resolved'),
                    ('pending_review', 'Pending review'),
                    ('rejected', 'Rejected'),
                ],
                db_index=True,
                default='resolved',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='skill',
            name='is_operator_confirmed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='skill',
            name='source_terms',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Raw terms from CV extraction that created this provisional skill. Aids merge review.',
            ),
        ),
    ]
