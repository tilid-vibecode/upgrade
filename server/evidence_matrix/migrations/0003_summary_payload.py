from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evidence_matrix', '0002_rename_evidence_ma_workspa_7ce624_idx_evidence_ma_workspa_5823a4_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='evidencematrixrun',
            name='summary_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
