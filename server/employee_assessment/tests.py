from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase

from company_intake.models import IntakeWorkspace
from development_plans.services import _generate_individual_plan_payload
from employee_assessment.models import (
    AssessmentCycle,
    AssessmentPackStatus,
    AssessmentStatus,
    EmployeeAssessmentPack,
)
from employee_assessment.services import (
    build_pack_response,
    generate_assessment_cycle,
    get_assessment_status,
    get_current_cycle,
    get_latest_submitted_pack,
    get_pack_by_uuid,
    list_cycle_packs,
    open_assessment_pack,
    submit_assessment_pack_response,
)
from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
from org_context.models import (
    Employee,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    PlanningContext,
    RoleProfile,
    RoleSkillRequirement,
    Skill,
    SkillResolutionOverride,
)
from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun


class Stage7AssessmentTests(TestCase):
    def _create_published_blueprint(self, workspace) -> SkillBlueprintRun:
        return SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            role_candidates=[],
            required_skill_set=[],
            assessment_plan={
                'per_employee_question_count': 8,
                'question_themes': ['Execution confidence', 'Evidence gaps'],
                'global_notes': 'Keep it short and practical.',
            },
        )

    def _create_role_with_requirements(self, workspace, blueprint, *, skill_rows: list[dict]):
        role = RoleProfile.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            name='Backend Engineer',
            family='backend_engineer',
            seniority='senior',
        )
        requirements = []
        for row in skill_rows:
            skill = Skill.objects.create(
                workspace=workspace,
                canonical_key=row['canonical_key'],
                display_name_en=row['display_name_en'],
                display_name_ru=row.get('display_name_ru', ''),
                source='role_library_seed',
                metadata={},
            )
            requirements.append(
                RoleSkillRequirement.objects.create(
                    workspace=workspace,
                    role_profile=role,
                    skill=skill,
                    target_level=row.get('target_level', 4),
                    priority=row.get('priority', 4),
                    is_required=True,
                    source_kind='blueprint',
                    metadata={
                        'criticality': row.get('criticality', 'high'),
                        'requirement_type': row.get('requirement_type', 'core'),
                        'supported_initiatives': row.get('supported_initiatives', ['launch']),
                    },
                )
            )
        return role, requirements

    def _mock_wording(self, pack_plan):
        return {
            'introduction': 'This short pack helps us close the most important evidence gaps.',
            'hidden_skills_prompt': {
                'question_id': 'hidden-skills',
                'prompt': 'Which skills or tools do you use that may not be obvious from your recent docs?',
            },
            'aspiration_prompt': {
                'question_id': 'aspiration',
                'prompt': 'Which adjacent roles or responsibilities would you like to grow into?',
            },
            'targeted_questions': [
                {
                    'question_id': question['question_id'],
                    'prompt': f'How confidently can you apply {question["skill_name_en"]} in normal work?',
                    'optional_example_prompt': f'Share one recent example that shows {question["skill_name_en"]}.',
                }
                for question in pack_plan.get('targeted_questions', [])
            ],
            'closing_prompt': 'Short examples are enough.',
        }

    def test_generate_assessment_cycle_targets_gaps_and_skips_strong_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='Stage 7 Co', slug='stage-7-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {
                    'canonical_key': 'python',
                    'display_name_en': 'Python',
                    'target_level': 4,
                    'priority': 5,
                },
                {
                    'canonical_key': 'system-design',
                    'display_name_en': 'System Design',
                    'target_level': 4,
                    'priority': 5,
                },
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.91,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[0].skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.8,
            weight=0.7,
            evidence_text='Strong Python ownership in production services.',
            metadata={},
        )

        with patch(
            'employee_assessment.services.retrieve_employee_cv_evidence_sync',
            side_effect=[
                [
                    {
                        'score': 0.42,
                        'doc_type': 'cv_skill_evidence',
                        'section_heading': 'Python',
                        'chunk_text': 'Strong Python ownership in production services.',
                    },
                    {
                        'score': 0.39,
                        'doc_type': 'cv_role_history',
                        'section_heading': 'Backend Engineer',
                        'chunk_text': 'Built Python APIs for billing.',
                    },
                ],
                [],
            ],
        ), patch(
            'employee_assessment.services._phrase_assessment_pack_with_llm',
            new=AsyncMock(side_effect=self._mock_wording),
        ):
            cycle = async_to_sync(generate_assessment_cycle)(workspace, title='Cycle 1')

        cycle.refresh_from_db()
        pack = cycle.packs.select_related('employee').get(employee=employee)
        targeted_keys = [item['skill_key'] for item in pack.questionnaire_payload['targeted_questions']]

        self.assertEqual(cycle.status, AssessmentStatus.GENERATED)
        self.assertEqual(pack.status, AssessmentPackStatus.GENERATED)
        self.assertEqual(targeted_keys, ['system-design'])
        self.assertEqual(pack.questionnaire_payload['hidden_skills_prompt']['question_id'], 'hidden-skills')
        self.assertEqual(pack.questionnaire_payload['aspiration_prompt']['question_id'], 'aspiration')
        self.assertLessEqual(len(pack.questionnaire_payload['targeted_questions']), 10)

    def test_generate_assessment_cycle_rejects_empty_context_cohort(self):
        workspace = IntakeWorkspace.objects.create(name='Context Assessments Co', slug='context-assessments-co')
        planning_context = PlanningContext.objects.create(
            workspace=workspace,
            name='Scenario',
            slug='scenario',
            kind=PlanningContext.Kind.ORG,
        )
        Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            planning_context=planning_context,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            role_candidates=[],
            required_skill_set=[],
        )

        with self.assertRaisesMessage(ValueError, 'at least one matched employee in scope'):
            async_to_sync(generate_assessment_cycle)(workspace, planning_context=planning_context)

    def test_get_pack_by_uuid_is_read_only_and_open_is_explicit(self):
        workspace = IntakeWorkspace.objects.create(name='Open Pack Co', slug='open-pack-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        role, _requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[{'canonical_key': 'python', 'display_name_en': 'Python'}],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.91,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )

        with patch(
            'employee_assessment.services.retrieve_employee_cv_evidence_sync',
            return_value=[],
        ), patch(
            'employee_assessment.services._phrase_assessment_pack_with_llm',
            new=AsyncMock(side_effect=self._mock_wording),
        ):
            cycle = async_to_sync(generate_assessment_cycle)(workspace)

        pack = cycle.packs.get(employee=employee)
        fetched_pack = async_to_sync(get_pack_by_uuid)(str(pack.uuid), mark_opened=False)
        fetched_pack.refresh_from_db()
        pack.refresh_from_db()
        self.assertEqual(fetched_pack.status, AssessmentPackStatus.GENERATED)
        self.assertIsNone(fetched_pack.opened_at)

        opened_pack = async_to_sync(open_assessment_pack)(pack)
        opened_pack.refresh_from_db()
        cycle.refresh_from_db()

        self.assertEqual(opened_pack.status, AssessmentPackStatus.OPENED)
        self.assertIsNotNone(opened_pack.opened_at)
        self.assertEqual(cycle.status, AssessmentStatus.RUNNING)

    def test_partial_save_keeps_pack_open_and_does_not_create_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='Partial Co', slug='partial-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Pack',
            status=AssessmentPackStatus.GENERATED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
                'targeted_questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
            },
        )

        updated_pack = async_to_sync(submit_assessment_pack_response)(
            pack,
            {
                'final_submit': False,
                'targeted_answers': [
                    {
                        'question_id': 'skill:python',
                        'self_rated_level': 3,
                        'answer_confidence': 0.7,
                        'example_text': 'Built APIs with Python.',
                    }
                ],
            },
        )

        updated_pack.refresh_from_db()
        self.assertEqual(updated_pack.status, AssessmentPackStatus.OPENED)
        self.assertEqual(EmployeeSkillEvidence.objects.filter(workspace=workspace).count(), 0)

    def test_build_pack_response_handles_cycle_listing_without_async_fk_lookup(self):
        workspace = IntakeWorkspace.objects.create(name='Pack Response Co', slug='pack-response-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Pack',
            status=AssessmentPackStatus.GENERATED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={},
        )

        pack = async_to_sync(list_cycle_packs)(cycle)[0]
        payload = async_to_sync(build_pack_response)(pack)

        self.assertEqual(payload['cycle_uuid'], cycle.uuid)
        self.assertEqual(payload['employee_uuid'], employee.uuid)
        self.assertEqual(payload['employee_name'], 'Alice Doe')

    def test_final_submission_persists_normalized_evidence_and_indexes_pack(self):
        workspace = IntakeWorkspace.objects.create(name='Submit Co', slug='submit-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        python_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            source='role_library_seed',
            metadata={},
        )
        SkillResolutionOverride.objects.create(
            workspace=workspace,
            raw_term='Roadmapping',
            normalized_term='roadmapping',
            canonical_key='roadmapping',
            display_name_en='Roadmapping',
            display_name_ru='',
            aliases=[],
            status='approved',
            source='test_override',
            metadata={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Pack',
            status=AssessmentPackStatus.OPENED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [
                    {
                        'question_id': 'hidden-skills',
                        'question_type': 'hidden_skills',
                    },
                    {
                        'question_id': 'aspiration',
                        'question_type': 'aspiration',
                    },
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'skill_name_ru': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    },
                ],
                'targeted_questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'skill_name_ru': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
            },
        )

        with patch(
            'employee_assessment.services.index_employee_assessment_pack_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 3},
        ):
            updated_pack = async_to_sync(submit_assessment_pack_response)(
                pack,
                {
                    'final_submit': True,
                    'targeted_answers': [
                        {
                            'question_id': 'skill:python',
                            'self_rated_level': 3,
                            'answer_confidence': 0.8,
                            'example_text': 'Built billing APIs with Python.',
                        }
                    ],
                    'hidden_skills': [
                        {
                            'skill_name_en': 'Roadmapping',
                            'self_rated_level': 2,
                            'answer_confidence': 0.7,
                            'example_text': 'Helped plan release scope.',
                        }
                    ],
                    'aspiration': {
                        'target_role_family': 'platform_sre_engineer',
                        'notes': 'Interested in reliability work.',
                        'interest_signal': 'high',
                    },
                    'confidence_statement': 'Answers reflect recent work.',
                },
            )

        updated_pack.refresh_from_db()
        cycle.refresh_from_db()
        evidence_rows = list(
            EmployeeSkillEvidence.objects.filter(workspace=workspace, employee=employee).order_by('skill__display_name_en')
        )

        self.assertEqual(updated_pack.status, AssessmentPackStatus.SUBMITTED)
        self.assertIsNotNone(updated_pack.submitted_at)
        self.assertEqual(cycle.status, AssessmentStatus.COMPLETED)
        self.assertEqual(len(evidence_rows), 2)
        evidence_by_skill = {
            row.skill.display_name_en: row
            for row in evidence_rows
        }
        self.assertEqual(evidence_by_skill['Python'].source_kind, 'self_assessment')
        self.assertEqual(float(evidence_by_skill['Python'].weight), 0.44)
        self.assertEqual(float(evidence_by_skill['Roadmapping'].weight), 0.39)
        self.assertEqual(
            evidence_by_skill['Roadmapping'].metadata['assessment_pack_uuid'],
            str(updated_pack.uuid),
        )
        self.assertIn('vector_index', updated_pack.fused_summary)
        self.assertEqual(updated_pack.fused_summary['submitted_skill_rows'][0]['skill_key'], python_skill.canonical_key)

    def test_final_submission_keeps_unresolved_hidden_skill_in_pending_review_bucket(self):
        workspace = IntakeWorkspace.objects.create(name='Pending Hidden Co', slug='pending-hidden-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        python_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            source='role_library_seed',
            metadata={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Pack',
            status=AssessmentPackStatus.OPENED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [
                    {
                        'question_id': 'hidden-skills',
                        'question_type': 'hidden_skills',
                    },
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'skill_name_ru': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    },
                ],
                'targeted_questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'skill_name_ru': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
            },
        )

        with patch(
            'employee_assessment.services.index_employee_assessment_pack_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1},
        ):
            updated_pack = async_to_sync(submit_assessment_pack_response)(
                pack,
                {
                    'final_submit': True,
                    'targeted_answers': [
                        {
                            'question_id': 'skill:python',
                            'self_rated_level': 3,
                            'answer_confidence': 0.8,
                            'example_text': 'Built billing APIs with Python.',
                        }
                    ],
                    'hidden_skills': [
                        {
                            'skill_name_en': 'Edge Data Mesh',
                            'self_rated_level': 2,
                            'answer_confidence': 0.7,
                            'example_text': 'Working with mesh concepts.',
                        }
                    ],
                    'aspiration': {},
                    'confidence_statement': 'Answers reflect recent work.',
                },
            )

        updated_pack.refresh_from_db()
        evidence_rows = list(
            EmployeeSkillEvidence.objects.filter(workspace=workspace, employee=employee)
        )

        self.assertEqual(len(evidence_rows), 1)
        self.assertEqual(evidence_rows[0].skill, python_skill)
        self.assertEqual(updated_pack.response_payload['pending_hidden_skills'][0]['skill_name_en'], 'Edge Data Mesh')

    def test_final_submission_replaces_prior_self_assessment_for_overlapping_skill(self):
        workspace = IntakeWorkspace.objects.create(name='Replace Co', slug='replace-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        python_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            source='role_library_seed',
            metadata={},
        )
        old_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Old Cycle',
            status=AssessmentStatus.COMPLETED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        old_pack = EmployeeAssessmentPack.objects.create(
            cycle=old_cycle,
            employee=employee,
            title='Old Pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
            submitted_at=old_cycle.updated_at,
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=python_skill,
            source_kind='self_assessment',
            current_level=2,
            confidence=0.5,
            weight=0.28,
            evidence_text='Old self-assessment.',
            metadata={
                'assessment_cycle_uuid': str(old_cycle.uuid),
                'assessment_pack_uuid': str(old_pack.uuid),
                'question_id': 'skill:python',
            },
        )
        new_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='New Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        new_pack = EmployeeAssessmentPack.objects.create(
            cycle=new_cycle,
            employee=employee,
            title='New Pack',
            status=AssessmentPackStatus.OPENED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
                'targeted_questions': [
                    {
                        'question_id': 'skill:python',
                        'question_type': 'targeted_skill',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'target_level': 4,
                        'why_asked': 'Gap exists.',
                    }
                ],
            },
        )

        with patch(
            'employee_assessment.services.index_employee_assessment_pack_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1},
        ):
            async_to_sync(submit_assessment_pack_response)(
                new_pack,
                {
                    'final_submit': True,
                    'targeted_answers': [
                        {
                            'question_id': 'skill:python',
                            'self_rated_level': 4,
                            'answer_confidence': 0.9,
                            'example_text': 'Current Python ownership.',
                        }
                    ],
                },
            )

        evidence_rows = list(
            EmployeeSkillEvidence.objects.filter(
                workspace=workspace,
                employee=employee,
                source_kind='self_assessment',
                skill=python_skill,
            ).order_by('-updated_at')
        )
        # Old-cycle evidence is preserved; new-cycle creates its own row.
        self.assertEqual(len(evidence_rows), 2)
        new_row = next(r for r in evidence_rows if r.metadata.get('assessment_pack_uuid') == str(new_pack.uuid))
        old_row = next(r for r in evidence_rows if r.metadata.get('assessment_pack_uuid') == str(old_pack.uuid))
        self.assertEqual(new_row.assessment_cycle_id, new_cycle.pk)
        self.assertEqual(new_row.assessment_pack_id, new_pack.pk)
        self.assertIsNone(old_row.assessment_cycle_id)  # Legacy row without FK

    def test_submit_rejects_unknown_question_id(self):
        workspace = IntakeWorkspace.objects.create(name='Reject Co', slug='reject-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Pack',
            status=AssessmentPackStatus.GENERATED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [],
                'targeted_questions': [],
            },
        )

        with self.assertRaisesMessage(ValueError, 'Unknown targeted question_id=skill:python.'):
            async_to_sync(submit_assessment_pack_response)(
                pack,
                {
                    'final_submit': True,
                    'targeted_answers': [
                        {
                            'question_id': 'skill:python',
                            'self_rated_level': 3,
                            'answer_confidence': 0.8,
                        }
                    ],
                },
            )

    def test_superseded_pack_cannot_accept_submission(self):
        workspace = IntakeWorkspace.objects.create(name='Superseded Pack Co', slug='superseded-pack-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Old Cycle',
            status=AssessmentStatus.SUPERSEDED,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Old Pack',
            status=AssessmentPackStatus.SUPERSEDED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
        )

        with self.assertRaisesMessage(
            ValueError,
            'This assessment pack belongs to a superseded cycle and can no longer accept responses.',
        ):
            async_to_sync(submit_assessment_pack_response)(
                pack,
                {
                    'final_submit': True,
                    'targeted_answers': [],
                },
            )

        with self.assertRaisesMessage(
            ValueError,
            'This assessment pack belongs to a superseded cycle and can no longer be opened.',
        ):
            async_to_sync(open_assessment_pack)(pack)

    def test_submitted_pack_cannot_be_overwritten(self):
        workspace = IntakeWorkspace.objects.create(name='Submitted Pack Co', slug='submitted-pack-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Current Cycle',
            status=AssessmentStatus.RUNNING,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Submitted Pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={
                'schema_version': 'stage7-v1',
                'questions': [],
                'targeted_questions': [],
            },
            response_payload={'final_submit': True},
            fused_summary={'submitted_skill_rows': []},
            submitted_at=cycle.updated_at,
        )

        with self.assertRaisesMessage(
            ValueError,
            'This assessment pack has already been finalized and can no longer accept changes.',
        ):
            async_to_sync(submit_assessment_pack_response)(
                pack,
                {
                    'final_submit': True,
                    'targeted_answers': [],
                },
            )

    def test_regenerate_supersedes_previous_nonterminal_cycle(self):
        workspace = IntakeWorkspace.objects.create(name='Supersede Co', slug='supersede-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        role, _requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[{'canonical_key': 'python', 'display_name_en': 'Python'}],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.91,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )

        with patch(
            'employee_assessment.services.retrieve_employee_cv_evidence_sync',
            return_value=[],
        ), patch(
            'employee_assessment.services._phrase_assessment_pack_with_llm',
            new=AsyncMock(side_effect=self._mock_wording),
        ):
            first_cycle = async_to_sync(generate_assessment_cycle)(workspace, title='Cycle 1')
            second_cycle = async_to_sync(generate_assessment_cycle)(workspace, title='Cycle 2')

        first_cycle.refresh_from_db()
        second_cycle.refresh_from_db()
        self.assertEqual(first_cycle.status, AssessmentStatus.SUPERSEDED)
        self.assertEqual(second_cycle.status, AssessmentStatus.GENERATED)
        self.assertEqual(first_cycle.packs.first().status, AssessmentPackStatus.SUPERSEDED)

    def test_current_cycle_prefers_last_usable_cycle_over_failed_attempt(self):
        workspace = IntakeWorkspace.objects.create(name='Current Cycle Co', slug='current-cycle-co')
        usable_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Usable',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        AssessmentCycle.objects.create(
            workspace=workspace,
            title='Failed',
            status=AssessmentStatus.FAILED,
            uses_self_report=True,
            configuration={},
            result_summary={'error_message': 'boom'},
        )

        current_cycle = async_to_sync(get_current_cycle)(workspace)
        self.assertEqual(current_cycle, usable_cycle)

    def test_get_assessment_status_summarizes_current_cycle(self):
        workspace = IntakeWorkspace.objects.create(name='Status Co', slug='status-co')
        employee_one = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        employee_two = Employee.objects.create(
            workspace=workspace,
            full_name='Bob Roe',
            email='bob@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        blueprint = self._create_published_blueprint(workspace)
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.RUNNING,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee_one,
            title='Pack 1',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
            submitted_at=cycle.updated_at,
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee_two,
            title='Pack 2',
            status=AssessmentPackStatus.OPENED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
            opened_at=cycle.updated_at,
        )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            source='self_assessment',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee_one,
            skill=skill,
            source_kind='self_assessment',
            current_level=3,
            confidence=0.8,
            weight=0.44,
            evidence_text='Submitted through pack.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid), 'assessment_pack_uuid': 'pack-1'},
        )

        payload = async_to_sync(get_assessment_status)(workspace)

        self.assertEqual(payload['latest_attempt_uuid'], cycle.uuid)
        self.assertEqual(payload['latest_attempt_status'], AssessmentStatus.RUNNING)
        self.assertEqual(payload['current_cycle_uuid'], cycle.uuid)
        self.assertEqual(payload['submitted_packs'], 1)
        self.assertEqual(payload['opened_packs'], 1)
        self.assertEqual(payload['employees_with_submitted_self_assessment'], 1)
        self.assertEqual(payload['completion_rate'], 0.5)

    def test_get_assessment_status_exposes_latest_failed_attempt(self):
        workspace = IntakeWorkspace.objects.create(name='Status Failure Co', slug='status-failure-co')
        usable_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Usable',
            status=AssessmentStatus.COMPLETED,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        failed_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Failed',
            status=AssessmentStatus.FAILED,
            uses_self_report=True,
            configuration={},
            result_summary={'error_message': 'boom'},
        )

        payload = async_to_sync(get_assessment_status)(workspace)

        self.assertEqual(payload['latest_attempt_uuid'], failed_cycle.uuid)
        self.assertEqual(payload['latest_attempt_status'], AssessmentStatus.FAILED)
        self.assertEqual(payload['current_cycle_uuid'], usable_cycle.uuid)

    def test_get_latest_submitted_pack_ignores_newer_unsubmitted_pack(self):
        workspace = IntakeWorkspace.objects.create(name='Latest Pack Co', slug='latest-pack-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle_one = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle 1',
            status=AssessmentStatus.COMPLETED,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        submitted_pack = EmployeeAssessmentPack.objects.create(
            cycle=cycle_one,
            employee=employee,
            title='Submitted Pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
            response_payload={'targeted_answers': []},
            fused_summary={'submitted_skill_rows': [{'skill_name_en': 'Python'}]},
            submitted_at=cycle_one.updated_at,
        )
        cycle_two = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle 2',
            status=AssessmentStatus.GENERATED,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle_two,
            employee=employee,
            title='Generated Pack',
            status=AssessmentPackStatus.GENERATED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
        )

        latest_pack = async_to_sync(get_latest_submitted_pack)(employee)
        self.assertEqual(latest_pack, submitted_pack)

    def test_development_plan_input_uses_latest_submitted_pack(self):
        workspace = IntakeWorkspace.objects.create(name='Plan Co', slug='plan-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle_one = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle 1',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle_one,
            employee=employee,
            title='Submitted Pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
            fused_summary={'submitted_skill_rows': [{'skill_name_en': 'Python'}]},
            submitted_at=cycle_one.updated_at,
        )
        cycle_two = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle 2',
            status=AssessmentStatus.GENERATED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle_two,
            employee=employee,
            title='Generated Pack',
            status=AssessmentPackStatus.GENERATED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={'questions': [], 'targeted_questions': []},
        )
        matrix = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            title='Matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_payload={
                'employees': [],
                'team_summary': {},
                'employee_gap_summary': [],
            },
            summary_payload={},
        )

        with patch(
            'development_plans.services.call_openai_structured',
            return_value=SimpleNamespace(
                parsed={
                    'current_role_fit': 'Strong fit',
                    'adjacent_roles': [],
                    'strengths': [],
                    'priority_gaps': [],
                    'development_actions': [],
                    'roadmap_alignment': 'Aligned',
                }
            ),
        ):
            payload = async_to_sync(_generate_individual_plan_payload)(
                blueprint,
                matrix,
                employee,
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'best_fit_role': None,
                    'top_gaps': [],
                },
            )

        self.assertEqual(payload['current_role_fit'], 'Strong fit')
