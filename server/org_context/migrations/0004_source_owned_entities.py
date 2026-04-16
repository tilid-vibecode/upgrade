import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0004_workspacesource_parsing_status'),
        ('org_context', '0003_rename_org_context_worksp_5a7f6e_idx_org_context_workspa_e38ed5_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='source',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='employees_owned',
                to='company_intake.workspacesource',
            ),
        ),
        migrations.AddField(
            model_name='orgunit',
            name='source',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='org_units_owned',
                to='company_intake.workspacesource',
            ),
        ),
        migrations.AddField(
            model_name='project',
            name='source',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='projects_owned',
                to='company_intake.workspacesource',
            ),
        ),
    ]
