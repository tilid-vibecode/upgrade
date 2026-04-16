import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0007_intakeworkspace_operator_token'),
        ('org_context', '0017_skill_resolution_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='SkillReviewDecision',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('skill_canonical_key', models.CharField(help_text='Canonical key of the skill being reviewed.', max_length=255)),
                (
                    'action',
                    models.CharField(
                        choices=[
                            ('accepted', 'Accepted'),
                            ('rejected', 'Rejected'),
                            ('merged', 'Merged'),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    'merge_target_skill_uuid',
                    models.UUIDField(
                        blank=True,
                        help_text='Target skill UUID if action is merged.',
                        null=True,
                    ),
                ),
                ('note', models.TextField(blank=True, default='')),
                ('reviewed_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed_by', models.CharField(blank=True, default='', max_length=255)),
                (
                    'employee',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='skill_review_decisions',
                        to='org_context.employee',
                    ),
                ),
                (
                    'workspace',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='skill_review_decisions',
                        to='company_intake.intakeworkspace',
                    ),
                ),
            ],
            options={
                'indexes': [
                    models.Index(fields=['workspace', 'employee'], name='org_context_workspa_fcffaf_idx'),
                    models.Index(fields=['workspace', 'skill_canonical_key'], name='org_context_workspa_063a92_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('workspace', 'employee', 'skill_canonical_key'),
                        name='uq_skill_review_decision_per_employee',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='employeeskillevidence',
            name='is_operator_confirmed',
            field=models.BooleanField(
                default=False,
                help_text='True after an operator explicitly accepted this evidence row.',
            ),
        ),
        migrations.AddField(
            model_name='employeeskillevidence',
            name='operator_action',
            field=models.CharField(
                blank=True,
                choices=[
                    ('', ''),
                    ('accepted', 'Accepted'),
                    ('rejected', 'Rejected'),
                    ('merged', 'Merged'),
                ],
                default='',
                help_text='Last operator action on this evidence row.',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='employeeskillevidence',
            name='operator_note',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Free-text note from operator review.',
            ),
        ),
    ]
