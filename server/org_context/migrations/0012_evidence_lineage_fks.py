# Generated manually — adds explicit assessment_cycle and assessment_pack FKs
# to EmployeeSkillEvidence for evidence lineage tracking.

import django.db.models.deletion
from django.db import migrations, models


def backfill_evidence_cycle_fks(apps, schema_editor):
    """Backfill assessment_cycle and assessment_pack FKs from JSON metadata."""
    Evidence = apps.get_model('org_context', 'EmployeeSkillEvidence')
    Cycle = apps.get_model('employee_assessment', 'AssessmentCycle')
    Pack = apps.get_model('employee_assessment', 'EmployeeAssessmentPack')

    cycle_cache = {str(c.uuid): c.pk for c in Cycle.objects.all()}
    pack_cache = {str(p.uuid): p.pk for p in Pack.objects.all()}

    batch = []
    for row in Evidence.objects.filter(source_kind='self_assessment').iterator(chunk_size=500):
        meta = row.metadata or {}
        cycle_uuid = str(meta.get('assessment_cycle_uuid', '') or '').strip()
        pack_uuid = str(meta.get('assessment_pack_uuid', '') or '').strip()
        changed = False
        if cycle_uuid and cycle_uuid in cycle_cache:
            row.assessment_cycle_id = cycle_cache[cycle_uuid]
            changed = True
        if pack_uuid and pack_uuid in pack_cache:
            row.assessment_pack_id = pack_cache[pack_uuid]
            changed = True
        if changed:
            batch.append(row)

    if batch:
        Evidence.objects.bulk_update(batch, ['assessment_cycle_id', 'assessment_pack_id'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        (
            "org_context",
            "0011_rename_org_context_workspa_6f4f0d_idx_org_context_workspa_dddf27_idx_and_more",
        ),
        ("employee_assessment", "0003_employeeassessmentpack"),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeskillevidence",
            name="assessment_cycle",
            field=models.ForeignKey(
                blank=True,
                help_text="The assessment cycle that produced this evidence (self_assessment only).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="evidence_rows",
                to="employee_assessment.assessmentcycle",
            ),
        ),
        migrations.AddField(
            model_name="employeeskillevidence",
            name="assessment_pack",
            field=models.ForeignKey(
                blank=True,
                help_text="The assessment pack that produced this evidence (self_assessment only).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="evidence_rows",
                to="employee_assessment.employeeassessmentpack",
            ),
        ),
        migrations.AddIndex(
            model_name="employeeskillevidence",
            index=models.Index(
                fields=["workspace", "source_kind", "assessment_cycle"],
                name="org_context_evidence_cycle_idx",
            ),
        ),
        migrations.RunPython(
            backfill_evidence_cycle_fks,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
