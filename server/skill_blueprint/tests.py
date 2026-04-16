from importlib import import_module
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.db import IntegrityError, transaction
from django.test import TestCase

from company_intake.models import (
    IntakeWorkspace,
    WorkspaceSource,
    WorkspaceSourceKind,
    WorkspaceSourceStatus,
    WorkspaceSourceTransport,
)
from org_context.models import (
    Employee,
    EmployeeCVProfile,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    ParsedSource,
    RoadmapAnalysisRun,
    RoleProfile,
    RoleSkillRequirement,
    Skill,
    SkillAlias,
    SkillResolutionOverride,
)

from .models import (
    BlueprintStatus,
    ClarificationCycle,
    ClarificationQuestion,
    ClarificationQuestionStatus,
    RoleLibraryEntry,
    RoleLibrarySnapshot,
    RoleLibraryStatus,
    SkillBlueprintRun,
)
from .services import (
    _build_deterministic_shortlist,
    _build_role_library_url_candidates,
    _build_role_library_markdown_candidates,
    _flatten_required_skill_set,
    _load_employee_matching_inputs_sync,
    _normalize_company_context_payload,
    _normalize_role_library_public_url,
    _build_blueprint_inputs_sync,
    _compute_coverage_analysis_sync,
    discover_role_library_urls,
    fetch_page_text,
    get_role_library_seed_manifest,
    _load_answered_clarifications_sync,
    _merge_role_overlay,
    _persist_blueprint_payload_sync,
    _select_curated_role_library_urls,
    _sync_clarification_cycle_from_run_sync,
    answer_blueprint_clarifications,
    approve_blueprint_run,
    build_role_library_snapshot_response,
    get_active_clarification_run,
    get_current_published_blueprint_run,
    get_effective_blueprint_run,
    generate_skill_blueprint,
    get_latest_approved_blueprint_run,
    get_default_blueprint_run,
    get_latest_blueprint_run,
    get_latest_published_blueprint_run,
    list_clarification_question_history,
    list_open_clarification_questions,
    normalize_external_role_title,
    patch_blueprint_run,
    publish_blueprint_run,
    refresh_blueprint_from_clarifications,
    review_blueprint_run,
    start_blueprint_revision,
    sync_role_library_for_workspace,
)


class RoleLibraryNormalizationTests(TestCase):
    def setUp(self):
        super().setUp()
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='Postgres',
            normalized_term='postgres',
            canonical_key='postgresql',
            display_name_en='PostgreSQL',
            display_name_ru='',
            aliases=['postgres'],
            status='approved',
            source='test_override',
            metadata={},
        )

    def test_normalize_external_role_title_maps_to_canonical_family(self):
        normalized = normalize_external_role_title(
            role_name='Senior Product Manager, Growth',
            role_family_hint='Product',
        )

        self.assertEqual(normalized['canonical_family'], 'product_manager')
        self.assertEqual(normalized['normalized_department'], 'Product')

    def test_normalize_external_role_title_marks_unknown_roles_uncategorized(self):
        normalized = normalize_external_role_title(
            role_name='Operations Excellence Lead',
            role_family_hint='',
        )

        self.assertEqual(normalized['canonical_family'], 'uncategorized')
        self.assertEqual(normalized['normalized_department'], 'Other')

    def test_normalize_external_role_title_maps_business_support_and_founding_roles(self):
        business_development = normalize_external_role_title(
            role_name='Business Development Manager',
            role_family_hint='Business Development',
            department='Sales',
        )
        support_manager = normalize_external_role_title(
            role_name='Senior Support Manager',
            role_family_hint='Support',
            department='Technical Support',
        )
        founding_engineer = normalize_external_role_title(
            role_name='Founding Engineer',
            role_family_hint='Engineering',
            department='Development',
        )

        self.assertEqual(business_development['canonical_family'], 'business_development_manager')
        self.assertEqual(business_development['normalized_department'], 'Sales')
        self.assertEqual(support_manager['canonical_family'], 'support_manager')
        self.assertEqual(support_manager['normalized_department'], 'Support')
        self.assertEqual(founding_engineer['canonical_family'], 'founding_engineer')
        self.assertEqual(founding_engineer['normalized_department'], 'Engineering')

    def test_normalize_external_role_title_does_not_repeat_warning_for_uncategorized_hint(self):
        with self.assertLogs('skill_blueprint.services', level='WARNING') as captured:
            normalize_external_role_title(
                role_name='Operations Excellence Lead',
                role_family_hint='Operations',
                department='Operations',
            )
            normalize_external_role_title(
                role_name='Operations Excellence Lead',
                role_family_hint='uncategorized',
                department='Operations',
            )

        self.assertEqual(len(captured.records), 1)

    def test_normalize_external_role_title_maps_additional_workspace_titles(self):
        self.assertEqual(
            normalize_external_role_title(role_name='Markenting specialist')['canonical_family'],
            'marketing_specialist',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='Community manager')['canonical_family'],
            'marketing_specialist',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='Content manager')['canonical_family'],
            'marketing_specialist',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='Digital Marketing Manager')['canonical_family'],
            'marketing_specialist',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='Support specialist')['canonical_family'],
            'support_manager',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='Founding B2B Sales Lead')['canonical_family'],
            'sales_manager',
        )
        self.assertEqual(
            normalize_external_role_title(role_name='CEO')['canonical_family'],
            'executive_leader',
        )


class BlueprintCompanyContextNormalizationTests(TestCase):
    def setUp(self):
        super().setUp()
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='Postgres',
            normalized_term='postgres',
            canonical_key='postgresql',
            display_name_en='PostgreSQL',
            display_name_ru='',
            aliases=['postgres'],
            status='approved',
            source='test_override',
            metadata={},
        )

    def test_workspace_profile_fills_locations_and_tech_stack_when_llm_payload_is_sparse(self):
        normalized = _normalize_company_context_payload(
            {
                'company_name': 'Hyperskill',
                'what_company_does': 'Learning platform for developers.',
                'why_skills_improvement_now': 'Ship roadmap work faster.',
                'products': ['Hyperskill'],
                'customers': ['Developers'],
                'markets': ['Global'],
                'missing_information': [],
            },
            {
                'company_profile': {
                    'company_name': 'Hyperskill',
                    'company_description': 'Learning platform for developers.',
                    'main_products': ['Hyperskill'],
                    'target_customers': ['Developers'],
                    'primary_market_geography': 'Global',
                    'locations': ['Remote', 'Europe'],
                    'current_tech_stack': ['Python', 'React', 'Postgres'],
                    'planned_tech_stack': ['FastAPI', 'OpenAI API'],
                    'pilot_scope_notes': 'Ship roadmap work faster.',
                    'notable_constraints_or_growth_plans': '',
                }
            },
        )

        self.assertEqual(normalized['locations'], ['Remote', 'Europe'])
        self.assertEqual(normalized['current_tech_stack'], ['Python', 'React', 'Postgres'])
        self.assertEqual(normalized['planned_tech_stack'], ['FastAPI', 'OpenAI API'])

    def test_select_curated_role_library_urls_prioritizes_seed_matches(self):
        selected, summary = _select_curated_role_library_urls(
            [
                'https://handbook.gitlab.com/job-description-library/engineering/',
            ],
            [
                'https://handbook.gitlab.com/job-description-library/engineering/backend-engineer/',
                'https://handbook.gitlab.com/job-description-library/engineering/frontend-engineer/',
                'https://handbook.gitlab.com/job-description-library/product/product-manager/',
                'https://handbook.gitlab.com/job-description-library/random/misc-role/',
            ],
            max_pages=3,
        )

        self.assertEqual(len(selected), 3)
        self.assertIn('backend_engineer', summary['matched_families'])
        self.assertIn('frontend_engineer', summary['matched_families'])

    def test_merge_role_overlay_adds_canonical_family_and_normalized_skills(self):
        normalized = _merge_role_overlay(
            {
                'role_name': 'Backend Engineer',
                'department': 'Engineering',
                'role_family': 'Engineering',
                'summary': 'Build backend systems.',
                'levels': [],
                'responsibilities': ['Build APIs'],
                'requirements': ['Experience with Python'],
                'skills': ['Python'],
                'required_skills': ['Django'],
                'desirable_skills': ['Postgres'],
                'seniority_signals': ['Senior'],
                'stakeholder_expectations': ['Work with product managers'],
                'canonical_role_family_hint': '',
            },
            page_url='https://example.com/backend-engineer',
            page_title='Backend Engineer',
        )


class EmployeeMatchingInputTests(TestCase):
    def test_load_employee_matching_inputs_includes_rich_cv_context_and_skips_unresolved_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='Matching Input Co', slug='matching-input-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Engineering Manager',
            metadata={},
        )
        active_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            source='seed',
            metadata={},
        )
        pending_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='legacy-stack',
            display_name_en='Legacy Stack',
            source='seed',
            metadata={},
            resolution_status=Skill.ResolutionStatus.PENDING_REVIEW,
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=active_skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.9,
            weight=0.8,
            evidence_text='Built backend services in Python.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=pending_skill,
            source_kind='employee_cv',
            current_level=2,
            confidence=0.7,
            weight=0.7,
            evidence_text='Pending skill that should not be surfaced yet.',
            metadata={},
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Alice CV',
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='CV',
            status=WorkspaceSourceStatus.PARSED,
        )
        EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=employee,
            status=EmployeeCVProfile.Status.MATCHED,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.USABLE,
            headline='Delivery-focused engineering leader',
            current_role='Engineering Manager',
            seniority='senior',
            role_family='engineering_manager',
            extracted_payload={
                'role_history': [
                    {
                        'company_name': 'Example Co',
                        'role_title': 'Engineering Manager',
                        'start_date': '2020',
                        'end_date': '2025',
                        'achievements': ['Scaled the platform for 10M users'],
                        'domains': ['B2B SaaS'],
                        'leadership_signals': ['Managed 12 engineers'],
                    }
                ],
                'achievements': [
                    {'summary': 'Reduced infra cost by 30%', 'confidence_score': 0.92},
                ],
                'domain_experience': [
                    {'domain': 'B2B SaaS', 'confidence_score': 0.88},
                ],
                'leadership_signals': [
                    {'signal': 'Managed 12 engineers', 'confidence_score': 0.9},
                ],
            },
            metadata={},
        )

        payloads = _load_employee_matching_inputs_sync(workspace.pk)

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload['headline'], 'Delivery-focused engineering leader')
        self.assertEqual(payload['seniority'], 'senior')
        self.assertEqual(len(payload['skills_from_evidence']), 1)
        self.assertEqual(payload['skills_from_evidence'][0]['skill_name_en'], 'Python')
        self.assertEqual(payload['role_history'][0]['company_name'], 'Example Co')
        self.assertEqual(payload['achievements'][0]['summary'], 'Reduced infra cost by 30%')
        self.assertEqual(payload['domain_experience'][0]['domain'], 'B2B SaaS')
        self.assertEqual(payload['leadership_signals'][0]['signal'], 'Managed 12 engineers')

    def test_deterministic_shortlist_prefers_role_history_and_resolved_skill_overlap(self):
        workspace = IntakeWorkspace.objects.create(name='Shortlist Co', slug='shortlist-co')
        employees = [
            {
                'employee_uuid': 'emp-1',
                'full_name': 'Alice Doe',
                'current_title': 'Senior Backend Engineer',
                'seniority': 'senior',
                'org_units': ['Engineering'],
                'projects': ['Marketplace'],
                'skills_from_evidence': [
                    {'skill_name_en': 'Python', 'resolution_status': Skill.ResolutionStatus.RESOLVED},
                    {'skill_name_en': 'API Design', 'resolution_status': Skill.ResolutionStatus.RESOLVED},
                ],
                'role_history': [
                    {'role_title': 'Backend Engineer', 'domains': ['marketplace']},
                ],
            },
            {
                'employee_uuid': 'emp-2',
                'full_name': 'Bob Doe',
                'current_title': 'Designer',
                'seniority': 'mid',
                'org_units': ['Design'],
                'projects': ['Landing Page'],
                'skills_from_evidence': [
                    {'skill_name_en': 'Figma', 'resolution_status': Skill.ResolutionStatus.RESOLVED},
                ],
                'role_history': [
                    {'role_title': 'UX Designer', 'domains': ['marketing']},
                ],
            },
        ]
        role_profiles = [
            {
                'role_uuid': 'role-1',
                'name': 'Backend Engineer',
                'family': 'backend_engineer',
                'seniority': 'senior',
                'department': 'Engineering',
                'related_initiatives': ['marketplace'],
                'skill_requirements': [
                    {'skill_name_en': 'Python'},
                    {'skill_name_en': 'API Design'},
                ],
            }
        ]

        shortlist = _build_deterministic_shortlist(employees, role_profiles, workspace)

        self.assertEqual(shortlist['role-1'][0]['employee_uuid'], 'emp-1')
        self.assertGreater(shortlist['role-1'][0]['shortlist_score'], shortlist['role-1'][1]['shortlist_score'])

    def test_flatten_required_skill_set_exposes_display_fields(self):
        flattened = _flatten_required_skill_set(
            [
                {
                    'role_name': 'Backend Engineer',
                    'skills': [
                        {
                            'skill_name_en': 'Python',
                            'target_level': 4,
                            'priority': 5,
                            'requirement_type': 'core',
                            'supported_initiatives': ['marketplace-launch'],
                            'confidence': 0.82,
                            'criticality': 'high',
                            'reason': 'Needed for API implementation',
                        }
                    ],
                }
            ]
        )

        self.assertEqual(flattened[0]['target_level'], 4)
        self.assertEqual(flattened[0]['priority'], 5)
        self.assertEqual(flattened[0]['requirement_type'], 'core')
        self.assertEqual(flattened[0]['criticality'], 'high')
        self.assertIn('Needed for API implementation', flattened[0]['reason'])
        self.assertIn('Backend Engineer', flattened[0]['required_by_roles'])
        self.assertIn('marketplace-launch', flattened[0]['supported_initiatives'])

    def test_compute_coverage_analysis_matches_aliases_and_normalized_role_families(self):
        workspace = IntakeWorkspace.objects.create(name='Coverage Co', slug='coverage-co')
        roadmap_analysis = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            workstreams=[
                {
                    'id': 'ws-platform',
                    'initiative_id': 'init-platform',
                    'name': 'Platform hardening',
                    'team_shape': {'roles_needed': ['DevOps Engineer']},
                    'required_capabilities': [{'capability': 'K8s', 'level': 'advanced', 'criticality': 'high'}],
                }
            ],
            capability_bundles=[],
        )
        blueprint_run = SkillBlueprintRun.objects.create(workspace=workspace, title='Blueprint', status=BlueprintStatus.DRAFT)
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='kubernetes',
            display_name_en='Kubernetes',
            display_name_ru='Kubernetes',
            resolution_status=Skill.ResolutionStatus.RESOLVED,
        )
        SkillAlias.objects.create(skill=skill, alias='K8s')
        role_profile = RoleProfile.objects.create(
            workspace=workspace,
            blueprint_run=blueprint_run,
            name='Platform Engineer',
            family='platform_sre_engineer',
            seniority='senior',
        )
        RoleSkillRequirement.objects.create(
            workspace=workspace,
            role_profile=role_profile,
            skill=skill,
            target_level=4,
            priority=5,
        )

        coverage = _compute_coverage_analysis_sync(workspace.pk, str(blueprint_run.uuid), str(roadmap_analysis.uuid))

        self.assertEqual(coverage['coverage_score'], 100)
        self.assertEqual(coverage['workstream_coverage'][0]['roles_covered'], ['platform_sre_engineer'])


class RoleLibrarySyncTests(TestCase):
    def setUp(self):
        super().setUp()
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='Postgres',
            normalized_term='postgres',
            canonical_key='postgresql',
            display_name_en='PostgreSQL',
            display_name_ru='',
            aliases=['postgres'],
            status='approved',
            source='test_override',
            metadata={},
        )

    def test_sync_role_library_for_workspace_persists_canonical_summary_and_aliases(self):
        workspace = IntakeWorkspace.objects.create(name='Stage 3 Co', slug='stage-3-co')

        _page_text_by_url = {
            'https://handbook.gitlab.com/job-description-library/engineering/backend-engineer/': {
                'url': 'https://handbook.gitlab.com/job-description-library/engineering/backend-engineer/',
                'title': 'Backend Engineer',
                'text': 'Backend Engineer\n\nResponsibilities\n\nBuild APIs.\n\nSkills\n\nPython and Django.' * 20,
            },
            'https://handbook.gitlab.com/job-description-library/product/product-manager/': {
                'url': 'https://handbook.gitlab.com/job-description-library/product/product-manager/',
                'title': 'Product Manager',
                'text': 'Product Manager\n\nResponsibilities\n\nSet roadmap.\n\nSkills\n\nExperimentation and analytics.' * 20,
            },
        }

        async def _fake_fetch_page_text(url):
            if url in _page_text_by_url:
                return _page_text_by_url[url]
            return {'url': url, 'title': '', 'text': 'Index page.'}

        with patch(
            'skill_blueprint.services.discover_role_library_urls',
            new=AsyncMock(
                return_value=(
                    [
                        'https://handbook.gitlab.com/job-description-library/engineering/backend-engineer/',
                        'https://handbook.gitlab.com/job-description-library/product/product-manager/',
                    ],
                    [],
                )
            ),
        ), patch(
            'skill_blueprint.services.fetch_page_text',
            new=_fake_fetch_page_text,
        ), patch(
            'skill_blueprint.services.extract_role_library_entry_with_llm',
            new=AsyncMock(
                side_effect=[
                    {
                        'role_name': 'Backend Engineer',
                        'department': 'Engineering',
                        'role_family': 'Engineering',
                        'summary': 'Build backend systems.',
                        'levels': [],
                        'responsibilities': ['Build APIs'],
                        'requirements': ['Experience with distributed systems'],
                        'skills': ['Python'],
                        'required_skills': ['Django'],
                        'desirable_skills': ['Postgres'],
                        'seniority_signals': ['Senior'],
                        'stakeholder_expectations': ['Collaborate with product'],
                        'canonical_role_family_hint': '',
                    },
                    {
                        'role_name': 'Senior Product Manager, Growth',
                        'department': 'Product',
                        'role_family': 'Product',
                        'summary': 'Drive growth roadmap.',
                        'levels': [],
                        'responsibilities': ['Own growth experiments'],
                        'requirements': ['Strong communication'],
                        'skills': ['Experimentation'],
                        'required_skills': ['Product Analytics'],
                        'desirable_skills': ['Stakeholder Management'],
                        'seniority_signals': ['Senior'],
                        'stakeholder_expectations': ['Partner with GTM'],
                        'canonical_role_family_hint': '',
                    },
                ]
            ),
        ):
            snapshot = async_to_sync(sync_role_library_for_workspace)(
                workspace,
                base_urls=['https://handbook.gitlab.com/job-description-library/engineering/'],
                max_pages=5,
            )

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, RoleLibraryStatus.COMPLETED)
        self.assertEqual(snapshot.summary['canonical_family_counts']['backend_engineer'], 1)
        self.assertEqual(snapshot.summary['canonical_family_counts']['product_manager'], 1)
        self.assertGreaterEqual(snapshot.summary['normalized_skill_count'], 4)
        self.assertGreater(snapshot.summary['alias_count'], 0)

        entries = list(RoleLibraryEntry.objects.filter(snapshot=snapshot).order_by('role_name'))
        self.assertEqual(entries[0].role_family, 'backend_engineer')
        self.assertEqual(entries[1].role_family, 'product_manager')
        self.assertTrue(Skill.objects.filter(workspace=workspace, canonical_key='python').exists())
        self.assertTrue(SkillAlias.objects.filter(skill__workspace=workspace, alias='postgres').exists())

        payload = async_to_sync(build_role_library_snapshot_response)(snapshot)
        self.assertIn('backend_engineer', payload['canonical_family_counts'])
        self.assertGreaterEqual(payload['normalized_skill_count'], 4)
        self.assertTrue(payload['seed_urls_used'])

    def test_normalize_role_library_public_url_uses_public_job_families_route(self):
        self.assertEqual(
            _normalize_role_library_public_url('https://handbook.gitlab.com/job-description-library/design/'),
            'https://handbook.gitlab.com/job-description-library/design/',
        )
        self.assertEqual(
            _build_role_library_url_candidates('https://handbook.gitlab.com/job-description-library/design/'),
            [
                'https://handbook.gitlab.com/job-description-library/design/',
                'https://handbook.gitlab.com/job-families/design/',
            ],
        )

    def test_checked_in_seed_manifest_includes_product_leadership_leaf(self):
        manifest = get_role_library_seed_manifest()

        self.assertEqual(manifest['provider'], 'gitlab_handbook')
        self.assertIn(
            'https://handbook.gitlab.com/job-description-library/product/product-management-leadership/',
            manifest['base_urls'],
        )

    def test_build_role_library_markdown_candidates_use_public_gitlab_source(self):
        candidates = _build_role_library_markdown_candidates(
            'https://handbook.gitlab.com/job-description-library/product/product-management-leadership/'
        )

        self.assertIn(
            'https://gitlab.com/gitlab-com/content-sites/handbook/-/raw/main/content/job-description-library/product/product-management-leadership.md',
            candidates,
        )
        self.assertIn(
            'https://gitlab.com/gitlab-com/content-sites/handbook/-/raw/main/content/job-families/product/product-management-leadership.md',
            candidates,
        )

    def test_fetch_page_text_falls_back_to_markdown_source(self):
        with patch(
            'skill_blueprint.services.fetch_raw_html',
            new=AsyncMock(side_effect=RuntimeError('redirected to sign in')),
        ), patch(
            'skill_blueprint.services.fetch_role_library_markdown',
            new=AsyncMock(
                return_value=(
                    '---\n'
                    'title: Product Management - Leadership\n'
                    '---\n'
                    '# Product Management - Leadership\n\n'
                    'Managers in the Product department.\n'
                    '- Coaches product managers\n'
                )
            ),
        ):
            payload = async_to_sync(fetch_page_text)(
                'https://handbook.gitlab.com/job-description-library/product/product-management-leadership/'
            )

        self.assertEqual(payload['source_format'], 'markdown')
        self.assertEqual(payload['title'], 'Product Management - Leadership')
        self.assertIn('Managers in the Product department.', payload['text'])

    def test_discover_role_library_urls_keeps_seed_when_markdown_source_is_public(self):
        url = 'https://handbook.gitlab.com/job-description-library/product/product-management-leadership/'

        with patch(
            'skill_blueprint.services.fetch_raw_html',
            new=AsyncMock(side_effect=RuntimeError('403 forbidden')),
        ), patch(
            'skill_blueprint.services.fetch_role_library_markdown',
            new=AsyncMock(return_value='# Product Management - Leadership\n\nCoaches PMs.'),
        ):
            discovered, failures = async_to_sync(discover_role_library_urls)([url], max_pages=5)

        self.assertEqual(discovered, [url])
        self.assertEqual(failures, [])

    def test_sync_role_library_for_workspace_marks_failure(self):
        workspace = IntakeWorkspace.objects.create(name='Stage 3 Failure Co', slug='stage-3-failure-co')

        with patch(
            'skill_blueprint.services.discover_role_library_urls',
            new=AsyncMock(side_effect=RuntimeError('network down')),
        ):
            snapshot = async_to_sync(sync_role_library_for_workspace)(workspace, max_pages=5)

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, RoleLibraryStatus.FAILED)
        self.assertIn('network down', snapshot.error_message)


class BlueprintInputRetrievalTests(TestCase):
    def _create_parsed_source(
        self,
        workspace: IntakeWorkspace,
        *,
        source_kind: str,
        title: str,
        text: str,
    ) -> ParsedSource:
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title=title,
            source_kind=source_kind,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=text,
            status=WorkspaceSourceStatus.PARSED,
        )
        return ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=len(text.split()),
            char_count=len(text),
            extracted_text=text,
            metadata={},
        )

    def _create_snapshot(self, workspace: IntakeWorkspace) -> RoleLibrarySnapshot:
        snapshot = RoleLibrarySnapshot.objects.create(
            workspace=workspace,
            status=RoleLibraryStatus.COMPLETED,
            provider='gitlab_handbook',
            summary={
                'canonical_family_counts': {'backend_engineer': 1},
                'normalized_skill_count': 3,
                'alias_count': 2,
                'seed_urls_used': ['https://example.com/backend-engineer'],
            },
        )
        RoleLibraryEntry.objects.create(
            snapshot=snapshot,
            role_name='Backend Engineer',
            department='Engineering',
            role_family='backend_engineer',
            page_url='https://example.com/backend-engineer',
            skills=['Python', 'Django', 'PostgreSQL'],
            responsibilities=['Build backend systems'],
            requirements=['Experience with APIs'],
            metadata={
                'canonical_role_family': 'backend_engineer',
                'required_skills': ['Python', 'Django'],
                'desirable_skills': ['PostgreSQL'],
                'stakeholder_expectations': ['Cross-functional delivery with product and design'],
                'occupation_reference': {'name_en': 'Backend Software Engineer'},
                'normalized_skills': [
                    {'canonical_key': 'python', 'display_name_en': 'Python', 'display_name_ru': 'Python', 'aliases': ['py']},
                    {'canonical_key': 'django', 'display_name_en': 'Django', 'display_name_ru': 'Django', 'aliases': []},
                    {'canonical_key': 'postgresql', 'display_name_en': 'PostgreSQL', 'display_name_ru': 'PostgreSQL', 'aliases': ['Postgres']},
                ],
            },
        )
        return snapshot

    def _create_roadmap_analysis(self, workspace: IntakeWorkspace) -> RoadmapAnalysisRun:
        return RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            source_summary={'source_count': 1},
            input_snapshot={'analysis_fingerprint': 'fingerprint-1'},
            initiatives=[
                {
                    'id': 'init-marketplace-launch',
                    'name': 'Marketplace Launch',
                    'goal': 'Launch an integration marketplace.',
                    'criticality': 'high',
                    'planned_window': 'Q2 2026',
                    'source_refs': ['roadmap-source'],
                    'confidence': 0.9,
                }
            ],
            workstreams=[
                {
                    'id': 'ws-marketplace-api',
                    'initiative_id': 'init-marketplace-launch',
                    'name': 'Marketplace API',
                    'scope': 'Build partner-facing APIs and billing hooks for marketplace launch.',
                    'delivery_type': 'backend_service',
                    'affected_systems': ['api', 'billing'],
                    'team_shape': {
                        'estimated_headcount': 2,
                        'roles_needed': ['Backend Engineer'],
                        'duration_months': 3,
                    },
                    'required_capabilities': [
                        {'capability': 'Python', 'level': 'advanced', 'criticality': 'high'},
                        {'capability': 'API Design', 'level': 'advanced', 'criticality': 'high'},
                    ],
                    'source_refs': ['roadmap-source'],
                    'confidence': 0.85,
                }
            ],
            capability_bundles=[
                {
                    'bundle_id': 'bundle-backend-platform',
                    'workstream_ids': ['ws-marketplace-api'],
                    'capability_name': 'Backend platform delivery',
                    'capability_type': 'technical',
                    'criticality': 'high',
                    'inferred_role_families': ['backend_engineer'],
                    'skill_hints': ['Python', 'API Design'],
                    'evidence_refs': ['roadmap-source'],
                    'confidence': 0.84,
                }
            ],
            dependencies=[],
            delivery_risks=[],
            prd_summaries=[],
            clarification_questions=[],
        )

    def test_build_blueprint_inputs_prefers_retrieved_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='Hyperskill', slug='hyperskill')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Fallback roadmap text that should not be preferred.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.STRATEGY,
            title='Growth Strategy',
            text='Fallback strategy text that should not be preferred.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            title='Backend Engineer JD',
            text='Fallback role reference text that should not be preferred.',
        )
        snapshot = self._create_snapshot(workspace)

        def _fake_retrieve(*args, **kwargs):
            doc_types = kwargs.get('doc_types') or []
            if doc_types == ['roadmap_context']:
                return [
                    {
                        'source_kind': WorkspaceSourceKind.ROADMAP,
                        'source_title': 'Revenue Roadmap',
                        'chunk_index': 1,
                        'score': 0.91,
                        'chunk_text': 'Retrieved roadmap evidence about GitHub integration and Q2 launch.',
                    }
                ]
            if doc_types == ['strategy_context']:
                return [
                    {
                        'source_kind': WorkspaceSourceKind.STRATEGY,
                        'source_title': 'Growth Strategy',
                        'chunk_index': 1,
                        'score': 0.82,
                        'chunk_text': 'Retrieved strategy evidence about enterprise expansion.',
                    }
                ]
            if doc_types == ['role_reference']:
                return [
                    {
                        'source_kind': WorkspaceSourceKind.JOB_DESCRIPTION,
                        'source_title': 'Backend Engineer JD',
                        'chunk_index': 1,
                        'score': 0.88,
                        'chunk_text': 'Retrieved role reference evidence about API ownership and Python.',
                    }
                ]
            return []

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', side_effect=_fake_retrieve):
            inputs = _build_blueprint_inputs_sync(workspace.pk, snapshot.pk)

        self.assertIn('Retrieved roadmap evidence', inputs['roadmap_input'])
        self.assertEqual(inputs['roadmap_input_mode'], 'legacy')
        self.assertIsNone(inputs['roadmap_analysis_uuid'])
        self.assertIn('Retrieved strategy evidence', inputs['strategy_evidence_digest'])
        self.assertIn('Retrieved role reference evidence', inputs['role_reference_evidence_digest'])
        self.assertEqual(inputs['evidence_digest'], '')
        self.assertIn('backend_engineer', inputs['role_library_digest'])
        self.assertEqual(inputs['source_summary']['retrieval']['roadmap_context']['match_count'], 1)
        self.assertTrue(inputs['source_summary']['retrieval']['roadmap_context']['used_vector_retrieval'])
        self.assertEqual(inputs['source_summary']['retrieval']['strategy_context']['match_count'], 1)
        self.assertTrue(inputs['source_summary']['retrieval']['strategy_context']['used_vector_retrieval'])
        self.assertEqual(inputs['source_summary']['retrieval']['role_reference']['match_count'], 1)
        self.assertTrue(inputs['source_summary']['retrieval']['role_reference']['used_vector_retrieval'])
        self.assertEqual(inputs['source_summary']['role_library']['canonical_family_counts']['backend_engineer'], 1)

    def test_build_blueprint_inputs_falls_back_to_parsed_text_when_retrieval_empty(self):
        workspace = IntakeWorkspace.objects.create(name='Hyperskill', slug='hyperskill-fallback')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Fallback roadmap text for blueprint generation.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.STRATEGY,
            title='Growth Strategy',
            text='Fallback strategy text for blueprint generation.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            title='Backend Engineer JD',
            text='Fallback role reference text for blueprint generation.',
        )
        snapshot = self._create_snapshot(workspace)

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]):
            inputs = _build_blueprint_inputs_sync(workspace.pk, snapshot.pk)

        self.assertIn('Fallback roadmap text for blueprint generation.', inputs['roadmap_input'])
        self.assertEqual(inputs['roadmap_input_mode'], 'legacy')
        self.assertIn('Fallback strategy text for blueprint generation.', inputs['strategy_evidence_digest'])
        self.assertIn('Fallback role reference text for blueprint generation.', inputs['role_reference_evidence_digest'])
        self.assertIn('Fallback roadmap text for blueprint generation.', inputs['evidence_digest'])
        self.assertIn('Fallback strategy text for blueprint generation.', inputs['evidence_digest'])
        self.assertIn('Fallback role reference text for blueprint generation.', inputs['evidence_digest'])
        self.assertIn('Backend Engineer', inputs['role_library_digest'])
        self.assertEqual(inputs['source_summary']['retrieval']['roadmap_context']['match_count'], 0)
        self.assertFalse(inputs['source_summary']['retrieval']['roadmap_context']['used_vector_retrieval'])
        self.assertTrue(inputs['source_summary']['retrieval']['roadmap_context']['used_text_fallback'])
        self.assertEqual(inputs['source_summary']['retrieval']['strategy_context']['match_count'], 0)
        self.assertFalse(inputs['source_summary']['retrieval']['strategy_context']['used_vector_retrieval'])
        self.assertTrue(inputs['source_summary']['retrieval']['strategy_context']['used_text_fallback'])
        self.assertEqual(inputs['source_summary']['retrieval']['role_reference']['match_count'], 0)
        self.assertFalse(inputs['source_summary']['retrieval']['role_reference']['used_vector_retrieval'])
        self.assertTrue(inputs['source_summary']['retrieval']['role_reference']['used_text_fallback'])

    def test_build_blueprint_inputs_prefers_structured_roadmap_analysis_when_available(self):
        workspace = IntakeWorkspace.objects.create(name='Hyperskill', slug='hyperskill-structured')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Fallback roadmap text that should not be re-used when structured analysis exists.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.STRATEGY,
            title='Growth Strategy',
            text='Strategy context stays available as a supplementary signal.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            title='Backend Engineer JD',
            text='Backend engineer owns APIs and integrations.',
        )
        roadmap_analysis = self._create_roadmap_analysis(workspace)
        snapshot = self._create_snapshot(workspace)

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]):
            inputs = _build_blueprint_inputs_sync(workspace.pk, snapshot.pk)

        self.assertEqual(inputs['roadmap_analysis_uuid'], str(roadmap_analysis.uuid))
        self.assertEqual(inputs['roadmap_input_mode'], 'structured')
        self.assertIn('Marketplace Launch', inputs['roadmap_input'])
        self.assertIn('Marketplace API', inputs['roadmap_input'])
        self.assertTrue(inputs['source_summary']['retrieval']['roadmap_context']['used_structured_analysis'])
        self.assertNotIn('Fallback roadmap text that should not be re-used', inputs['evidence_digest'])


class BlueprintLifecycleTests(TestCase):
    def _create_parsed_source(
        self,
        workspace: IntakeWorkspace,
        *,
        source_kind: str,
        title: str,
        text: str,
    ) -> ParsedSource:
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title=title,
            source_kind=source_kind,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=text,
            status=WorkspaceSourceStatus.PARSED,
        )
        return ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=len(text.split()),
            char_count=len(text),
            extracted_text=text,
            metadata={},
        )

    def _create_snapshot(self, workspace: IntakeWorkspace) -> RoleLibrarySnapshot:
        snapshot = RoleLibrarySnapshot.objects.create(
            workspace=workspace,
            status=RoleLibraryStatus.COMPLETED,
            provider='gitlab_handbook',
            summary={
                'canonical_family_counts': {'backend_engineer': 1},
                'normalized_skill_count': 3,
                'alias_count': 2,
                'seed_urls_used': ['https://example.com/backend-engineer'],
            },
        )
        RoleLibraryEntry.objects.create(
            snapshot=snapshot,
            role_name='Backend Engineer',
            department='Engineering',
            role_family='backend_engineer',
            page_url='https://example.com/backend-engineer',
            skills=['Python', 'Django', 'API Design'],
            responsibilities=['Build backend systems'],
            requirements=['Experience with APIs'],
            metadata={
                'canonical_role_family': 'backend_engineer',
                'required_skills': ['Python', 'Django'],
                'desirable_skills': ['API Design'],
                'stakeholder_expectations': ['Cross-functional delivery with product and design'],
                'occupation_reference': {'name_en': 'Backend Software Engineer'},
                'normalized_skills': [
                    {'canonical_key': 'python', 'display_name_en': 'Python', 'display_name_ru': 'Python', 'aliases': ['py']},
                    {'canonical_key': 'django', 'display_name_en': 'Django', 'display_name_ru': 'Django', 'aliases': []},
                    {'canonical_key': 'api-design', 'display_name_en': 'API Design', 'display_name_ru': 'Проектирование API', 'aliases': []},
                ],
            },
        )
        return snapshot

    def _create_roadmap_analysis(self, workspace: IntakeWorkspace) -> RoadmapAnalysisRun:
        return RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            source_summary={'source_count': 1},
            input_snapshot={'analysis_fingerprint': 'fingerprint-1'},
            initiatives=[
                {
                    'id': 'init-marketplace-launch',
                    'name': 'Marketplace Launch',
                    'goal': 'Launch an integration marketplace.',
                    'criticality': 'high',
                    'planned_window': 'Q2 2026',
                    'source_refs': ['roadmap-source'],
                    'confidence': 0.9,
                }
            ],
            workstreams=[
                {
                    'id': 'ws-marketplace-api',
                    'initiative_id': 'init-marketplace-launch',
                    'name': 'Marketplace API',
                    'scope': 'Build partner-facing APIs and billing hooks for marketplace launch.',
                    'delivery_type': 'backend_service',
                    'affected_systems': ['api', 'billing'],
                    'team_shape': {
                        'estimated_headcount': 2,
                        'roles_needed': ['Backend Engineer'],
                        'duration_months': 3,
                    },
                    'required_capabilities': [
                        {'capability': 'Python', 'level': 'advanced', 'criticality': 'high'},
                        {'capability': 'API Design', 'level': 'advanced', 'criticality': 'high'},
                    ],
                    'source_refs': ['roadmap-source'],
                    'confidence': 0.85,
                }
            ],
            capability_bundles=[
                {
                    'bundle_id': 'bundle-backend-platform',
                    'workstream_ids': ['ws-marketplace-api'],
                    'capability_name': 'Backend platform delivery',
                    'capability_type': 'technical',
                    'criticality': 'high',
                    'inferred_role_families': ['backend_engineer'],
                    'skill_hints': ['Python', 'API Design'],
                    'evidence_refs': ['roadmap-source'],
                    'confidence': 0.84,
                }
            ],
            dependencies=[],
            delivery_risks=[],
            prd_summaries=[],
            clarification_questions=[],
        )

    def _build_blueprint_payload(self) -> dict:
        return {
            'company_context': {
                'company_name': 'Hyperskill',
                'what_company_does': 'Learning platform for developers.',
                'why_skills_improvement_now': 'Roadmap execution requires clearer role targets.',
                'products': ['Hyperskill'],
                'customers': ['Developers'],
                'markets': ['Global'],
                'locations': ['Remote'],
                'current_tech_stack': ['Python', 'React'],
                'planned_tech_stack': ['Python', 'React', 'Stripe'],
                'missing_information': [],
            },
            'roadmap_context': [
                {
                    'initiative_id': 'marketplace-launch',
                    'title': 'Marketplace launch',
                    'category': 'growth',
                    'summary': 'Launch a paid marketplace integration in Q2.',
                    'time_horizon': 'Q2 2026',
                    'desired_market_outcome': 'Increase conversion and revenue.',
                    'target_customer_segments': ['B2B learners'],
                    'tech_stack': ['Python', 'React', 'Stripe'],
                    'success_metrics': ['Revenue uplift', 'Activation rate'],
                    'product_implications': ['Billing flow redesign'],
                    'market_implications': ['Clearer GTM packaging'],
                    'functions_required': ['Engineering', 'Product', 'Marketing'],
                    'confidence': 0.82,
                    'ambiguities': ['Pricing owner unclear'],
                    'criticality': 'high',
                }
            ],
            'role_candidates': [
                {
                    'role_name': 'Backend Engineer',
                    'canonical_role_family': 'backend_engineer',
                    'role_family': 'Engineering',
                    'seniority': 'senior',
                    'headcount_needed': 1,
                    'related_initiatives': ['marketplace-launch'],
                    'rationale': 'Own integration APIs and billing backend changes.',
                    'responsibilities': ['Build marketplace APIs', 'Ship billing integrations'],
                    'skills': [
                        {
                            'skill_name_en': 'Python',
                            'skill_name_ru': 'Python',
                            'target_level': 4,
                            'priority': 5,
                            'reason': 'Core delivery skill',
                            'requirement_type': 'core',
                            'criticality': 'high',
                            'supported_initiatives': ['marketplace-launch'],
                            'confidence': 0.9,
                        },
                        {
                            'skill_name_en': 'API Design',
                            'skill_name_ru': 'Проектирование API',
                            'target_level': 4,
                            'priority': 4,
                            'reason': 'Needed for partner integrations',
                            'requirement_type': 'org_specific',
                            'criticality': 'high',
                            'supported_initiatives': ['marketplace-launch'],
                            'confidence': 0.86,
                        },
                    ],
                    'role_already_exists_internally': True,
                    'likely_requires_hiring': False,
                    'confidence': 0.84,
                    'ambiguity_notes': ['Ownership split with platform team'],
                }
            ],
            'clarification_questions': [
                {
                    'question': 'Who owns pricing changes for the marketplace launch?',
                    'scope': 'roadmap',
                    'priority': 'high',
                    'why_it_matters': 'It affects whether product or marketing capacity is required.',
                    'impacted_roles': ['Backend Engineer'],
                    'impacted_initiatives': ['marketplace-launch'],
                }
            ],
            'automation_candidates': [],
            'occupation_map': [
                {
                    'role_name': 'Backend Engineer',
                    'reference_role': 'Backend Software Engineer',
                    'reference_url': 'https://example.com/occupations/backend',
                    'match_reason': 'Curated canonical role-family mapping',
                    'match_score': 92,
                }
            ],
            'assessment_plan': {
                'global_notes': 'Focus on roadmap-critical backend capabilities first.',
                'question_themes': ['Confidence in backend delivery', 'Hidden adjacent skills'],
                'per_employee_question_count': 6,
            },
        }

    def test_generate_skill_blueprint_sets_needs_clarification_and_persists_requirement_metadata(self):
        workspace = IntakeWorkspace.objects.create(name='Hyperskill', slug='hyperskill-stage4')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Launch marketplace integration in Q2 with billing support.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.STRATEGY,
            title='Growth Strategy',
            text='Expand monetization and activation for B2B learners.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            title='Backend Engineer JD',
            text='Backend engineer owns APIs, data contracts, and integrations.',
        )
        snapshot = self._create_snapshot(workspace)

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._extract_blueprint_with_llm',
            new=AsyncMock(return_value=self._build_blueprint_payload()),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            run = async_to_sync(generate_skill_blueprint)(workspace, role_library_snapshot=snapshot)

        run.refresh_from_db()
        self.assertEqual(run.status, BlueprintStatus.NEEDS_CLARIFICATION)
        self.assertEqual(run.role_library_snapshot, snapshot)
        self.assertEqual(run.generation_mode, 'generation')
        self.assertEqual(run.review_summary['clarification_summary']['open'], 1)
        self.assertEqual(run.review_summary['role_candidate_count'], 1)
        self.assertTrue(RoleProfile.objects.filter(workspace=workspace, family='backend_engineer', blueprint_run=run).exists())
        requirement = RoleSkillRequirement.objects.get(
            workspace=workspace,
            role_profile__blueprint_run=run,
            role_profile__family='backend_engineer',
            skill__canonical_key='api-design',
        )
        self.assertEqual(requirement.metadata['requirement_type'], 'org_specific')
        self.assertEqual(requirement.metadata['supported_initiatives'], ['marketplace-launch'])
        self.assertGreater(float(requirement.metadata['confidence']), 0.8)
        self.assertTrue(ClarificationCycle.objects.filter(workspace=workspace, blueprint_run=run).exists())
        self.assertEqual(
            ClarificationQuestion.objects.filter(workspace=workspace, blueprint_run=run).count(),
            1,
        )

    def test_generate_skill_blueprint_links_latest_completed_roadmap_analysis(self):
        workspace = IntakeWorkspace.objects.create(name='Structured Co', slug='structured-co')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Launch marketplace integration in Q2 with billing support.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.STRATEGY,
            title='Growth Strategy',
            text='Expand monetization and activation for B2B learners.',
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            title='Backend Engineer JD',
            text='Backend engineer owns APIs, data contracts, and integrations.',
        )
        roadmap_analysis = self._create_roadmap_analysis(workspace)
        snapshot = self._create_snapshot(workspace)

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._extract_blueprint_with_llm',
            new=AsyncMock(return_value=self._build_blueprint_payload()),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            run = async_to_sync(generate_skill_blueprint)(workspace, role_library_snapshot=snapshot)

        run.refresh_from_db()
        self.assertEqual(run.roadmap_analysis_id, roadmap_analysis.uuid)
        self.assertEqual(run.input_snapshot['roadmap_analysis_uuid'], str(roadmap_analysis.uuid))
        self.assertEqual(run.input_snapshot['roadmap_input_mode'], 'structured')
        self.assertIn('Marketplace API', run.input_snapshot['roadmap_analysis_digest'])

    def test_patch_blueprint_run_creates_derived_run(self):
        workspace = IntakeWorkspace.objects.create(name='Patch Co', slug='patch-co')
        roadmap_analysis = self._create_roadmap_analysis(workspace)
        base_run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.DRAFT,
            roadmap_analysis=roadmap_analysis,
            role_candidates=[
                {
                    'role_name': 'Backend Engineer',
                    'canonical_role_family': 'backend_engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'headcount_needed': 1,
                    'related_initiatives': ['marketplace-launch'],
                    'rationale': 'Initial role set',
                    'responsibilities': ['Build APIs'],
                    'skills': [
                        {
                            'skill_name_en': 'Python',
                            'skill_name_ru': 'Python',
                            'target_level': 4,
                            'priority': 5,
                            'reason': 'Core delivery skill',
                            'requirement_type': 'core',
                            'criticality': 'high',
                            'supported_initiatives': ['marketplace-launch'],
                            'confidence': 0.9,
                        }
                    ],
                    'role_already_exists_internally': True,
                    'likely_requires_hiring': False,
                    'confidence': 0.8,
                    'ambiguity_notes': [],
                }
            ],
            clarification_questions=[],
            source_summary={'counts_by_kind': {'roadmap': 1}},
            input_snapshot={'generation_mode': 'generation'},
        )

        with patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            patched = async_to_sync(patch_blueprint_run)(
                base_run,
                patch_payload={
                    'role_candidates': [
                        {
                            'role_name': 'Backend Engineer',
                            'canonical_role_family': 'backend_engineer',
                            'role_family': 'Engineering',
                            'seniority': 'senior',
                            'headcount_needed': 1,
                            'related_initiatives': ['marketplace-launch'],
                            'rationale': 'Need stronger analytics instrumentation support.',
                            'responsibilities': ['Build APIs', 'Support instrumentation'],
                            'skills': [
                                {
                                    'skill_name_en': 'Python',
                                    'skill_name_ru': 'Python',
                                    'target_level': 4,
                                    'priority': 5,
                                    'reason': 'Core delivery skill',
                                    'requirement_type': 'core',
                                    'criticality': 'high',
                                    'supported_initiatives': ['marketplace-launch'],
                                    'confidence': 0.9,
                                },
                                {
                                    'skill_name_en': 'Product Analytics',
                                    'skill_name_ru': 'Продуктовая аналитика',
                                    'target_level': 3,
                                    'priority': 3,
                                    'reason': 'Needed for launch instrumentation',
                                    'requirement_type': 'adjacent',
                                    'criticality': 'medium',
                                    'supported_initiatives': ['marketplace-launch'],
                                    'confidence': 0.7,
                                },
                            ],
                            'role_already_exists_internally': True,
                            'likely_requires_hiring': False,
                            'confidence': 0.78,
                            'ambiguity_notes': [],
                        }
                    ],
                    'patch_reason': 'Add analytics adjacency',
                    'operator_name': 'Nikita',
                },
            )

        patched.refresh_from_db()
        self.assertEqual(patched.generation_mode, 'patch')
        self.assertEqual(patched.derived_from_run, base_run)
        self.assertEqual(patched.roadmap_analysis_id, roadmap_analysis.uuid)
        self.assertEqual(patched.input_snapshot['generation_mode'], 'patch')
        self.assertEqual(patched.change_log[-1]['event'], 'patch')
        self.assertIn('Product Analytics', [item['skill_name_en'] for item in patched.role_candidates[0]['skills']])

    def test_multiple_runs_can_persist_same_role_name_without_collision(self):
        workspace = IntakeWorkspace.objects.create(name='Repeat Co', slug='repeat-co')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Launch marketplace integration in Q2 with billing support.',
        )
        snapshot = self._create_snapshot(workspace)
        payload = self._build_blueprint_payload()

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._extract_blueprint_with_llm',
            new=AsyncMock(return_value=payload),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            base_run = async_to_sync(generate_skill_blueprint)(workspace, role_library_snapshot=snapshot)

        with patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            patched_run = async_to_sync(patch_blueprint_run)(
                base_run,
                patch_payload={
                    'role_candidates': payload['role_candidates'],
                    'patch_reason': 'Keep the same role names across runs',
                    'operator_name': 'Nikita',
                },
            )

        role_profiles = list(
            RoleProfile.objects.filter(
                workspace=workspace,
                name='Backend Engineer',
                seniority='senior',
            ).order_by('created_at')
        )
        self.assertEqual(len(role_profiles), 2)
        self.assertEqual({profile.blueprint_run_id for profile in role_profiles}, {base_run.pk, patched_run.pk})

    def test_patch_blueprint_run_with_skip_matching_clones_persisted_matches(self):
        workspace = IntakeWorkspace.objects.create(name='Skip Match Co', slug='skip-match-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Example',
            email='alice@example.com',
            current_title='Backend Engineer',
        )
        base_run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.DRAFT,
            role_candidates=[
                {
                    'role_name': 'Backend Engineer',
                    'canonical_role_family': 'backend_engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'headcount_needed': 1,
                    'related_initiatives': ['marketplace-launch'],
                    'rationale': 'Initial role set',
                    'responsibilities': ['Build APIs'],
                    'skills': [
                        {
                            'skill_name_en': 'Python',
                            'skill_name_ru': 'Python',
                            'target_level': 4,
                            'priority': 5,
                            'reason': 'Core delivery skill',
                            'requirement_type': 'core',
                            'criticality': 'high',
                            'supported_initiatives': ['marketplace-launch'],
                            'confidence': 0.9,
                        }
                    ],
                    'role_already_exists_internally': True,
                    'likely_requires_hiring': False,
                    'confidence': 0.8,
                    'ambiguity_notes': [],
                }
            ],
            employee_role_matches=[
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'matches': [
                        {
                            'role_name': 'Backend Engineer',
                            'seniority': 'senior',
                            'fit_score': 88,
                            'reason': 'Strong backend alignment',
                            'related_initiatives': ['marketplace-launch'],
                        }
                    ],
                }
            ],
            clarification_questions=[],
            source_summary={'counts_by_kind': {'roadmap': 1}},
            input_snapshot={'generation_mode': 'generation'},
        )

        patched = async_to_sync(patch_blueprint_run)(
            base_run,
            patch_payload={
                'role_candidates': base_run.role_candidates,
                'patch_reason': 'Carry forward employee matching results',
                'operator_name': 'Nikita',
            },
            skip_employee_matching=True,
        )

        patched.refresh_from_db()
        self.assertEqual(len(patched.employee_role_matches), 1)
        self.assertEqual(patched.employee_role_matches[0]['employee_uuid'], str(employee.uuid))
        persisted_matches = list(
            EmployeeRoleMatch.objects.filter(
                workspace=workspace,
                employee=employee,
                role_profile__blueprint_run=patched,
            ).select_related('role_profile')
        )
        self.assertEqual(len(persisted_matches), 1)
        self.assertEqual(persisted_matches[0].role_profile.name, 'Backend Engineer')

    def test_role_profile_constraints_allow_run_scoped_duplicates_but_protect_null_scope(self):
        workspace = IntakeWorkspace.objects.create(name='Constraint Co', slug='constraint-co')
        run_one = SkillBlueprintRun.objects.create(workspace=workspace, title='Run one', status=BlueprintStatus.DRAFT)
        run_two = SkillBlueprintRun.objects.create(workspace=workspace, title='Run two', status=BlueprintStatus.DRAFT)

        first = RoleProfile.objects.create(
            workspace=workspace,
            blueprint_run=run_one,
            name='Backend Engineer',
            family='backend_engineer',
            seniority='senior',
        )
        second = RoleProfile.objects.create(
            workspace=workspace,
            blueprint_run=run_two,
            name='Backend Engineer',
            family='backend_engineer',
            seniority='senior',
        )

        self.assertNotEqual(first.pk, second.pk)

        RoleProfile.objects.create(
            workspace=workspace,
            blueprint_run=None,
            name='Published Backend Engineer',
            family='backend_engineer',
            seniority='senior',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleProfile.objects.create(
                    workspace=workspace,
                    blueprint_run=None,
                    name='Published Backend Engineer',
                    family='backend_engineer',
                    seniority='senior',
                )

    def test_review_and_approve_blueprint_run_transitions_status(self):
        workspace = IntakeWorkspace.objects.create(name='Review Co', slug='review-co')
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
            roadmap_context=[{'initiative_id': 'initiative-1', 'ambiguities': []}],
            role_candidates=[{'role_name': 'Backend Engineer', 'skills': [], 'ambiguity_notes': []}],
            required_skill_set=[],
            employee_role_matches=[],
            clarification_questions=[
                {
                    'id': 'clar-1',
                    'question': 'Who owns pricing?',
                    'scope': 'roadmap',
                    'priority': 'high',
                    'why_it_matters': 'Role boundaries depend on it.',
                    'impacted_roles': ['Backend Engineer'],
                    'impacted_initiatives': ['initiative-1'],
                    'status': 'open',
                    'answer': '',
                    'note': '',
                }
            ],
        )

        with self.assertRaises(ValueError):
            async_to_sync(approve_blueprint_run)(
                run,
                approver_name='Nikita',
                approval_notes='Approve too early',
                clarification_updates=[],
            )

        reviewed = async_to_sync(review_blueprint_run)(
            run,
            reviewer_name='Nikita',
            review_notes='Resolved ownership.',
            clarification_updates=[
                {
                    'clarification_id': 'clar-1',
                    'answer': 'Product manager owns pricing changes.',
                    'status': 'resolved',
                    'note': '',
                }
            ],
        )

        reviewed.refresh_from_db()
        self.assertEqual(reviewed.status, BlueprintStatus.REVIEWED)
        self.assertEqual(reviewed.reviewed_by, 'Nikita')
        self.assertEqual(reviewed.review_summary['clarification_summary']['open'], 0)

        approved = async_to_sync(approve_blueprint_run)(
            reviewed,
            approver_name='Nikita',
            approval_notes='Approved for downstream stages.',
            clarification_updates=[],
        )

        approved.refresh_from_db()
        self.assertEqual(approved.status, BlueprintStatus.APPROVED)
        self.assertEqual(approved.approved_by, 'Nikita')
        self.assertEqual(approved.change_log[-1]['event'], 'approved')

    def test_get_latest_approved_blueprint_run_returns_latest_approved(self):
        workspace = IntakeWorkspace.objects.create(name='Approval Co', slug='approval-co')
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.DRAFT,
        )
        approved = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Approved blueprint',
            status=BlueprintStatus.APPROVED,
        )

        latest = async_to_sync(get_latest_approved_blueprint_run)(workspace)

        self.assertIsNotNone(latest)
        self.assertEqual(latest.pk, approved.pk)

    def test_answer_blueprint_clarifications_persists_db_rows_and_updates_run(self):
        workspace = IntakeWorkspace.objects.create(name='Clarify Co', slug='clarify-co')
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
            roadmap_context=[{'initiative_id': 'initiative-1', 'ambiguities': []}],
            role_candidates=[{'role_name': 'Backend Engineer', 'skills': [], 'ambiguity_notes': []}],
            required_skill_set=[],
            employee_role_matches=[],
            clarification_questions=[
                {
                    'id': 'clar-1',
                    'question': 'Who owns pricing?',
                    'scope': 'roadmap',
                    'priority': 'high',
                    'why_it_matters': 'Role boundaries depend on it.',
                    'impacted_roles': ['Backend Engineer'],
                    'impacted_initiatives': ['initiative-1'],
                    'status': 'open',
                    'answer': '',
                    'note': '',
                }
            ],
        )
        # Trigger the durable clarification sync through review path.
        reviewed = async_to_sync(review_blueprint_run)(
            run,
            reviewer_name='Nikita',
            review_notes='Keep open for now.',
            clarification_updates=[],
        )
        answered = async_to_sync(answer_blueprint_clarifications)(
            reviewed,
            operator_name='Nikita',
            answer_items=[
                {
                    'clarification_id': 'clar-1',
                    'answer_text': 'Product manager owns pricing changes.',
                    'status': 'accepted',
                    'status_note': 'Confirmed with leadership.',
                    'changed_target_model': True,
                }
            ],
        )

        answered.refresh_from_db()
        question = ClarificationQuestion.objects.get(blueprint_run=answered, question_key='clar-1')
        self.assertEqual(question.status, ClarificationQuestionStatus.ACCEPTED)
        self.assertEqual(question.answered_by, 'Nikita')
        self.assertTrue(question.changed_target_model)
        self.assertEqual(answered.review_summary['clarification_summary']['open'], 0)
        self.assertEqual(answered.status, BlueprintStatus.DRAFT)

    def test_closed_clarification_question_cannot_be_answered_in_place(self):
        workspace = IntakeWorkspace.objects.create(name='Closed Clarify Co', slug='closed-clarify-co')
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.DRAFT,
            clarification_questions=[
                {
                    'id': 'clar-accepted',
                    'question': 'This is already resolved.',
                    'scope': 'roadmap',
                    'priority': 'medium',
                    'why_it_matters': 'Resolution already captured.',
                    'status': 'accepted',
                    'answer': 'Existing answer',
                    'note': 'Already handled.',
                }
            ],
        )
        async_to_sync(review_blueprint_run)(
            run,
            reviewer_name='Nikita',
            review_notes='Sync accepted clarification.',
            clarification_updates=[],
        )

        with self.assertRaises(ValueError):
            async_to_sync(answer_blueprint_clarifications)(
                run,
                operator_name='Nikita',
                answer_items=[
                    {
                        'clarification_id': 'clar-accepted',
                        'answer_text': 'Trying to change a closed clarification.',
                    }
                ],
            )

    def test_approved_run_requires_revision_for_further_clarification_changes(self):
        workspace = IntakeWorkspace.objects.create(name='Approved Clarify Co', slug='approved-clarify-co')
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Approved blueprint',
            status=BlueprintStatus.APPROVED,
            clarification_questions=[
                {
                    'id': 'clar-approved',
                    'question': 'Resolved before approval.',
                    'scope': 'roadmap',
                    'priority': 'medium',
                    'why_it_matters': 'Approval snapshot.',
                    'status': 'accepted',
                    'answer': 'Resolved.',
                    'note': 'Approved baseline.',
                }
            ],
            review_summary={'clarification_summary': {'open': 0}},
        )
        _sync_clarification_cycle_from_run_sync(run.pk)

        with self.assertRaises(ValueError):
            async_to_sync(answer_blueprint_clarifications)(
                run,
                operator_name='Nikita',
                answer_items=[
                    {
                        'clarification_id': 'clar-approved',
                        'answer_text': 'Should require a revision.',
                    }
                ],
            )

        with self.assertRaises(ValueError):
            async_to_sync(review_blueprint_run)(
                run,
                reviewer_name='Nikita',
                review_notes='Should not mutate approved snapshot.',
                clarification_updates=[],
            )

    def test_refresh_blueprint_from_clarifications_creates_derived_run(self):
        workspace = IntakeWorkspace.objects.create(name='Refresh Co', slug='refresh-co')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Launch marketplace integration in Q2 with billing support.',
        )
        roadmap_analysis = self._create_roadmap_analysis(workspace)
        snapshot = self._create_snapshot(workspace)
        payload = self._build_blueprint_payload()

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._extract_blueprint_with_llm',
            new=AsyncMock(return_value=payload),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            base_run = async_to_sync(generate_skill_blueprint)(workspace, role_library_snapshot=snapshot)

        async_to_sync(answer_blueprint_clarifications)(
            base_run,
            operator_name='Nikita',
            answer_items=[
                {
                    'clarification_id': 'who-owns-pricing-changes-for-the-marketplace-launch-1',
                    'answer_text': 'Product owns pricing, marketing owns packaging.',
                    'status': 'accepted',
                    'changed_target_model': True,
                }
            ],
        )

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._refresh_blueprint_from_clarifications_with_llm',
            new=AsyncMock(return_value=payload),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            refreshed = async_to_sync(refresh_blueprint_from_clarifications)(
                base_run,
                operator_name='Nikita',
                refresh_note='Apply clarified pricing ownership.',
            )

        refreshed.refresh_from_db()
        self.assertEqual(refreshed.generation_mode, 'clarification_refresh')
        self.assertEqual(refreshed.derived_from_run, base_run)
        self.assertEqual(refreshed.roadmap_analysis_id, roadmap_analysis.uuid)
        self.assertEqual(
            refreshed.input_snapshot['clarification_refresh']['answered_question_count'],
            1,
        )

    def test_refresh_blueprint_preserves_base_roadmap_analysis_lineage_when_newer_analysis_exists(self):
        workspace = IntakeWorkspace.objects.create(name='Refresh Lineage Co', slug='refresh-lineage-co')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Revenue Roadmap',
            text='Launch marketplace integration in Q2 with billing support.',
        )
        base_analysis = self._create_roadmap_analysis(workspace)
        newer_analysis = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Newer analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            initiatives=[{'id': 'init-other', 'name': 'Other Initiative', 'goal': 'Something else', 'criticality': 'medium', 'planned_window': 'Q3', 'source_refs': [], 'confidence': 0.8}],
            workstreams=[{'id': 'ws-other', 'initiative_id': 'init-other', 'name': 'Other Workstream', 'scope': 'Other scope', 'delivery_type': 'feature_extension', 'affected_systems': [], 'team_shape': {'roles_needed': ['Backend Engineer']}, 'required_capabilities': [], 'confidence': 0.7}],
            capability_bundles=[],
            dependencies=[],
            delivery_risks=[],
            prd_summaries=[],
            clarification_questions=[],
        )
        snapshot = self._create_snapshot(workspace)
        payload = self._build_blueprint_payload()

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._extract_blueprint_with_llm',
            new=AsyncMock(return_value=payload),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            base_run = async_to_sync(generate_skill_blueprint)(workspace, role_library_snapshot=snapshot)

        self.assertEqual(base_run.roadmap_analysis_id, newer_analysis.uuid)
        base_run.roadmap_analysis = base_analysis
        base_run.save(update_fields=['roadmap_analysis', 'updated_at'])

        async_to_sync(answer_blueprint_clarifications)(
            base_run,
            operator_name='Nikita',
            answer_items=[
                {
                    'clarification_id': 'who-owns-pricing-changes-for-the-marketplace-launch-1',
                    'answer_text': 'Product owns pricing.',
                    'status': 'accepted',
                    'changed_target_model': True,
                }
            ],
        )

        with patch('skill_blueprint.services.retrieve_workspace_evidence_sync', return_value=[]), patch(
            'skill_blueprint.services._refresh_blueprint_from_clarifications_with_llm',
            new=AsyncMock(return_value=payload),
        ), patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            refreshed = async_to_sync(refresh_blueprint_from_clarifications)(
                base_run,
                operator_name='Nikita',
                refresh_note='Keep the original roadmap lineage.',
            )

        refreshed.refresh_from_db()
        self.assertEqual(refreshed.roadmap_analysis_id, base_analysis.uuid)
        self.assertEqual(refreshed.input_snapshot['roadmap_analysis_uuid'], str(base_analysis.uuid))
        self.assertIn('Marketplace API', refreshed.input_snapshot['roadmap_analysis_digest'])
        self.assertNotIn('Other Workstream', refreshed.input_snapshot['roadmap_analysis_digest'])

    def test_refresh_excludes_rejected_clarifications_without_operator_input(self):
        workspace = IntakeWorkspace.objects.create(name='Rejected Refresh Co', slug='rejected-refresh-co')
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
            clarification_questions=[
                {
                    'id': 'clar-rejected',
                    'question': 'Question without answer',
                    'scope': 'role',
                    'priority': 'medium',
                    'why_it_matters': 'Should not appear in refresh context.',
                    'status': 'rejected',
                    'answer': '',
                    'note': '',
                }
            ],
        )
        _sync_clarification_cycle_from_run_sync(run.pk)

        answered = _load_answered_clarifications_sync(run.pk)

        self.assertEqual(answered, [])

    def test_publish_and_default_blueprint_selection_prefers_published_run(self):
        workspace = IntakeWorkspace.objects.create(name='Publish Co', slug='publish-co')
        reviewed = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Reviewed blueprint',
            status=BlueprintStatus.REVIEWED,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        published = async_to_sync(publish_blueprint_run)(
            reviewed,
            publisher_name='Nikita',
            publish_notes='Published for downstream stages.',
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Newer draft',
            status=BlueprintStatus.DRAFT,
            clarification_questions=[],
        )

        default_run = async_to_sync(get_default_blueprint_run)(workspace)
        latest_published = async_to_sync(get_latest_published_blueprint_run)(workspace)
        latest_actual = async_to_sync(get_latest_blueprint_run)(workspace)

        self.assertIsNotNone(default_run)
        self.assertEqual(default_run.pk, published.pk)
        self.assertIsNotNone(latest_published)
        self.assertEqual(latest_published.pk, published.pk)
        self.assertIsNotNone(latest_actual)
        self.assertNotEqual(latest_actual.pk, published.pk)
        self.assertEqual(latest_actual.title, 'Newer draft')

        current_published = async_to_sync(get_current_published_blueprint_run)(workspace)
        effective_run = async_to_sync(get_effective_blueprint_run)(workspace)
        self.assertIsNotNone(current_published)
        self.assertEqual(current_published.pk, published.pk)
        self.assertIsNotNone(effective_run)
        self.assertEqual(effective_run.pk, published.pk)

    def test_published_run_is_immutable_and_revision_flow_creates_new_draft(self):
        workspace = IntakeWorkspace.objects.create(name='Immutable Co', slug='immutable-co')
        reviewed = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published baseline',
            status=BlueprintStatus.REVIEWED,
            role_candidates=[
                {
                    'role_name': 'Backend Engineer',
                    'canonical_role_family': 'backend_engineer',
                    'role_family': 'backend_engineer',
                    'seniority': 'senior',
                    'headcount_needed': 1,
                    'related_initiatives': ['marketplace-launch'],
                    'rationale': 'Baseline role',
                    'responsibilities': ['Build APIs'],
                    'skills': [],
                    'role_already_exists_internally': True,
                    'likely_requires_hiring': False,
                    'confidence': 0.8,
                    'ambiguity_notes': [],
                }
            ],
            clarification_questions=[],
            employee_role_matches=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        published = async_to_sync(publish_blueprint_run)(
            reviewed,
            publisher_name='Nikita',
            publish_notes='Baseline publication.',
        )

        with self.assertRaises(ValueError):
            async_to_sync(answer_blueprint_clarifications)(
                published,
                operator_name='Nikita',
                answer_items=[
                    {
                        'clarification_id': 'clar-1',
                        'answer_text': 'Mutating published run should fail.',
                    }
                ],
            )

        with self.assertRaises(ValueError):
            async_to_sync(patch_blueprint_run)(
                published,
                patch_payload={
                    'patch_reason': 'This should require an explicit revision flow.',
                    'operator_name': 'Nikita',
                },
                skip_employee_matching=True,
            )

        with patch(
            'skill_blueprint.services.match_employees_to_roles',
            new=AsyncMock(return_value=[]),
        ):
            revision = async_to_sync(start_blueprint_revision)(
                published,
                operator_name='Nikita',
                revision_reason='Start a safe working copy from the published blueprint.',
            )

        revision.refresh_from_db()
        published.refresh_from_db()
        self.assertEqual(revision.derived_from_run, published)
        self.assertFalse(revision.is_published)
        self.assertEqual(revision.generation_mode, 'patch')
        self.assertTrue(published.is_published)

    def test_open_clarification_queue_scopes_to_latest_mutable_run(self):
        workspace = IntakeWorkspace.objects.create(name='Clarification Queue Co', slug='clarification-queue-co')
        older_run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Older draft',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
            clarification_questions=[
                {
                    'id': 'old-clar',
                    'question': 'Old question',
                    'scope': 'roadmap',
                    'priority': 'high',
                    'why_it_matters': 'Older draft issue.',
                    'status': 'open',
                }
            ],
        )
        newer_run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Newer draft',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
            clarification_questions=[
                {
                    'id': 'new-clar',
                    'question': 'New question',
                    'scope': 'role',
                    'priority': 'medium',
                    'why_it_matters': 'Latest draft issue.',
                    'status': 'open',
                }
            ],
        )

        async_to_sync(review_blueprint_run)(
            older_run,
            reviewer_name='Nikita',
            review_notes='Sync older clarification cycle.',
            clarification_updates=[],
        )
        async_to_sync(review_blueprint_run)(
            newer_run,
            reviewer_name='Nikita',
            review_notes='Sync newer clarification cycle.',
            clarification_updates=[],
        )

        active_run = async_to_sync(get_active_clarification_run)(workspace)
        active_questions = async_to_sync(list_open_clarification_questions)(workspace)
        history_questions = async_to_sync(list_clarification_question_history)(workspace)

        self.assertIsNotNone(active_run)
        self.assertEqual(active_run.pk, newer_run.pk)
        self.assertEqual([question.question_key for question in active_questions], ['new-clar'])
        self.assertEqual(
            {question.question_key for question in history_questions},
            {'old-clar', 'new-clar'},
        )

    def test_blueprint_persistence_keeps_shared_skill_catalog_stable(self):
        workspace = IntakeWorkspace.objects.create(name='Skill Catalog Co', slug='skill-catalog-co')
        existing_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='product-analytics',
            display_name_en='Analytics',
            display_name_ru='Аналитика',
            source='employee_cv',
            metadata={'category': 'cv'},
        )
        run = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.RUNNING,
        )

        _persist_blueprint_payload_sync(
            run.pk,
            {
                'role_candidates': [
                    {
                        'role_name': 'Backend Engineer',
                        'canonical_role_family': 'backend_engineer',
                        'role_family': 'backend_engineer',
                        'seniority': 'senior',
                        'headcount_needed': 1,
                        'related_initiatives': ['marketplace-launch'],
                        'rationale': 'Need stronger analytics support.',
                        'responsibilities': ['Build APIs', 'Support instrumentation'],
                        'skills': [
                            {
                                'skill_name_en': 'Product Analytics',
                                'skill_name_ru': 'Продуктовая аналитика',
                                'target_level': 3,
                                'priority': 3,
                                'reason': 'Needed for launch instrumentation.',
                                'requirement_type': 'adjacent',
                                'criticality': 'medium',
                                'supported_initiatives': ['marketplace-launch'],
                                'confidence': 0.7,
                            }
                        ],
                        'role_already_exists_internally': True,
                        'likely_requires_hiring': False,
                        'confidence': 0.8,
                        'ambiguity_notes': [],
                    }
                ],
                'occupation_map': [],
            },
        )

        existing_skill.refresh_from_db()
        requirement = RoleSkillRequirement.objects.get(
            workspace=workspace,
            role_profile__blueprint_run=run,
            skill=existing_skill,
        )

        self.assertEqual(existing_skill.display_name_en, 'Analytics')
        self.assertEqual(existing_skill.display_name_ru, 'Аналитика')
        self.assertEqual(existing_skill.source, 'employee_cv')
        self.assertEqual(existing_skill.metadata, {'category': 'cv'})
        self.assertFalse('blueprint_run_uuid' in existing_skill.metadata)
        self.assertTrue(
            SkillAlias.objects.filter(skill=existing_skill, alias='Продуктовая аналитика', language_code='ru').exists()
        )
        self.assertEqual(requirement.metadata['reason'], 'Needed for launch instrumentation.')
        self.assertEqual(requirement.metadata['blueprint_run_uuid'], str(run.uuid))

    def test_downstream_service_modules_import_cleanly(self):
        self.assertIsNotNone(import_module('evidence_matrix.services'))
        self.assertIsNotNone(import_module('employee_assessment.services'))
        self.assertIsNotNone(import_module('development_plans.services'))
