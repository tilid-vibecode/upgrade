import json
from io import StringIO
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import TestCase

from company_intake.models import IntakeWorkspace
from employee_assessment.models import AssessmentCycle, AssessmentStatus
from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
from evidence_matrix.services import (
    build_evidence_matrix,
    build_matrix_run_response,
    build_matrix_slice_response,
    get_current_completed_matrix_run,
    get_latest_matrix_run,
    get_matrix_employee_payload,
)
from org_context.models import (
    Employee,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    EscoOccupation,
    EscoOccupationBroaderRelation,
    EscoOccupationSkillRelation,
    EscoSkill,
    EscoSkillBroaderRelation,
    EscoSkillRelation,
    OccupationMapping,
    RoleProfile,
    RoleSkillRequirement,
    Skill,
)
from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun


class Stage8EvidenceMatrixTests(TestCase):
    def _create_esco_skill(self, preferred_label: str, suffix: str) -> EscoSkill:
        return EscoSkill.objects.create(
            concept_uri=f'http://data.europa.eu/esco/skill/{suffix}',
            concept_type='KnowledgeSkillCompetence',
            skill_type='skill/competence',
            reuse_level='cross-sector',
            preferred_label=preferred_label,
            normalized_preferred_label=preferred_label.casefold(),
            status='released',
            metadata={},
        )

    def _create_workspace_skill(
        self,
        workspace,
        *,
        canonical_key: str,
        display_name_en: str,
        esco_skill: EscoSkill | None = None,
    ) -> Skill:
        return Skill.objects.create(
            workspace=workspace,
            canonical_key=canonical_key,
            display_name_en=display_name_en,
            display_name_ru='',
            source='test_seed',
            esco_skill=esco_skill,
            metadata={},
        )

    def _create_published_blueprint(self, workspace) -> SkillBlueprintRun:
        return SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            role_candidates=[],
            required_skill_set=[],
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
            skill = self._create_workspace_skill(
                workspace,
                canonical_key=row['canonical_key'],
                display_name_en=row['display_name_en'],
                esco_skill=row.get('esco_skill'),
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

    def test_build_evidence_matrix_fuses_sources_and_attaches_provenance(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Co', slug='matrix-co')
        blueprint = self._create_published_blueprint(workspace)
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
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={'schema_version': 'stage7-v1'},
            result_summary={},
        )
        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {'canonical_key': 'python', 'display_name_en': 'Python', 'target_level': 4, 'priority': 5},
                {
                    'canonical_key': 'system-design',
                    'display_name_en': 'System Design',
                    'target_level': 4,
                    'priority': 4,
                },
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.86,
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
            evidence_text='Built billing APIs and production services in Python.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[0].skill,
            source_kind='self_assessment',
            current_level=3,
            confidence=0.8,
            weight=0.44,
            evidence_text='Recently built Python integrations.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[1].skill,
            source_kind='self_assessment',
            current_level=2,
            confidence=0.6,
            weight=0.33,
            evidence_text='Can design service boundaries with support.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )

        def _fake_retrieval(*_args, **kwargs):
            skill_keys = kwargs.get('skill_keys') or []
            if skill_keys == ['python']:
                return [
                    {
                        'retrieval_lane': 'cv',
                        'doc_type': 'cv_skill_evidence',
                        'score': 0.48,
                        'section_heading': 'Python',
                        'source_title': 'Alice Doe CV',
                        'evidence_row_uuid': '',
                        'chunk_text': 'Built billing APIs and production services in Python.',
                    },
                    {
                        'retrieval_lane': 'self_assessment',
                        'doc_type': 'self_assessment_example',
                        'score': 0.41,
                        'section_heading': 'Python',
                        'source_title': 'Self assessment',
                        'evidence_row_uuid': '',
                        'question_id': 'skill:python',
                        'chunk_text': 'Recently built Python integrations.',
                    },
                ]
            return [
                {
                    'retrieval_lane': 'self_assessment',
                    'doc_type': 'self_assessment_example',
                    'score': 0.38,
                    'section_heading': 'System Design',
                    'source_title': 'Self assessment',
                    'evidence_row_uuid': '',
                    'question_id': 'skill:system-design',
                    'chunk_text': 'Can design service boundaries with support.',
                }
            ]

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            side_effect=_fake_retrieval,
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': ['System Design'],
                    'coverage_risks': ['Backend depth'],
                    'mobility_opportunities': ['Alice can grow'],
                    'incompleteness_flags': [],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        run.refresh_from_db()
        self.assertEqual(run.status, EvidenceMatrixStatus.COMPLETED)
        self.assertEqual(run.blueprint_run, blueprint)
        self.assertEqual(run.matrix_version, 'stage8-v2')
        self.assertEqual(run.input_snapshot['blueprint_run_uuid'], str(blueprint.uuid))
        self.assertEqual(run.input_snapshot['assessment_cycle_uuids_used'], [str(cycle.uuid)])

        cells = {
            item['skill_key']: item
            for item in run.matrix_payload['matrix_cells']
        }
        python_cell = cells['python']
        self.assertEqual(python_cell['current_level'], 3.61)
        self.assertEqual(python_cell['gap'], 0.39)
        self.assertEqual(python_cell['confidence'], 0.87)
        self.assertEqual(
            [item['source_kind'] for item in python_cell['evidence_source_mix']],
            ['employee_cv', 'self_assessment'],
        )
        self.assertGreaterEqual(len(python_cell['provenance_snippets']), 2)
        self.assertEqual(
            {item['retrieval_lane'] for item in python_cell['provenance_snippets']},
            {'postgres', 'cv', 'self_assessment'},
        )
        self.assertEqual(run.matrix_payload['employees'][0]['best_fit_role']['role_name'], 'Backend Engineer')
        self.assertTrue(run.heatmap_payload['skill_columns'])
        self.assertTrue(run.risk_payload['top_priority_gaps'])
        self.assertTrue(run.summary_payload['critical_gaps'])

    def test_build_evidence_matrix_marks_incomplete_cells(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Flags Co', slug='matrix-flags-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Bob Doe',
            email='bob@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Flags Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {'canonical_key': 'go', 'display_name_en': 'Go', 'target_level': 4, 'priority': 5},
                {'canonical_key': 'leadership', 'display_name_en': 'Leadership', 'target_level': 3, 'priority': 4},
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.5,
            rationale='Tentative fit',
            related_initiatives=['launch'],
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[0].skill,
            source_kind='self_assessment',
            current_level=2,
            confidence=0.5,
            weight=0.28,
            evidence_text='Used Go in one internal service.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': ['Go', 'Leadership'],
                    'coverage_risks': ['Single-source evidence'],
                    'mobility_opportunities': [],
                    'incompleteness_flags': ['low confidence'],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        cells = {
            item['skill_key']: item
            for item in run.matrix_payload['matrix_cells']
        }
        self.assertIn('self_report_only', cells['go']['incompleteness_flags'])
        self.assertIn('low_confidence', cells['go']['incompleteness_flags'])
        self.assertIn('role_match_uncertain', cells['go']['advisory_flags'])
        self.assertIn('no_evidence', cells['leadership']['incompleteness_flags'])
        self.assertIn('role_match_uncertain', run.matrix_payload['employees'][0]['advisory_flags'])
        self.assertGreater(
            run.incompleteness_payload['employees_with_insufficient_evidence_count'],
            0,
        )

    def test_build_evidence_matrix_uses_esco_exact_hierarchy_related_and_occupation_prior(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix ESCO Co', slug='matrix-esco-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Cara Doe',
            email='cara@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='ESCO Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )

        api_design_esco = self._create_esco_skill('API Design', 'api-design')
        architecture_parent_esco = self._create_esco_skill('Software Architecture', 'software-architecture')
        architecture_child_esco = self._create_esco_skill('Microservice Architecture', 'microservice-architecture')
        stakeholder_esco = self._create_esco_skill('Stakeholder Management', 'stakeholder-management')
        workshop_esco = self._create_esco_skill('Facilitate Workshops', 'facilitate-workshops')
        incident_esco = self._create_esco_skill('Incident Response', 'incident-response')

        EscoSkillBroaderRelation.objects.create(
            concept_type='KnowledgeSkillCompetence',
            concept_uri=architecture_child_esco.concept_uri,
            concept_label=architecture_child_esco.preferred_label,
            broader_type='KnowledgeSkillCompetence',
            broader_uri=architecture_parent_esco.concept_uri,
            broader_label=architecture_parent_esco.preferred_label,
            esco_skill=architecture_child_esco,
            broader_skill=architecture_parent_esco,
        )
        EscoSkillRelation.objects.create(
            original_skill=stakeholder_esco,
            related_skill=workshop_esco,
            original_skill_type='skill/competence',
            relation_type='related',
            related_skill_type='skill/competence',
            metadata={},
        )

        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {
                    'canonical_key': 'api-design',
                    'display_name_en': 'API Design',
                    'target_level': 4,
                    'priority': 5,
                    'esco_skill': api_design_esco,
                },
                {
                    'canonical_key': 'software-architecture',
                    'display_name_en': 'Software Architecture',
                    'target_level': 4,
                    'priority': 4,
                    'esco_skill': architecture_parent_esco,
                },
                {
                    'canonical_key': 'stakeholder-management',
                    'display_name_en': 'Stakeholder Management',
                    'target_level': 4,
                    'priority': 4,
                    'esco_skill': stakeholder_esco,
                },
                {
                    'canonical_key': 'incident-response',
                    'display_name_en': 'Incident Response',
                    'target_level': 4,
                    'priority': 5,
                    'esco_skill': incident_esco,
                },
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.86,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )

        exact_alias_skill = self._create_workspace_skill(
            workspace,
            canonical_key='design-apis',
            display_name_en='Design APIs',
            esco_skill=api_design_esco,
        )
        hierarchy_skill = self._create_workspace_skill(
            workspace,
            canonical_key='microservice-architecture',
            display_name_en='Microservice Architecture',
            esco_skill=architecture_child_esco,
        )
        related_skill = self._create_workspace_skill(
            workspace,
            canonical_key='facilitate-workshops',
            display_name_en='Facilitate Workshops',
            esco_skill=workshop_esco,
        )

        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=exact_alias_skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.8,
            weight=0.7,
            evidence_text='Designed public and internal APIs across multiple services.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=hierarchy_skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.7,
            weight=0.6,
            evidence_text='Led multiple microservice decomposition projects.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=related_skill,
            source_kind='self_assessment',
            current_level=4,
            confidence=0.7,
            weight=0.4,
            evidence_text='Regularly facilitate roadmap and stakeholder workshops.',
            assessment_cycle=cycle,
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )

        esco_occupation = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/backend-software-engineer',
            concept_type='Occupation',
            isco_group='2512',
            preferred_label='Backend software engineer',
            normalized_preferred_label='backend software engineer',
            status='released',
            metadata={},
        )
        OccupationMapping.objects.create(
            workspace=workspace,
            role_profile=role,
            occupation_key='backend-software-engineer',
            occupation_name_en='Backend software engineer',
            occupation_name_ru='',
            esco_occupation=esco_occupation,
            match_score=0.92,
            metadata={},
        )
        secondary_esco_occupation = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/site-reliability-engineer',
            concept_type='Occupation',
            isco_group='2529',
            preferred_label='Site reliability engineer',
            normalized_preferred_label='site reliability engineer',
            status='released',
            metadata={},
        )
        OccupationMapping.objects.create(
            workspace=workspace,
            role_profile=role,
            occupation_key='site-reliability-engineer',
            occupation_name_en='Site reliability engineer',
            occupation_name_ru='',
            esco_occupation=secondary_esco_occupation,
            match_score=0.41,
            metadata={},
        )
        EscoOccupationSkillRelation.objects.create(
            occupation=esco_occupation,
            skill=incident_esco,
            relation_type='essential',
            skill_type='skill/competence',
            metadata={},
        )
        EscoOccupationSkillRelation.objects.create(
            occupation=secondary_esco_occupation,
            skill=incident_esco,
            relation_type='optional',
            skill_type='skill/competence',
            metadata={},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ) as retrieve_mock, patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': [],
                    'coverage_risks': [],
                    'mobility_opportunities': [],
                    'incompleteness_flags': [],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        cells = {
            item['skill_key']: item
            for item in run.matrix_payload['matrix_cells']
        }
        self.assertGreater(cells['api-design']['exact_match_count'], 0)
        self.assertEqual(cells['api-design']['current_level'], 4.0)

        self.assertGreater(cells['software-architecture']['hierarchy_match_count'], 0)
        self.assertGreater(cells['software-architecture']['current_level'], 0.0)
        self.assertIn('indirect_evidence_only', cells['software-architecture']['incompleteness_flags'])

        self.assertGreater(cells['stakeholder-management']['related_match_count'], 0)
        self.assertGreater(cells['stakeholder-management']['current_level'], 0.0)
        self.assertIn('indirect_evidence_only', cells['stakeholder-management']['incompleteness_flags'])

        self.assertGreater(cells['incident-response']['occupation_prior_count'], 0)
        self.assertEqual(cells['incident-response']['occupation_prior_count'], 1)
        self.assertGreater(cells['incident-response']['current_level'], 0.0)
        self.assertIn('occupation_prior_only', cells['incident-response']['incompleteness_flags'])
        self.assertEqual(
            cells['incident-response']['esco_support_breakdown'][0]['occupation_names'],
            ['Backend software engineer'],
        )

        support_summary = run.matrix_payload['team_summary']['esco_support_summary']
        self.assertGreaterEqual(support_summary['cells_with_exact_match'], 1)
        self.assertGreaterEqual(support_summary['cells_with_hierarchy_match'], 1)
        self.assertGreaterEqual(support_summary['cells_with_related_match'], 1)
        self.assertGreaterEqual(support_summary['cells_with_occupation_prior'], 1)

        query_skill_keys = {
            call.kwargs['query_text']: set(call.kwargs.get('skill_keys') or [])
            for call in retrieve_mock.call_args_list
        }
        architecture_query = next(
            query_text
            for query_text in query_skill_keys
            if 'Software Architecture' in query_text
        )
        stakeholder_query = next(
            query_text
            for query_text in query_skill_keys
            if 'Stakeholder Management' in query_text
        )
        self.assertIn('software-architecture', query_skill_keys[architecture_query])
        self.assertIn('microservice-architecture', query_skill_keys[architecture_query])
        self.assertIn('stakeholder-management', query_skill_keys[stakeholder_query])
        self.assertIn('facilitate-workshops', query_skill_keys[stakeholder_query])

    def test_build_evidence_matrix_walks_multihop_esco_hierarchy(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix ESCO Deep Co', slug='matrix-esco-deep-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Deep Graph Doe',
            email='deep@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='ESCO Deep Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )

        root_esco = self._create_esco_skill('Distributed Systems Design', 'distributed-systems-design')
        middle_esco = self._create_esco_skill('Service Architecture', 'service-architecture')
        leaf_esco = self._create_esco_skill('Event-Driven Microservices', 'event-driven-microservices')

        EscoSkillBroaderRelation.objects.create(
            concept_type='KnowledgeSkillCompetence',
            concept_uri=middle_esco.concept_uri,
            concept_label=middle_esco.preferred_label,
            broader_type='KnowledgeSkillCompetence',
            broader_uri=root_esco.concept_uri,
            broader_label=root_esco.preferred_label,
            esco_skill=middle_esco,
            broader_skill=root_esco,
        )
        EscoSkillBroaderRelation.objects.create(
            concept_type='KnowledgeSkillCompetence',
            concept_uri=leaf_esco.concept_uri,
            concept_label=leaf_esco.preferred_label,
            broader_type='KnowledgeSkillCompetence',
            broader_uri=middle_esco.concept_uri,
            broader_label=middle_esco.preferred_label,
            esco_skill=leaf_esco,
            broader_skill=middle_esco,
        )

        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {
                    'canonical_key': 'distributed-systems-design',
                    'display_name_en': 'Distributed Systems Design',
                    'target_level': 4,
                    'priority': 5,
                    'esco_skill': root_esco,
                }
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.82,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )
        deep_skill = self._create_workspace_skill(
            workspace,
            canonical_key='event-driven-microservices',
            display_name_en='Event-Driven Microservices',
            esco_skill=leaf_esco,
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=deep_skill,
            source_kind='self_assessment',
            current_level=4,
            confidence=0.75,
            weight=0.5,
            evidence_text='Designed event-driven microservice ecosystems.',
            assessment_cycle=cycle,
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': [],
                    'coverage_risks': [],
                    'mobility_opportunities': [],
                    'incompleteness_flags': [],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        cell = run.matrix_payload['matrix_cells'][0]
        self.assertEqual(cell['skill_key'], requirements[0].skill.canonical_key)
        self.assertGreater(cell['hierarchy_match_count'], 0)
        self.assertGreater(cell['current_level'], 0.0)
        self.assertIn('hierarchy_child', cell['esco_support_types'])
        self.assertIn('indirect_evidence_only', cell['incompleteness_flags'])

    def test_build_evidence_matrix_expands_occupation_priors_through_ancestor_occupations(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Occ Ancestor Co', slug='matrix-occ-ancestor-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Priya Doe',
            email='priya@example.com',
            current_title='Platform Engineer',
            metadata={},
        )
        AssessmentCycle.objects.create(
            workspace=workspace,
            title='Ancestor Prior Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        incident_esco = self._create_esco_skill('Incident Response', 'incident-response-ancestor')
        role, _requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[
                {
                    'canonical_key': 'incident-response-ancestor',
                    'display_name_en': 'Incident Response',
                    'target_level': 4,
                    'priority': 5,
                    'esco_skill': incident_esco,
                }
            ],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.9,
            rationale='Primary fit',
            related_initiatives=['launch'],
            metadata={},
        )

        parent_occupation = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/software-engineer-parent',
            concept_type='Occupation',
            isco_group='2512',
            preferred_label='Software engineer',
            normalized_preferred_label='software engineer',
            status='released',
            metadata={},
        )
        child_occupation = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/platform-engineer-child',
            concept_type='Occupation',
            isco_group='2512',
            preferred_label='Platform engineer',
            normalized_preferred_label='platform engineer',
            status='released',
            metadata={},
        )
        EscoOccupationBroaderRelation.objects.create(
            concept_type='Occupation',
            concept_uri=child_occupation.concept_uri,
            concept_label=child_occupation.preferred_label,
            broader_type='Occupation',
            broader_uri=parent_occupation.concept_uri,
            broader_label=parent_occupation.preferred_label,
            esco_occupation=child_occupation,
            broader_occupation=parent_occupation,
        )
        OccupationMapping.objects.create(
            workspace=workspace,
            role_profile=role,
            occupation_key='platform-engineer',
            occupation_name_en='Platform engineer',
            occupation_name_ru='',
            esco_occupation=child_occupation,
            match_score=0.88,
            metadata={},
        )
        EscoOccupationSkillRelation.objects.create(
            occupation=parent_occupation,
            skill=incident_esco,
            relation_type='essential',
            skill_type='skill/competence',
            metadata={},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': [],
                    'coverage_risks': [],
                    'mobility_opportunities': [],
                    'incompleteness_flags': [],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        cell = run.matrix_payload['matrix_cells'][0]
        self.assertGreater(cell['occupation_prior_count'], 0)
        self.assertIn('occupation_prior_only', cell['incompleteness_flags'])
        occupation_prior_signals = [
            item
            for item in list(cell['evidence_rows'] or [])
            if item.get('support_type') == 'occupation_prior'
        ]
        self.assertTrue(occupation_prior_signals)
        self.assertEqual(occupation_prior_signals[0]['prior_origin'], 'ancestor')
        self.assertEqual(occupation_prior_signals[0]['occupation_name_en'], 'Software engineer')

    def test_export_matrix_calibration_command_outputs_signal_payloads(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Export Co', slug='matrix-export-co')
        run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v2',
            input_snapshot={
                'active_weight_profile': 'balanced_v1',
                'occupation_prior_policy': 'direct_and_ancestor',
            },
            summary_payload={'team_summary': 'Summary'},
            matrix_payload={
                'matrix_cells': [
                    {
                        'employee_uuid': 'emp-1',
                        'employee_name': 'Alice Doe',
                        'role_profile_uuid': 'role-1',
                        'role_name': 'Backend Engineer',
                        'skill_key': 'python',
                        'skill_name_en': 'Python',
                        'target_level': 4,
                        'current_level': 3.6,
                        'gap': 0.4,
                        'confidence': 0.84,
                        'priority': 5,
                        'role_fit_score': 0.9,
                        'esco_support_breakdown': [{'support_type': 'exact', 'label': 'Exact ESCO skill match'}],
                        'evidence_rows': [
                            {
                                'support_type': 'exact',
                                'source_kind': 'employee_cv',
                                'raw_current_level': 4.0,
                                'raw_confidence': 0.8,
                                'raw_weight': 0.7,
                                'current_level': 4.0,
                                'confidence': 0.8,
                                'weight': 0.7,
                                'prior_origin': 'direct',
                                'prior_distance': 0,
                            }
                        ],
                        'support_signals': [
                            {
                                'support_type': 'exact',
                                'source_kind': 'employee_cv',
                                'raw_current_level': 4.0,
                                'raw_confidence': 0.8,
                                'raw_weight': 0.7,
                                'raw_base_current_level': 4.0,
                                'raw_base_confidence': 0.8,
                                'raw_base_weight': 0.7,
                                'current_level': 4.0,
                                'confidence': 0.8,
                                'weight': 0.7,
                                'prior_origin': 'direct',
                                'prior_distance': 0,
                            },
                            {
                                'support_type': 'occupation_prior',
                                'source_kind': 'occupation_prior',
                                'raw_current_level': 1.2,
                                'raw_confidence': 0.31,
                                'raw_weight': 0.18,
                                'raw_base_current_level': 1.67,
                                'raw_base_confidence': 0.31,
                                'raw_base_weight': 0.25,
                                'current_level': 1.2,
                                'confidence': 0.31,
                                'weight': 0.18,
                                'prior_origin': 'ancestor',
                                'prior_distance': 1,
                            },
                        ],
                        'incompleteness_flags': [],
                    }
                ]
            },
        )

        out = StringIO()
        call_command(
            'export_matrix_calibration',
            '--workspace-slug',
            workspace.slug,
            '--matrix-run-uuid',
            str(run.uuid),
            stdout=out,
        )
        exported = json.loads(out.getvalue())
        self.assertEqual(exported['workspace_slug'], workspace.slug)
        self.assertEqual(exported['cell_count'], 1)
        self.assertEqual(exported['cells'][0]['support_signals'][0]['support_type'], 'exact')
        self.assertEqual(len(exported['cells'][0]['support_signals']), 2)
        self.assertIn('review_labels', exported['cells'][0])
        self.assertIsNone(exported['cells'][0]['review_labels']['ready'])

    def test_build_evidence_matrix_defaults_to_latest_cycle_and_excludes_stale_self_assessment_rows(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Cycle Co', slug='matrix-cycle-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        old_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Old cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        current_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Current cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[{'canonical_key': 'python', 'display_name_en': 'Python', 'target_level': 4, 'priority': 5}],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.9,
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
            evidence_text='Built Python systems.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[0].skill,
            source_kind='self_assessment',
            current_level=1,
            confidence=0.8,
            weight=0.44,
            evidence_text='Old cycle response.',
            metadata={'assessment_cycle_uuid': str(old_cycle.uuid)},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=requirements[0].skill,
            source_kind='self_assessment',
            current_level=5,
            confidence=0.8,
            weight=0.44,
            evidence_text='Current cycle response.',
            metadata={'assessment_cycle_uuid': str(current_cycle.uuid)},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': [],
                    'coverage_risks': [],
                    'mobility_opportunities': [],
                    'incompleteness_flags': [],
                }
            ),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        run.refresh_from_db()
        self.assertEqual(run.input_snapshot['selected_assessment_cycle_uuid'], str(current_cycle.uuid))
        self.assertEqual(run.input_snapshot['assessment_cycle_uuids_used'], [str(current_cycle.uuid)])

        cell = run.matrix_payload['matrix_cells'][0]
        source_levels = {
            item['source_kind']: item['current_level']
            for item in cell['evidence_source_mix']
        }
        self.assertEqual(source_levels['self_assessment'], 5.0)

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(
                return_value={
                    'team_summary': 'Summary',
                    'critical_gaps': [],
                    'coverage_risks': [],
                    'mobility_opportunities': [],
                    'incompleteness_flags': [],
                }
            ),
        ):
            old_run = async_to_sync(build_evidence_matrix)(
                workspace,
                assessment_cycle_uuid=str(old_cycle.uuid),
            )

        old_run.refresh_from_db()
        self.assertEqual(old_run.input_snapshot['selected_assessment_cycle_uuid'], str(old_cycle.uuid))
        old_cell = old_run.matrix_payload['matrix_cells'][0]
        old_source_levels = {
            item['source_kind']: item['current_level']
            for item in old_cell['evidence_source_mix']
        }
        self.assertEqual(old_source_levels['self_assessment'], 1.0)

    def test_matrix_summary_fallback_keeps_run_completed(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Fallback Co', slug='matrix-fallback-co')
        blueprint = self._create_published_blueprint(workspace)
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        AssessmentCycle.objects.create(
            workspace=workspace,
            title='Fallback Cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        role, requirements = self._create_role_with_requirements(
            workspace,
            blueprint,
            skill_rows=[{'canonical_key': 'python', 'display_name_en': 'Python', 'target_level': 4, 'priority': 5}],
        )
        EmployeeRoleMatch.objects.create(
            workspace=workspace,
            employee=employee,
            role_profile=role,
            source_kind='blueprint',
            fit_score=0.9,
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
            evidence_text='Strong Python ownership.',
            metadata={},
        )

        with patch(
            'evidence_matrix.services.retrieve_employee_fused_evidence_sync',
            return_value=[],
        ), patch(
            'evidence_matrix.services._build_matrix_summary_with_llm',
            new=AsyncMock(side_effect=RuntimeError('LLM unavailable')),
        ):
            run = async_to_sync(build_evidence_matrix)(workspace)

        run.refresh_from_db()
        self.assertEqual(run.status, EvidenceMatrixStatus.COMPLETED)
        self.assertTrue(run.summary_payload['team_summary'])
        self.assertIn('critical_gaps', run.summary_payload)

    def test_matrix_helpers_expose_slices_and_employee_payload(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Helper Co', slug='matrix-helper-co')
        blueprint = self._create_published_blueprint(workspace)
        run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            input_snapshot={'blueprint_run_uuid': str(blueprint.uuid)},
            summary_payload={'team_summary': 'Summary'},
            heatmap_payload={'rows': []},
            risk_payload={'risks': []},
            incompleteness_payload={'flag_counts': {}},
            matrix_payload={
                'employees': [
                    {
                        'employee_uuid': 'emp-1',
                        'full_name': 'Alice Doe',
                        'skills': [],
                    }
                ],
                'matrix_cells': [],
            },
        )

        response = async_to_sync(build_matrix_run_response)(run)
        heatmap_slice = async_to_sync(build_matrix_slice_response)(run, run.heatmap_payload)
        employee_payload = async_to_sync(get_matrix_employee_payload)(run, 'emp-1')

        self.assertEqual(str(response['blueprint_run_uuid']), str(blueprint.uuid))
        self.assertEqual(heatmap_slice['matrix_version'], 'stage8-v1')
        self.assertEqual(employee_payload['full_name'], 'Alice Doe')

    def test_build_evidence_matrix_requires_published_blueprint(self):
        workspace = IntakeWorkspace.objects.create(name='No Blueprint Co', slug='no-blueprint-co')
        with self.assertRaisesMessage(ValueError, 'published blueprint'):
            async_to_sync(build_evidence_matrix)(workspace)

    def test_current_completed_matrix_selector_ignores_failed_latest_and_old_blueprints(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Selector Co', slug='matrix-selector-co')
        old_blueprint = self._create_published_blueprint(workspace)
        current_blueprint = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Current blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            role_candidates=[],
            required_skill_set=[],
        )
        EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=old_blueprint,
            title='Old completed',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            matrix_payload={},
        )
        expected_run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=current_blueprint,
            title='Current completed',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            matrix_payload={},
        )
        EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=current_blueprint,
            title='Current failed rebuild',
            status=EvidenceMatrixStatus.FAILED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            matrix_payload={},
        )

        resolved = async_to_sync(get_current_completed_matrix_run)(workspace, blueprint_run=current_blueprint)
        self.assertEqual(resolved, expected_run)

    def test_current_completed_matrix_selector_prefers_current_cycle_lineage(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Cycle Selector Co', slug='matrix-cycle-selector-co')
        blueprint = self._create_published_blueprint(workspace)
        old_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Old cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        current_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Current cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Old cycle matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            input_snapshot={'selected_assessment_cycle_uuid': str(old_cycle.uuid)},
            matrix_payload={},
        )
        expected_run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Current cycle matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            input_snapshot={'selected_assessment_cycle_uuid': str(current_cycle.uuid)},
            matrix_payload={},
        )

        resolved = async_to_sync(get_current_completed_matrix_run)(workspace, blueprint_run=blueprint)
        self.assertEqual(resolved, expected_run)

    def test_latest_matrix_selector_returns_latest_attempt_even_when_current_cycle_moved_on(self):
        workspace = IntakeWorkspace.objects.create(name='Matrix Latest Selector Co', slug='matrix-latest-selector-co')
        blueprint = self._create_published_blueprint(workspace)
        old_cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Old cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        AssessmentCycle.objects.create(
            workspace=workspace,
            title='Latest cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        expected_run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Previous matrix attempt',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            matrix_version='stage8-v1',
            input_snapshot={'selected_assessment_cycle_uuid': str(old_cycle.uuid)},
            matrix_payload={},
        )

        resolved = async_to_sync(get_latest_matrix_run)(workspace, blueprint_run=blueprint)
        self.assertEqual(resolved, expected_run)
