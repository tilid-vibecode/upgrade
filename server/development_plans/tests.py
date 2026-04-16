from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync, sync_to_async
from django.test import TestCase
from django.utils import timezone

from company_intake.models import IntakeWorkspace
from development_plans.models import (
    DevelopmentPlanArtifact,
    DevelopmentPlanRun,
    PlanRunStatus,
    PlanScope,
)
from development_plans.services import (
    _build_individual_recommendation_payload_sync,
    _build_team_recommendation_payload_sync,
    ensure_plan_export_artifacts,
    generate_development_plans,
    get_current_individual_plan_artifact_bundle,
    get_current_individual_plan,
    get_current_plan_summary,
    get_current_team_plan_artifact_bundle,
    get_current_team_actions,
    get_current_team_plan,
    get_latest_team_plan_artifact_bundle,
    get_latest_individual_plan,
    get_latest_plan_summary,
    get_latest_team_plan,
    list_latest_workspace_plan_artifacts,
    list_latest_individual_plans,
    list_current_individual_plans,
    list_workspace_plan_artifacts,
)
from employee_assessment.models import (
    AssessmentCycle,
    AssessmentPackStatus,
    AssessmentStatus,
    EmployeeAssessmentPack,
)
from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
from media_storage.models import MediaFile
from org_context.models import Employee, PlanningContext
from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun


class Stage9DevelopmentPlanTests(TestCase):
    def _create_blueprint(
        self,
        workspace,
        *,
        title='Published blueprint',
        is_published=True,
    ) -> SkillBlueprintRun:
        return SkillBlueprintRun.objects.create(
            workspace=workspace,
            title=title,
            status=BlueprintStatus.APPROVED,
            is_published=is_published,
            role_candidates=[],
            required_skill_set=[],
            company_context='B2B learning platform.',
            roadmap_context='Launch platform reliability and monetization initiatives.',
        )

    def _matrix_cells(self, employee, *, role_profile_uuid='role-backend'):
        return [
            {
                'cell_key': f'{employee.uuid}:python',
                'column_key': f'{role_profile_uuid}:python:4',
                'employee_uuid': str(employee.uuid),
                'employee_name': employee.full_name,
                'current_title': employee.current_title,
                'role_profile_uuid': role_profile_uuid,
                'role_name': 'Backend Engineer',
                'role_family': 'backend_engineer',
                'seniority': 'senior',
                'role_fit_score': 0.86,
                'skill_key': 'python',
                'skill_name_en': 'Python',
                'skill_name_ru': 'Python',
                'target_level': 4,
                'current_level': 3.8,
                'gap': 0.2,
                'confidence': 0.84,
                'priority': 5,
                'supported_initiatives': ['launch'],
                'evidence_source_mix': [{'source_kind': 'employee_cv'}],
                'contributing_evidence_row_uuids': [],
                'incompleteness_flags': [],
                'advisory_flags': [],
                'provenance_snippets': [
                    {'retrieval_lane': 'postgres', 'excerpt': 'Strong Python delivery evidence.'}
                ],
                'explanation_summary': 'Strong Python delivery evidence.',
            },
            {
                'cell_key': f'{employee.uuid}:system-design',
                'column_key': f'{role_profile_uuid}:system-design:4',
                'employee_uuid': str(employee.uuid),
                'employee_name': employee.full_name,
                'current_title': employee.current_title,
                'role_profile_uuid': role_profile_uuid,
                'role_name': 'Backend Engineer',
                'role_family': 'backend_engineer',
                'seniority': 'senior',
                'role_fit_score': 0.86,
                'skill_key': 'system-design',
                'skill_name_en': 'System Design',
                'skill_name_ru': 'System Design',
                'target_level': 4,
                'current_level': 2.8,
                'gap': 1.2,
                'confidence': 0.63,
                'priority': 5,
                'supported_initiatives': ['launch'],
                'evidence_source_mix': [{'source_kind': 'self_assessment'}],
                'contributing_evidence_row_uuids': [],
                'incompleteness_flags': [],
                'advisory_flags': [],
                'provenance_snippets': [
                    {'retrieval_lane': 'postgres', 'excerpt': 'Can design service boundaries with support.'}
                ],
                'explanation_summary': 'System design is the main growth area.',
            },
        ]

    def _create_matrix(
        self,
        workspace,
        blueprint,
        *,
        title='Matrix',
        employees=None,
        matrix_cells=None,
        risk_payload=None,
        assessment_cycle_uuids_used=None,
        updated_suffix='',
    ) -> EvidenceMatrixRun:
        employees = employees or []
        matrix_cells = matrix_cells or []
        risk_payload = risk_payload or {}
        return EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title=f'{title}{updated_suffix}',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            snapshot_key=f'matrix:{blueprint.uuid}:{updated_suffix}',
            matrix_version='stage8-v1',
            input_snapshot={
                'blueprint_run_uuid': str(blueprint.uuid),
                'assessment_cycle_uuids_used': list(assessment_cycle_uuids_used or []),
            },
            summary_payload={'team_summary': 'Team summary'},
            heatmap_payload={'skill_columns': []},
            risk_payload=risk_payload,
            incompleteness_payload={},
            matrix_payload={
                'employees': employees,
                'matrix_cells': matrix_cells,
                'team_summary': {'employee_count': len(employees)},
                'employee_gap_summary': [],
            },
        )

    def _team_matrix_payload(self, alice, bob):
        employees = [
            {
                'employee_uuid': str(alice.uuid),
                'full_name': alice.full_name,
                'current_title': alice.current_title,
                'best_fit_role': {
                    'role_name': 'Backend Engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'fit_score': 0.86,
                },
                'adjacent_roles': [
                    {
                        'role_name': 'Platform SRE',
                        'role_family': 'platform_sre_engineer',
                        'seniority': 'senior',
                        'fit_score': 0.79,
                    }
                ],
                'top_gaps': [
                    {
                        'skill_key': 'system-design',
                        'skill_name_en': 'System Design',
                        'supported_initiatives': ['launch'],
                    }
                ],
                'total_gap_score': 3.2,
                'average_confidence': 0.71,
            },
            {
                'employee_uuid': str(bob.uuid),
                'full_name': bob.full_name,
                'current_title': bob.current_title,
                'best_fit_role': {
                    'role_name': 'Backend Engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'fit_score': 0.74,
                },
                'adjacent_roles': [],
                'top_gaps': [],
                'total_gap_score': 1.4,
                'average_confidence': 0.62,
            },
        ]
        matrix_cells = self._matrix_cells(alice) + self._matrix_cells(bob)
        risk_payload = {
            'top_priority_gaps': [
                {
                    'column_key': 'role-backend:system-design:4',
                    'role_name': 'Backend Engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'skill_key': 'system-design',
                    'skill_name_en': 'System Design',
                    'target_level': 4,
                    'average_gap': 1.2,
                    'average_confidence': 0.67,
                    'max_priority': 5,
                    'priority_gap_score': 6.0,
                    'employees_meeting_target': 0,
                    'employees_below_target': 2,
                    'incomplete_count': 0,
                }
            ],
            'concentration_risks': [
                {
                    'role_name': 'Backend Engineer',
                    'skill_key': 'python',
                    'skill_name_en': 'Python',
                    'ready_employee_count': 1,
                    'priority': 5,
                }
            ],
            'near_fit_candidates': [
                {
                    'employee_uuid': str(alice.uuid),
                    'full_name': alice.full_name,
                    'role_name': 'Backend Engineer',
                    'gap': 0.7,
                    'confidence': 0.7,
                    'top_gaps': [
                        {
                            'column_key': 'role-backend:system-design:4',
                            'skill_key': 'system-design',
                        }
                    ],
                }
            ],
            'uncovered_roles': [
                {
                    'role_profile_uuid': 'role-sre',
                    'role_name': 'Platform SRE',
                    'seniority': 'senior',
                    'matched_employee_count': 0,
                }
            ],
            'employees_with_insufficient_evidence': [],
        }
        return employees, matrix_cells, risk_payload

    def _narrative_side_effect(self, **kwargs):
        schema_name = kwargs.get('schema_name')
        if schema_name == 'team_development_plan_stage9':
            return SimpleNamespace(
                parsed={
                    'executive_summary': 'Focus on closing coverage risk first.',
                    'roadmap_priority_note': 'Reliability and monetization gaps are time-sensitive.',
                    'priority_actions': [],
                    'hiring_recommendations': ['Hire for uncovered Platform SRE capability.'],
                    'development_focus': ['Develop system design coverage internally.'],
                    'single_points_of_failure': ['Python depth is concentrated in too few people.'],
                }
            )
        if schema_name == 'individual_development_plan_stage9':
            return SimpleNamespace(
                parsed={
                    'current_role_fit': 'Strong fit with visible growth headroom.',
                    'adjacent_roles': ['Platform SRE (senior)'],
                    'strengths': ['Python delivery is a reliable strength.'],
                    'priority_gaps': ['System Design remains the highest-priority gap.'],
                    'development_actions': [],
                    'roadmap_alignment': 'The plan aligns with current launch priorities.',
                    'mobility_note': 'There is a credible adjacent-role path if interest remains high.',
                }
            )
        raise AssertionError(f'Unexpected schema: {schema_name}')

    async def _fake_store_generated_artifact(
        self,
        *,
        scope: str,
        filename: str,
        content,
        content_type: str,
        description: str,
        metadata=None,
        prototype_workspace=None,
        **_ignored,
    ):
        media_file = await MediaFile.objects.create_pending(
            organization=None,
            uploaded_by=None,
            original_filename=filename,
            content_type=content_type,
            file_size=len(content.encode('utf-8') if isinstance(content, str) else content),
            file_category='text',
            persistent_key=f'test/{uuid4()}/{filename}',
            processing_key=f'test-processing/{uuid4()}/{filename}',
            prototype_workspace=prototype_workspace,
        )
        media_file.processing_metadata = {
            **(metadata or {}),
            'scope': scope,
            'description': description,
            'rendered_content': content if isinstance(content, str) else content.decode('utf-8'),
        }
        await sync_to_async(media_file.save)(update_fields=['processing_metadata', 'updated_at'])
        await MediaFile.objects.mark_uploaded(media_file)
        await MediaFile.objects.mark_ready(
            media_file,
            processing_description=description,
            processing_metadata=media_file.processing_metadata,
        )
        return media_file

    async def _fake_signed_url(self, media_file, **kwargs):
        return f'https://downloads.test/{media_file.original_filename}'

    def test_team_recommendation_payload_classifies_actions_and_attaches_context(self):
        workspace = IntakeWorkspace.objects.create(name='Plan Co', slug='plan-co')
        blueprint = self._create_blueprint(workspace)
        alice = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Analyst',
            metadata={},
        )
        bob = Employee.objects.create(
            workspace=workspace,
            full_name='Bob Doe',
            email='bob@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        employees, matrix_cells, risk_payload = self._team_matrix_payload(alice, bob)
        matrix = self._create_matrix(
            workspace,
            blueprint,
            employees=employees,
            matrix_cells=matrix_cells,
            risk_payload=risk_payload,
        )

        with patch(
            'development_plans.services.retrieve_workspace_evidence_sync',
            return_value=[
                {
                    'source_kind': 'roadmap',
                    'source_title': 'Roadmap',
                    'section_heading': 'Launch',
                    'score': 0.42,
                    'chunk_text': 'Reliability and system design are key to launch readiness.',
                }
            ],
        ), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[
                {
                    'retrieval_lane': 'cv',
                    'doc_type': 'cv_role_history',
                    'score': 0.38,
                    'section_heading': 'Backend systems',
                    'chunk_text': 'Built backend systems and reliability tooling.',
                }
            ],
        ):
            payload = _build_team_recommendation_payload_sync(workspace.pk, blueprint.pk, matrix.pk)

        action_types = [item['action_type'] for item in payload['priority_actions']]
        self.assertEqual(payload['plan_version'], 'stage9-v1')
        self.assertIn('hire', action_types)
        self.assertIn('develop', action_types)
        self.assertIn('move', action_types)
        self.assertIn('de-risk', action_types)
        self.assertEqual(payload['action_counts']['hire'], 1)
        self.assertTrue(payload['priority_actions'][0]['supporting_context']['roadmap_context'])
        develop_action = next(item for item in payload['priority_actions'] if item['action_type'] == 'develop')
        self.assertEqual(develop_action['linked_initiatives'], ['launch'])
        self.assertTrue(develop_action['supporting_context']['matrix_provenance'])

    def test_individual_recommendation_payload_uses_latest_self_report_and_placeholder_resources(self):
        workspace = IntakeWorkspace.objects.create(name='Individual Plan Co', slug='individual-plan-co')
        blueprint = self._create_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        employee_payload = {
            'employee_uuid': str(employee.uuid),
            'full_name': employee.full_name,
            'current_title': employee.current_title,
            'best_fit_role': {
                'role_name': 'Backend Engineer',
                'role_family': 'backend_engineer',
                'seniority': 'senior',
                'fit_score': 0.88,
            },
            'adjacent_roles': [
                {
                    'role_name': 'Platform SRE',
                    'role_family': 'platform_sre_engineer',
                    'seniority': 'senior',
                    'fit_score': 0.82,
                }
            ],
            'top_gaps': [{'skill_key': 'system-design'}],
            'total_gap_score': 3.1,
            'average_confidence': 0.72,
        }
        matrix = self._create_matrix(
            workspace,
            blueprint,
            employees=[employee_payload],
            matrix_cells=self._matrix_cells(employee),
            risk_payload={},
            assessment_cycle_uuids_used=[],
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Submitted pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={},
            fused_summary={
                'aspiration': {
                    'target_role_family': 'platform_sre_engineer',
                    'interest_signal': 'high',
                    'notes': 'Interested in reliability work.',
                }
            },
        )
        later_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Later cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=later_cycle,
            employee=employee,
            title='Later submitted pack',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_version='stage7-v1',
            questionnaire_payload={},
            fused_summary={
                'aspiration': {
                    'target_role_family': 'qa_engineer',
                    'interest_signal': 'low',
                    'notes': 'Different cycle and should not be used.',
                }
            },
        )
        matrix.input_snapshot = {
            **matrix.input_snapshot,
            'assessment_cycle_uuids_used': [str(cycle.uuid)],
        }
        matrix.save(update_fields=['input_snapshot', 'updated_at'])

        with patch(
            'development_plans.services.retrieve_workspace_evidence_sync',
            return_value=[
                {
                    'source_kind': 'roadmap',
                    'source_title': 'Roadmap',
                    'section_heading': 'Reliability',
                    'score': 0.4,
                    'chunk_text': 'Reliability work is important for the next launch cycle.',
                }
            ],
        ), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[
                {
                    'retrieval_lane': 'self_assessment',
                    'doc_type': 'self_assessment_example',
                    'score': 0.39,
                    'section_heading': 'System Design',
                    'chunk_text': 'Can design service boundaries with support.',
                }
            ],
        ) as fused_retrieval:
            payload = _build_individual_recommendation_payload_sync(
                workspace.pk,
                blueprint.pk,
                matrix.pk,
                employee.pk,
                employee_payload,
            )

        self.assertEqual(payload['current_role_goal'], 'adjacent_role_growth')
        self.assertEqual(payload['mobility_potential'], 'high')
        self.assertIn('Aspiration: platform_sre_engineer', payload['adjacent_roles'])
        self.assertTrue(payload['development_actions'])
        first_action = payload['development_actions'][0]
        self.assertEqual(first_action['action_type'], 'stretch_assignment')
        self.assertTrue(first_action['course_placeholder'].startswith('Placeholder resource:'))
        self.assertEqual(first_action['placeholder_resource_type'], 'stretch')
        self.assertTrue(first_action['supporting_context']['employee_evidence'])
        self.assertEqual(
            fused_retrieval.call_args.kwargs['cycle_uuids'],
            [str(cycle.uuid)],
        )

    def test_generate_development_plans_persists_lineage_and_current_selectors(self):
        workspace = IntakeWorkspace.objects.create(name='Generation Co', slug='generation-co')
        old_blueprint = self._create_blueprint(workspace, title='Old blueprint', is_published=False)
        current_blueprint = self._create_blueprint(workspace, title='Current blueprint', is_published=True)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Analyst',
            metadata={},
        )
        employee_payload = {
            'employee_uuid': str(employee.uuid),
            'full_name': employee.full_name,
            'current_title': employee.current_title,
            'best_fit_role': {
                'role_name': 'Backend Engineer',
                'role_family': 'backend_engineer',
                'seniority': 'senior',
                'fit_score': 0.86,
            },
            'adjacent_roles': [
                {
                    'role_name': 'Platform SRE',
                    'role_family': 'platform_sre_engineer',
                    'seniority': 'senior',
                    'fit_score': 0.79,
                }
            ],
            'top_gaps': [{'skill_key': 'system-design', 'supported_initiatives': ['launch']}],
            'total_gap_score': 3.2,
            'average_confidence': 0.71,
        }
        old_matrix = self._create_matrix(
            workspace,
            old_blueprint,
            title='Old Matrix',
            employees=[employee_payload],
            matrix_cells=self._matrix_cells(employee, role_profile_uuid='old-role'),
            risk_payload={'top_priority_gaps': [], 'concentration_risks': [], 'near_fit_candidates': [], 'uncovered_roles': []},
            updated_suffix='old',
        )
        current_matrix = self._create_matrix(
            workspace,
            current_blueprint,
            title='Current Matrix',
            employees=[employee_payload],
            matrix_cells=self._matrix_cells(employee, role_profile_uuid='current-role'),
            risk_payload={
                'top_priority_gaps': [
                    {
                        'column_key': 'current-role:system-design:4',
                        'role_name': 'Backend Engineer',
                        'role_family': 'backend_engineer',
                        'seniority': 'senior',
                        'skill_key': 'system-design',
                        'skill_name_en': 'System Design',
                        'target_level': 4,
                        'average_gap': 1.2,
                        'average_confidence': 0.67,
                        'max_priority': 5,
                        'priority_gap_score': 6.0,
                        'employees_meeting_target': 0,
                        'employees_below_target': 1,
                        'incomplete_count': 0,
                    }
                ],
                'concentration_risks': [],
                'near_fit_candidates': [],
                'uncovered_roles': [],
            },
            updated_suffix='current',
        )

        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=old_blueprint,
            matrix_run=old_matrix,
            title='Old team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            plan_version='stage9-v1',
            input_snapshot={'blueprint_run_uuid': str(old_blueprint.uuid)},
            recommendation_payload={'action_counts': {'hire': 1}},
            final_report_key='generated/old-team.json',
            summary={},
            plan_payload={'priority_actions': []},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=old_blueprint,
            matrix_run=old_matrix,
            title='Old individual plan',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            plan_version='stage9-v1',
            input_snapshot={'blueprint_run_uuid': str(old_blueprint.uuid)},
            recommendation_payload={},
            final_report_key='generated/old-individual.json',
            summary={},
            plan_payload={'development_actions': []},
        )

        artifact_counter = {'value': 0}

        def _artifact_stub(**kwargs):
            artifact_counter['value'] += 1
            return {
                'media_file_uuid': f'media-{artifact_counter["value"]}',
                'persistent_key': f'generated/{kwargs["filename"]}',
            }

        with patch(
            'development_plans.services.retrieve_workspace_evidence_sync',
            return_value=[],
        ), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'development_plans.services._upload_generated_plan_artifact',
            side_effect=_artifact_stub,
        ), patch(
            'development_plans.services.call_openai_structured',
            side_effect=self._narrative_side_effect,
        ):
            result = async_to_sync(generate_development_plans)(workspace, team_title='Stage 9 plan')

        team_run = result['team_plan']
        team_run.refresh_from_db()
        individual_runs = result['individual_plans']
        current_team = async_to_sync(get_current_team_plan)(workspace)
        latest_team = async_to_sync(get_latest_team_plan)(workspace)
        current_individual = async_to_sync(get_current_individual_plan)(workspace, str(employee.uuid))
        current_individuals = async_to_sync(list_current_individual_plans)(workspace)
        summary = async_to_sync(get_current_plan_summary)(workspace)
        latest_summary = async_to_sync(get_latest_plan_summary)(workspace)
        actions = async_to_sync(get_current_team_actions)(workspace)

        self.assertEqual(team_run.blueprint_run, current_blueprint)
        self.assertEqual(team_run.matrix_run, current_matrix)
        self.assertEqual(team_run.status, PlanRunStatus.COMPLETED)
        self.assertTrue(team_run.is_current)
        self.assertEqual(team_run.plan_version, 'stage9-v1')
        self.assertEqual(team_run.input_snapshot['matrix_run_uuid'], str(current_matrix.uuid))
        self.assertTrue(team_run.input_snapshot['generation_batch_uuid'])
        self.assertEqual(team_run.summary['artifact_persistent_key'], 'generated/team-development-plan.json')
        self.assertEqual(team_run.summary['batch_status'], 'completed')
        self.assertEqual(len(individual_runs), 1)
        self.assertEqual(individual_runs[0].employee, employee)
        self.assertEqual(individual_runs[0].blueprint_run, current_blueprint)
        self.assertTrue(individual_runs[0].is_current)
        self.assertEqual(current_team.uuid, team_run.uuid)
        self.assertEqual(latest_team.uuid, team_run.uuid)
        self.assertEqual(current_individual.uuid, individual_runs[0].uuid)
        self.assertEqual(len(current_individuals), 1)
        self.assertEqual(summary['team_plan_uuid'], team_run.uuid)
        self.assertEqual(summary['matrix_run_uuid'], current_matrix.uuid)
        self.assertEqual(summary['batch_status'], 'completed')
        self.assertEqual(latest_summary['team_plan_uuid'], team_run.uuid)
        self.assertEqual(actions['action_counts'], team_run.recommendation_payload['action_counts'])
        self.assertTrue(actions['priority_actions'])

    def test_team_qdrant_context_is_advisory_only(self):
        workspace = IntakeWorkspace.objects.create(name='Advisory Co', slug='advisory-co')
        blueprint = self._create_blueprint(workspace)
        alice = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Analyst',
            metadata={},
        )
        bob = Employee.objects.create(
            workspace=workspace,
            full_name='Bob Doe',
            email='bob@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        employees, matrix_cells, risk_payload = self._team_matrix_payload(alice, bob)
        matrix = self._create_matrix(
            workspace,
            blueprint,
            employees=employees,
            matrix_cells=matrix_cells,
            risk_payload=risk_payload,
        )

        with patch('development_plans.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ):
            baseline = _build_team_recommendation_payload_sync(workspace.pk, blueprint.pk, matrix.pk)

        with patch(
            'development_plans.services.retrieve_workspace_evidence_sync',
            return_value=[
                {
                    'source_kind': 'roadmap',
                    'source_title': 'Roadmap',
                    'section_heading': 'Launch',
                    'score': 0.45,
                    'chunk_text': 'Roadmap context for launch.',
                }
            ],
        ), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[
                {
                    'retrieval_lane': 'cv',
                    'doc_type': 'cv_role_history',
                    'score': 0.41,
                    'section_heading': 'Backend systems',
                    'chunk_text': 'Built backend systems and reliability tooling.',
                }
            ],
        ):
            enriched = _build_team_recommendation_payload_sync(workspace.pk, blueprint.pk, matrix.pk)

        self.assertEqual(
            [item['action_type'] for item in baseline['priority_actions']],
            [item['action_type'] for item in enriched['priority_actions']],
        )
        self.assertFalse(baseline['priority_actions'][0]['supporting_context']['roadmap_context'])
        self.assertTrue(enriched['priority_actions'][0]['supporting_context']['roadmap_context'])

    def test_partial_failed_latest_batch_does_not_replace_current_batch(self):
        workspace = IntakeWorkspace.objects.create(name='Partial Failure Co', slug='partial-failure-co')
        blueprint = self._create_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Analyst',
            metadata={},
        )
        matrix = self._create_matrix(
            workspace,
            blueprint,
            employees=[
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'current_title': employee.current_title,
                    'best_fit_role': {
                        'role_name': 'Backend Engineer',
                        'role_family': 'backend_engineer',
                        'seniority': 'senior',
                        'fit_score': 0.86,
                    },
                    'adjacent_roles': [],
                    'top_gaps': [{'skill_key': 'system-design', 'supported_initiatives': ['launch']}],
                    'total_gap_score': 3.2,
                    'average_confidence': 0.71,
                }
            ],
            matrix_cells=self._matrix_cells(employee),
            risk_payload={
                'top_priority_gaps': [
                    {
                        'column_key': 'role-backend:system-design:4',
                        'role_name': 'Backend Engineer',
                        'role_family': 'backend_engineer',
                        'seniority': 'senior',
                        'skill_key': 'system-design',
                        'skill_name_en': 'System Design',
                        'target_level': 4,
                        'average_gap': 1.2,
                        'average_confidence': 0.67,
                        'max_priority': 5,
                        'priority_gap_score': 6.0,
                        'employees_meeting_target': 0,
                        'employees_below_target': 1,
                        'incomplete_count': 0,
                    }
                ],
                'concentration_risks': [],
                'near_fit_candidates': [],
                'uncovered_roles': [],
            },
        )

        current_batch_uuid = uuid4()
        old_team = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Current team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=current_batch_uuid,
            completed_at=timezone.now(),
            is_current=True,
            plan_version='stage9-v1',
            input_snapshot={'generation_batch_uuid': str(current_batch_uuid)},
            recommendation_payload={'action_counts': {'develop': 1}},
            final_report_key='generated/current-team.json',
            summary={'batch_status': 'completed', 'expected_employee_count': 1},
            plan_payload={'priority_actions': [{'action_type': 'develop'}]},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Current PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=current_batch_uuid,
            completed_at=timezone.now(),
            is_current=True,
            plan_version='stage9-v1',
            input_snapshot={'generation_batch_uuid': str(current_batch_uuid)},
            recommendation_payload={},
            final_report_key='generated/current-pdp.json',
            summary={},
            plan_payload={'development_actions': []},
        )

        with patch(
            'development_plans.services.retrieve_workspace_evidence_sync',
            return_value=[],
        ), patch(
            'development_plans.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'development_plans.services._upload_generated_plan_artifact',
            side_effect=[
                {'media_file_uuid': 'media-team', 'persistent_key': 'generated/team-plan.json'},
                RuntimeError('boom'),
            ],
        ), patch(
            'development_plans.services.call_openai_structured',
            side_effect=self._narrative_side_effect,
        ):
            result = async_to_sync(generate_development_plans)(workspace, team_title='Broken batch')

        latest_team = async_to_sync(get_latest_team_plan)(workspace)
        current_team = async_to_sync(get_current_team_plan)(workspace)
        latest_summary = async_to_sync(get_latest_plan_summary)(workspace)
        current_summary = async_to_sync(get_current_plan_summary)(workspace)

        self.assertEqual(result['team_plan'].status, PlanRunStatus.COMPLETED)
        self.assertFalse(result['team_plan'].is_current)
        self.assertEqual(result['team_plan'].summary['batch_status'], 'partial_failed')
        self.assertEqual(latest_team.uuid, result['team_plan'].uuid)
        self.assertEqual(current_team.uuid, old_team.uuid)
        self.assertEqual(latest_summary['batch_status'], 'partial_failed')
        self.assertEqual(latest_summary['failed_individual_plan_count'], 1)
        self.assertEqual(current_summary['team_plan_uuid'], old_team.uuid)

    def test_current_selectors_fallback_to_latest_completed_legacy_rows(self):
        workspace = IntakeWorkspace.objects.create(name='Legacy Current Co', slug='legacy-current-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )

        legacy_team = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            title='Legacy team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            summary={'batch_status': 'legacy'},
            plan_payload={'priority_actions': []},
        )
        legacy_individual = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            title='Legacy PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'development_actions': []},
        )

        current_team = async_to_sync(get_current_team_plan)(workspace)
        current_individual = async_to_sync(get_current_individual_plan)(workspace, str(employee.uuid))
        current_summary = async_to_sync(get_current_plan_summary)(workspace)

        self.assertEqual(current_team.uuid, legacy_team.uuid)
        self.assertEqual(current_individual.uuid, legacy_individual.uuid)
        self.assertFalse(current_summary['is_current'])

    def test_latest_legacy_fallback_stays_on_team_lineage(self):
        workspace = IntakeWorkspace.objects.create(name='Legacy Latest Co', slug='legacy-latest-co')
        current_blueprint = self._create_blueprint(workspace)
        old_blueprint = self._create_blueprint(workspace, title='Older blueprint', is_published=False)
        current_matrix = self._create_matrix(workspace, current_blueprint, updated_suffix='current')
        old_matrix = self._create_matrix(workspace, old_blueprint, updated_suffix='old')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )

        latest_team = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=current_blueprint,
            matrix_run=current_matrix,
            title='Latest team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'priority_actions': []},
        )
        matching_individual = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=current_blueprint,
            matrix_run=current_matrix,
            title='Matching PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'development_actions': []},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=old_blueprint,
            matrix_run=old_matrix,
            title='Newer unrelated PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now() + timedelta(minutes=1),
            plan_version='stage9-v1',
            plan_payload={'development_actions': []},
        )

        latest_team_run = async_to_sync(get_latest_team_plan)(workspace)
        latest_individuals = async_to_sync(list_latest_individual_plans)(workspace)
        latest_individual = async_to_sync(get_latest_individual_plan)(workspace, str(employee.uuid))

        self.assertEqual(latest_team_run.uuid, latest_team.uuid)
        self.assertEqual(len(latest_individuals), 1)
        self.assertEqual(latest_individuals[0].uuid, matching_individual.uuid)
        self.assertEqual(latest_individual.uuid, matching_individual.uuid)

    def test_ensure_plan_export_artifacts_creates_json_markdown_and_html(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Co', slug='artifact-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)
        run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Stage 10 team export',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            generation_batch_uuid=uuid4(),
            plan_version='stage9-v1',
            input_snapshot={'generation_batch_uuid': 'batch-1'},
            recommendation_payload={
                'employee_count': 2,
                'action_counts': {'hire': 1, 'develop': 1},
                'top_priority_gaps': [{'role_name': 'Backend Engineer', 'skill_name_en': 'System Design', 'average_gap': 1.2, 'max_priority': 5}],
                'concentration_risks': [{'role_name': 'Backend Engineer', 'skill_name_en': 'Python', 'ready_employee_count': 1}],
                'near_fit_candidates': [{'full_name': 'Alice Doe', 'role_name': 'Backend Engineer', 'gap': 0.7}],
                'uncovered_roles': [],
            },
            summary={'batch_status': 'completed', 'expected_employee_count': 2},
            plan_payload={
                'executive_summary': 'Close the biggest capability risks first.',
                'roadmap_priority_note': 'System design depth matters for the next roadmap cycle.',
                'priority_actions': [
                    {
                        'action_type': 'hire',
                        'action': 'Hire targeted System Design depth for Backend Engineer.',
                        'owner_role': 'Backend Engineer',
                        'why_now': 'This gap is blocking delivery.',
                    }
                ],
                'hiring_recommendations': ['Hire targeted System Design depth for Backend Engineer.'],
                'development_focus': ['Develop internal platform depth.'],
                'single_points_of_failure': ['Python depth is concentrated in too few people.'],
                'action_counts': {'hire': 1, 'develop': 1},
            },
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ):
            artifacts = async_to_sync(ensure_plan_export_artifacts)(run)

        self.assertEqual({artifact.artifact_format for artifact in artifacts}, {'json', 'markdown', 'html'})
        by_format = {artifact.artifact_format: artifact for artifact in artifacts}
        self.assertIn('# Stage 10 team export', by_format['markdown'].media_file.processing_metadata['rendered_content'])
        self.assertIn('B2B learning platform.', by_format['markdown'].media_file.processing_metadata['rendered_content'])
        self.assertIn('Launch platform reliability and monetization initiatives.', by_format['markdown'].media_file.processing_metadata['rendered_content'])
        self.assertIn('<!doctype html>', by_format['html'].media_file.processing_metadata['rendered_content'])
        self.assertIn('"artifact_version": "stage10-v1"', by_format['json'].media_file.processing_metadata['rendered_content'])

    def test_ensure_plan_export_artifacts_formats_structured_blueprint_context_readably(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Context Co', slug='artifact-context-co')
        blueprint = self._create_blueprint(workspace)
        blueprint.company_context = {
            'company_name': 'Hyperskill',
            'what_company_does': 'Learning platform for developers.',
            'why_skills_improvement_now': 'Roadmap execution requires clearer role targets.',
            'products': ['Hyperskill'],
            'current_tech_stack': ['Python', 'React'],
        }
        blueprint.roadmap_context = [
            {
                'initiative_id': 'marketplace-launch',
                'title': 'Marketplace launch',
                'summary': 'Launch a paid marketplace integration in Q2.',
                'time_horizon': 'Q2 2026',
                'criticality': 'high',
                'functions_required': ['Engineering', 'Product'],
                'tech_stack': ['Python', 'Stripe'],
            }
        ]
        blueprint.save(update_fields=['company_context', 'roadmap_context', 'updated_at'])
        matrix = self._create_matrix(workspace, blueprint)
        run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Structured context export',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            generation_batch_uuid=uuid4(),
            plan_version='stage9-v1',
            input_snapshot={'generation_batch_uuid': 'batch-structured'},
            recommendation_payload={'employee_count': 1, 'action_counts': {'develop': 1}},
            summary={'batch_status': 'completed', 'expected_employee_count': 1},
            plan_payload={
                'executive_summary': 'Focus the plan on the most time-sensitive capability gaps.',
                'roadmap_priority_note': 'Marketplace launch needs platform and billing depth.',
                'priority_actions': [],
                'hiring_recommendations': [],
                'development_focus': [],
                'single_points_of_failure': [],
                'action_counts': {'develop': 1},
            },
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ):
            artifacts = async_to_sync(ensure_plan_export_artifacts)(run)

        by_format = {artifact.artifact_format: artifact for artifact in artifacts}
        markdown = by_format['markdown'].media_file.processing_metadata['rendered_content']
        html = by_format['html'].media_file.processing_metadata['rendered_content']
        json_payload = by_format['json'].media_file.processing_metadata['rendered_content']

        self.assertIn('Company name: Hyperskill', markdown)
        self.assertIn('What the company does: Learning platform for developers.', markdown)
        self.assertIn('Marketplace launch (Q2 2026): Launch a paid marketplace integration in Q2.', markdown)
        self.assertIn('Functions required: Engineering, Product', markdown)
        self.assertNotIn("{'company_name': 'Hyperskill'", markdown)

        self.assertIn('Company name: Hyperskill', html)
        self.assertIn('Marketplace launch (Q2 2026): Launch a paid marketplace integration in Q2.', html)
        self.assertNotIn('{&#x27;company_name&#x27;: &#x27;Hyperskill&#x27;', html)

        self.assertIn('"company_name": "Hyperskill"', json_payload)
        self.assertIn('"title": "Marketplace launch"', json_payload)

    def test_latest_team_artifact_bundle_uses_latest_run_and_signed_urls(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Latest Co', slug='artifact-latest-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)

        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Older team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now() - timedelta(days=1),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'Older', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 1},
        )
        latest_run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Latest team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'Latest', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 2},
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ), patch(
            'development_plans.services.generate_signed_url_for_file',
            side_effect=self._fake_signed_url,
        ) as signed_url_mock:
            bundle = async_to_sync(get_latest_team_plan_artifact_bundle)(workspace)

        self.assertEqual(bundle['plan_uuid'], latest_run.uuid)
        self.assertEqual(len(bundle['artifacts']), 3)
        self.assertTrue(all(item['signed_url'].startswith('https://downloads.test/') for item in bundle['artifacts']))
        self.assertTrue(signed_url_mock.await_args_list)
        for call in signed_url_mock.await_args_list:
            self.assertIn('attachment;', call.kwargs.get('response_content_disposition', ''))
            self.assertIn('filename*=', call.kwargs.get('response_content_disposition', ''))
            self.assertIsNotNone(call.kwargs.get('response_content_type'))

    def test_current_individual_artifact_bundle_uses_current_selector(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Current Co', slug='artifact-current-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        current_run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Current PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            completed_at=timezone.now() - timedelta(hours=1),
            plan_version='stage9-v1',
            plan_payload={'current_role_fit': 'Strong fit', 'adjacent_roles': [], 'strengths': [], 'priority_gaps': [], 'development_actions': [], 'roadmap_alignment': '', 'mobility_note': ''},
            recommendation_payload={'employee_uuid': str(employee.uuid), 'employee_name': employee.full_name},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Newer non-current PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            is_current=False,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'current_role_fit': 'Newer fit', 'adjacent_roles': [], 'strengths': [], 'priority_gaps': [], 'development_actions': [], 'roadmap_alignment': '', 'mobility_note': ''},
            recommendation_payload={'employee_uuid': str(employee.uuid), 'employee_name': employee.full_name},
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ), patch(
            'development_plans.services.generate_signed_url_for_file',
            side_effect=self._fake_signed_url,
        ):
            bundle = async_to_sync(get_current_individual_plan_artifact_bundle)(workspace, str(employee.uuid))

        self.assertEqual(bundle['plan_uuid'], current_run.uuid)
        self.assertEqual(bundle['scope'], 'individual')
        self.assertTrue(bundle['selected_as_current'])
        self.assertEqual(len(bundle['artifacts']), 3)

    def test_list_latest_workspace_plan_artifacts_stays_on_latest_team_lineage(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Lineage Co', slug='artifact-lineage-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        old_batch = uuid4()
        new_batch = uuid4()
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Old team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=old_batch,
            completed_at=timezone.now() - timedelta(days=1),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'Old', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 1},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Old PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=old_batch,
            completed_at=timezone.now() - timedelta(days=1),
            plan_version='stage9-v1',
            plan_payload={'current_role_fit': 'Old fit', 'adjacent_roles': [], 'strengths': [], 'priority_gaps': [], 'development_actions': [], 'roadmap_alignment': '', 'mobility_note': ''},
            recommendation_payload={'employee_uuid': str(employee.uuid), 'employee_name': employee.full_name},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Latest team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=new_batch,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'Latest', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 1},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Latest PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=new_batch,
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'current_role_fit': 'Latest fit', 'adjacent_roles': [], 'strengths': [], 'priority_gaps': [], 'development_actions': [], 'roadmap_alignment': '', 'mobility_note': ''},
            recommendation_payload={'employee_uuid': str(employee.uuid), 'employee_name': employee.full_name},
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ):
            artifacts = async_to_sync(list_latest_workspace_plan_artifacts)(workspace)

        self.assertEqual(len(artifacts), 6)
        self.assertEqual({artifact.generation_batch_uuid for artifact in artifacts}, {new_batch})

    def test_workspace_artifact_list_preserves_rerun_versioning(self):
        workspace = IntakeWorkspace.objects.create(name='Artifact Version Co', slug='artifact-version-co')
        blueprint = self._create_blueprint(workspace)
        matrix = self._create_matrix(workspace, blueprint)
        first_run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='First team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=uuid4(),
            completed_at=timezone.now() - timedelta(days=1),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'First', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 1},
        )
        second_run = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix,
            title='Second team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            generation_batch_uuid=uuid4(),
            completed_at=timezone.now(),
            plan_version='stage9-v1',
            plan_payload={'executive_summary': 'Second', 'priority_actions': [], 'hiring_recommendations': [], 'development_focus': [], 'single_points_of_failure': [], 'action_counts': {}},
            recommendation_payload={'employee_count': 1},
        )

        with patch(
            'development_plans.services.store_prototype_generated_text_artifact',
            side_effect=self._fake_store_generated_artifact,
        ):
            artifacts = async_to_sync(list_workspace_plan_artifacts)(workspace)

        self.assertEqual(len(artifacts), 6)
        self.assertEqual(artifacts[0].plan_run_id, second_run.uuid)
        self.assertEqual(
            {artifact.plan_run_id for artifact in artifacts},
            {first_run.uuid, second_run.uuid},
        )
        self.assertEqual(DevelopmentPlanArtifact.objects.filter(plan_run=first_run).count(), 3)
        self.assertEqual(DevelopmentPlanArtifact.objects.filter(plan_run=second_run).count(), 3)


class DevelopmentPlanPlanningContextSelectorTests(TestCase):
    def test_current_and_latest_selectors_are_scoped_by_planning_context(self):
        workspace = IntakeWorkspace.objects.create(name='Scoped Plans Co', slug='scoped-plans-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Johnson',
            current_title='Backend Engineer',
        )
        planning_context = PlanningContext.objects.create(
            workspace=workspace,
            name='AI Context',
            slug='ai-context',
            kind=PlanningContext.Kind.ORG,
        )

        legacy_team = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            title='Legacy team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            completed_at=timezone.now() - timedelta(days=1),
            plan_payload={},
            recommendation_payload={},
        )
        legacy_individual = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            employee=employee,
            title='Legacy PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            completed_at=timezone.now() - timedelta(days=1),
            plan_payload={},
            recommendation_payload={},
        )
        context_team = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            planning_context=planning_context,
            title='Context team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            completed_at=timezone.now(),
            plan_payload={},
            recommendation_payload={},
        )
        context_individual = DevelopmentPlanRun.objects.create(
            workspace=workspace,
            planning_context=planning_context,
            employee=employee,
            title='Context PDP',
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            completed_at=timezone.now(),
            plan_payload={},
            recommendation_payload={},
        )

        self.assertEqual(async_to_sync(get_current_team_plan)(workspace).uuid, legacy_team.uuid)
        self.assertEqual(
            async_to_sync(get_current_team_plan)(workspace, planning_context=planning_context).uuid,
            context_team.uuid,
        )
        self.assertEqual(async_to_sync(get_latest_team_plan)(workspace).uuid, legacy_team.uuid)
        self.assertEqual(
            async_to_sync(get_latest_team_plan)(workspace, planning_context=planning_context).uuid,
            context_team.uuid,
        )

        legacy_runs = async_to_sync(list_current_individual_plans)(workspace)
        context_runs = async_to_sync(list_current_individual_plans)(workspace, planning_context=planning_context)

        self.assertEqual([run.uuid for run in legacy_runs], [legacy_individual.uuid])
        self.assertEqual([run.uuid for run in context_runs], [context_individual.uuid])
        self.assertEqual(
            async_to_sync(get_current_individual_plan)(workspace, str(employee.uuid)).uuid,
            legacy_individual.uuid,
        )
        self.assertEqual(
            async_to_sync(get_current_individual_plan)(
                workspace,
                str(employee.uuid),
                planning_context=planning_context,
            ).uuid,
            context_individual.uuid,
        )
