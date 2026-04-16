import django.db.models.deletion
from django.db import migrations, models


def backfill_roleprofile_blueprint_run(apps, schema_editor):
    RoleProfile = apps.get_model('org_context', 'RoleProfile')
    SkillBlueprintRun = apps.get_model('skill_blueprint', 'SkillBlueprintRun')

    run_ids = {
        str(run_id)
        for run_id in SkillBlueprintRun.objects.values_list('uuid', flat=True)
    }

    for role_profile in RoleProfile.objects.all().iterator():
        metadata = role_profile.metadata or {}
        blueprint_run_uuid = str(metadata.get('blueprint_run_uuid') or '').strip()
        if not blueprint_run_uuid or blueprint_run_uuid not in run_ids:
            continue
        role_profile.blueprint_run_id = blueprint_run_uuid
        role_profile.save(update_fields=['blueprint_run'])


class Migration(migrations.Migration):

    dependencies = [
        ('skill_blueprint', '0005_stage4_blueprint_review_flow'),
        ('org_context', '0004_source_owned_entities'),
    ]

    operations = [
        migrations.AddField(
            model_name='roleprofile',
            name='blueprint_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='role_profiles',
                to='skill_blueprint.skillblueprintrun',
            ),
        ),
        migrations.RunPython(
            backfill_roleprofile_blueprint_run,
            migrations.RunPython.noop,
        ),
        migrations.AlterUniqueTogether(
            name='roleprofile',
            unique_together={('workspace', 'blueprint_run', 'name', 'seniority')},
        ),
        migrations.AddIndex(
            model_name='roleprofile',
            index=models.Index(fields=['workspace', 'blueprint_run'], name='org_context_workspa_1c7cc3_idx'),
        ),
    ]
