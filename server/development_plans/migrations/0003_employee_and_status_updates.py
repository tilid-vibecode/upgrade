import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0002_employeerolematch'),
        ('development_plans', '0002_rename_developmen_workspa_7b6323_idx_development_workspa_d155fc_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='developmentplanrun',
            name='employee',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='development_plans', to='org_context.employee'),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'employee'], name='development_workspa_63eb2c_idx'),
        ),
    ]
