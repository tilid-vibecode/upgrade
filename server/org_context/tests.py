from pathlib import Path
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from fastapi import HTTPException

from company_intake.models import (
    IntakeWorkspace,
    WorkspaceSource,
    WorkspaceSourceKind,
    WorkspaceSourceStatus,
    WorkspaceSourceTransport,
)
from server.qdrant_manager import QdrantManager

from .models import (
    CatalogOverrideStatus,
    CatalogResolutionReviewItem,
    ContextProfile,
    Employee,
    EmployeeCVMatchCandidate,
    EmployeeCVProfile,
    EmployeeSkillEvidence,
    EscoOccupation,
    EscoOccupationLabel,
    EscoSkill,
    OccupationResolutionOverride,
    OrgUnit,
    ParsedSource,
    PlanningContext,
    PlanningContextSource,
    Project,
    RoadmapAnalysisRun,
    ReportingLine,
    Skill,
    SkillAlias,
    SkillReviewDecision,
    SkillResolutionOverride,
    SourceChunk,
)
from .prototype_fastapi_views import (
    accept_high_confidence_employee_skills,
    approve_workspace_pending_skill,
    bulk_resolve_workspace_skill_queue,
    bulk_review_employee_skill_evidence,
    create_planning_context,
    create_workspace_project,
    add_planning_context_source,
    clear_workspace_employee_no_cv,
    delete_workspace_employee_view,
    get_latest_roadmap_analysis,
    get_planning_context_detail,
    get_roadmap_analysis_status,
    get_employee_evidence_detail,
    get_parsed_source_detail,
    get_workspace_cv_review_items,
    get_workspace_cv_evidence_status,
    get_workspace_employees_without_cv_evidence,
    list_workspace_pending_skill_queue,
    list_workspace_projects,
    list_planning_contexts,
    get_workspace_unmatched_cvs,
    list_workspace_parsed_sources,
    mark_workspace_employee_no_cv,
    preview_workspace_org_csv_source,
    reparse_workspace_source as reparse_workspace_source_view,
    resolve_workspace_cv_match,
    trigger_roadmap_analysis,
)
from .entities import (
    CVEvidenceBuildRequest,
    CVMatchResolutionRequest,
    EmployeeCvAvailabilityRequest,
    EmployeeSkillAcceptAllRequest,
    EmployeeSkillBulkReviewRequest,
    EmployeeSkillBulkReviewActionRequest,
    OrgCsvPreviewRequest,
    ParsedSourceReparseRequest,
    PlanningContextCreateRequest,
    PlanningContextSourceCreateRequest,
    ProjectCreateRequest,
    PendingSkillApprovalRequest,
    WorkspaceSkillResolutionRequest,
    WorkspaceSkillResolutionRequestItem,
)
from .cv_services import _build_cv_input_revision, build_cv_evidence_for_workspace, rebuild_cv_evidence_for_workspace
from .entities import RoadmapAnalysisRunRequest
from .roadmap_services import (
    CAPABILITY_BUNDLE_SCHEMA,
    INITIATIVE_EXTRACTION_SCHEMA,
    RISK_ANALYSIS_SCHEMA,
    WORKSTREAM_SYNTHESIS_SCHEMA,
    build_roadmap_analysis_status_payload,
    run_roadmap_analysis,
)
from .skill_catalog import normalize_skill_seed, resolve_esco_occupation_sync, resolve_workspace_skill_sync
from .services import (
    ExtractedContent,
    build_chunk_payloads,
    build_org_csv_preview_sync,
    chunk_text,
    clean_supervisor_label,
    extract_html_text,
    infer_csv_mapping,
    infer_csv_mapping_details,
    is_department_lead_marker,
    parse_workspace_source,
    split_projects,
)
from .vector_indexing import (
    build_chunk_document_id,
    index_employee_cv_profile_sync,
    index_parsed_source_chunks_sync,
)


class OrgContextHelpersTests(SimpleTestCase):
    def test_infer_csv_mapping_handles_russian_headers(self):
        headers = ['Логин', 'Имя', 'Роль', 'Подразделение', 'Лид подразделения', 'Проекты']
        mapping = infer_csv_mapping(headers)
        self.assertEqual(mapping['employee_id'], 'Логин')
        self.assertEqual(mapping['full_name'], 'Имя')
        self.assertEqual(mapping['department'], 'Подразделение')
        self.assertEqual(mapping['projects'], 'Проекты')

    def test_infer_csv_mapping_details_flags_ambiguous_headers(self):
        headers = ['Name', 'Employee Name', 'Department']
        mapping_details = infer_csv_mapping_details(headers)

        self.assertEqual(mapping_details['inferred_mapping'].get('department'), 'Department')
        self.assertEqual(
            sorted(mapping_details['ambiguous_targets'].get('full_name', [])),
            ['Employee Name', 'Name'],
        )


class CatalogResolutionTests(TestCase):
    def test_normalize_skill_seed_uses_override_and_registers_review_item_for_freeform_term(self):
        workspace = IntakeWorkspace.objects.create(name='Catalog Review Co', slug='catalog-review-co')
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='analytics',
            normalized_term='analytics',
            canonical_key='product-analytics',
            display_name_en='Product Analytics',
            display_name_ru='',
            aliases=['Analytics'],
            status='approved',
            source='test_override',
            metadata={},
        )

        overridden = normalize_skill_seed('Analytics', workspace=workspace)
        self.assertEqual(overridden['canonical_key'], 'product-analytics')
        self.assertEqual(overridden['display_name_en'], 'Product Analytics')
        self.assertEqual(overridden['match_source'], 'override')

        unresolved = normalize_skill_seed(
            'Edge Data Mesh',
            workspace=workspace,
            review_metadata={'source': 'unit_test'},
        )
        self.assertEqual(unresolved['canonical_key'], 'edge-data-mesh')
        self.assertTrue(unresolved['needs_review'])
        review_item = CatalogResolutionReviewItem.objects.get(
            workspace=workspace,
            term_kind=CatalogResolutionReviewItem.TermKind.SKILL,
            normalized_term='edge data mesh',
        )
        self.assertEqual(review_item.status, 'open')
        self.assertEqual(review_item.metadata['source'], 'unit_test')

        gated = normalize_skill_seed(
            'Edge Data Mesh',
            workspace=workspace,
            review_metadata={'source': 'unit_test'},
            allow_freeform=False,
        )
        self.assertEqual(gated['match_source'], 'review_pending')
        self.assertTrue(gated['needs_review'])

    def test_resolve_workspace_skill_sync_does_not_materialize_review_pending_term(self):
        workspace = IntakeWorkspace.objects.create(name='Catalog Gate Co', slug='catalog-gate-co')

        skill, normalized_skill, is_resolved = resolve_workspace_skill_sync(
            workspace,
            raw_term='Edge Data Mesh',
            created_source='unit_test',
            allow_freeform=False,
        )

        self.assertIsNone(skill)
        self.assertFalse(is_resolved)
        self.assertTrue(normalized_skill['needs_review'])
        self.assertFalse(workspace.skills.exists())  # type: ignore[attr-defined]

    def test_resolve_workspace_skill_sync_clears_review_flag_when_override_resolves_term(self):
        workspace = IntakeWorkspace.objects.create(name='Catalog Override Co', slug='catalog-override-co')
        SkillResolutionOverride.objects.create(
            workspace=workspace,
            raw_term='developed API requirements',
            normalized_term='developed api requirements',
            canonical_key='api-design',
            display_name_en='API Design',
            display_name_ru='Разработка API',
            aliases=['API Design'],
            status=CatalogOverrideStatus.APPROVED,
            source='operator',
            metadata={},
        )

        skill, normalized_skill, is_resolved = resolve_workspace_skill_sync(
            workspace,
            raw_term='API Development',
            normalized_skill={
                'canonical_key': 'api-development',
                'display_name_en': 'API Development',
                'display_name_ru': 'Разработка API',
                'aliases': [],
                'needs_review': True,
            },
            preferred_display_name_ru='Разработка API',
            aliases=['developed API requirements'],
            created_source='unit_test',
            allow_freeform=False,
        )

        self.assertTrue(is_resolved)
        self.assertIsNotNone(skill)
        self.assertEqual(skill.canonical_key, 'api-design')
        self.assertFalse(normalized_skill['needs_review'])

    def test_resolve_esco_occupation_sync_prefers_override_and_otherwise_uses_ranked_matching(self):
        workspace = IntakeWorkspace.objects.create(name='Occupation Match Co', slug='occupation-match-co')
        product_manager = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/product-manager',
            concept_type='Occupation',
            isco_group='2431',
            preferred_label='Product manager',
            normalized_preferred_label='product manager',
            status='released',
            metadata={},
        )
        backend_engineer = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/backend-software-engineer',
            concept_type='Occupation',
            isco_group='2512',
            preferred_label='Backend software engineer',
            normalized_preferred_label='backend software engineer',
            status='released',
            metadata={},
        )
        EscoOccupationLabel.objects.create(
            esco_occupation=product_manager,
            label='Product owner',
            normalized_label='product owner',
            label_kind='alt',
            language_code='en',
        )
        OccupationResolutionOverride.objects.create(
            workspace=workspace,
            raw_term='platform captain',
            normalized_term='platform captain',
            occupation_key='backend-software-engineer',
            occupation_name_en='Backend software engineer',
            aliases=['platform lead'],
            esco_occupation=backend_engineer,
            status='approved',
            source='operator',
            metadata={},
        )
        OccupationResolutionOverride.objects.create(
            workspace=workspace,
            raw_term='product captain',
            normalized_term='product captain',
            occupation_key='',
            occupation_name_en='',
            aliases=['product lead'],
            esco_occupation=None,
            status='approved',
            source='operator',
            metadata={},
        )

        occupation, metadata = resolve_esco_occupation_sync(
            'Platform Captain',
            workspace=workspace,
            role_family_hint='backend_engineer',
        )
        self.assertEqual(occupation, backend_engineer)
        self.assertEqual(metadata['match_source'], 'override')

        ranked_occupation, ranked_metadata = resolve_esco_occupation_sync(
            'Senior Product Manager, Growth',
            workspace=workspace,
            role_family_hint='product_manager',
        )
        self.assertEqual(ranked_occupation, product_manager)
        self.assertEqual(ranked_metadata['match_source'], 'esco_ranked')
        self.assertIn(ranked_metadata['match_confidence'], {'medium', 'high'})
        self.assertTrue(ranked_metadata['candidate_matches'])

        fallback_occupation, fallback_metadata = resolve_esco_occupation_sync(
            'Product Captain',
            workspace=workspace,
            role_family_hint='product_manager',
        )
        self.assertEqual(fallback_occupation, product_manager)
        self.assertEqual(fallback_metadata['match_source'], 'esco_ranked')

    def test_resolve_esco_occupation_sync_uses_extended_family_expansions(self):
        workspace = IntakeWorkspace.objects.create(name='Occupation Expansion Co', slug='occupation-expansion-co')
        support_manager = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/ict-help-desk-manager',
            concept_type='Occupation',
            isco_group='3512',
            preferred_label='ICT help desk manager',
            normalized_preferred_label='ict help desk manager',
            status='released',
            metadata={},
        )
        business_development = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/ict-business-development-manager',
            concept_type='Occupation',
            isco_group='2434',
            preferred_label='ICT business development manager',
            normalized_preferred_label='ict business development manager',
            status='released',
            metadata={},
        )
        software_developer = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/software-developer',
            concept_type='Occupation',
            isco_group='2512',
            preferred_label='software developer',
            normalized_preferred_label='software developer',
            status='released',
            metadata={},
        )
        EscoOccupationLabel.objects.create(
            esco_occupation=support_manager,
            label='technical support manager',
            normalized_label='technical support manager',
            label_kind='alt',
            language_code='en',
        )

        resolved_support, support_metadata = resolve_esco_occupation_sync(
            'Senior Support Manager',
            workspace=workspace,
            role_family_hint='support_manager',
        )
        resolved_business_development, business_development_metadata = resolve_esco_occupation_sync(
            'Business Development Manager',
            workspace=workspace,
            role_family_hint='business_development_manager',
        )
        resolved_founding_engineer, founding_engineer_metadata = resolve_esco_occupation_sync(
            'Founding Engineer',
            workspace=workspace,
            role_family_hint='founding_engineer',
        )

        self.assertEqual(resolved_support, support_manager)
        self.assertEqual(support_metadata['match_source'], 'esco_ranked')
        self.assertEqual(resolved_business_development, business_development)
        self.assertEqual(business_development_metadata['match_source'], 'esco_ranked')
        self.assertEqual(resolved_founding_engineer, software_developer)
        self.assertEqual(founding_engineer_metadata['match_source'], 'esco_ranked')

    def test_bootstrap_catalog_resolution_seeds_default_overrides(self):
        postgres_skill = SkillResolutionOverride.objects.filter(normalized_term='postgres').first()
        self.assertIsNone(postgres_skill)
        product_manager = EscoOccupation.objects.create(
            concept_uri='http://data.europa.eu/esco/occupation/product-manager-bootstrap',
            concept_type='Occupation',
            isco_group='1223',
            preferred_label='Product manager',
            normalized_preferred_label='product manager',
            status='released',
            metadata={},
        )
        out = StringIO()

        call_command('bootstrap_catalog_resolution', stdout=out)

        analytics_override = SkillResolutionOverride.objects.get(workspace__isnull=True, normalized_term='analytics')
        pm_override = OccupationResolutionOverride.objects.get(workspace__isnull=True, normalized_term='pm')
        self.assertEqual(analytics_override.canonical_key, 'product-analytics')
        self.assertEqual(analytics_override.source, 'bootstrap_catalog_resolution')
        self.assertEqual(pm_override.esco_occupation, product_manager)
        self.assertIn('Catalog bootstrap seeded', out.getvalue())

    def test_infer_csv_mapping_details_applies_override(self):
        headers = ['Name', 'Employee Name', 'Department']
        mapping_details = infer_csv_mapping_details(
            headers,
            mapping_override={'full_name': 'Employee Name'},
        )

        self.assertEqual(mapping_details['effective_mapping']['full_name'], 'Employee Name')
        self.assertEqual(mapping_details['override_applied']['full_name'], 'Employee Name')
        self.assertNotIn('full_name', mapping_details['ambiguous_targets'])

    def test_build_org_csv_preview_reports_parseability(self):
        preview = build_org_csv_preview_sync(
            '\n'.join(
                [
                    'Name,Employee Name,Department',
                    'ignored,Alice CEO,Leadership',
                ]
            )
        )

        self.assertFalse(preview['can_parse'])
        self.assertIn("Required CSV mapping target 'full_name' is missing.", preview['warnings'])

    def test_build_org_csv_preview_detects_headerless_org_csv_shape(self):
        preview = build_org_csv_preview_sync(
            '\n'.join(
                [
                    '166,Tad Asher,121,BigProject1,Frontend Developer',
                    '121,Ronald Dahl,112,BigProject1,Product Lead',
                    '112,Nicolas Hansen,,CEO,CEO',
                ]
            )
        )

        self.assertTrue(preview['can_parse'])
        self.assertEqual(preview['effective_mapping']['employee_id'], 'employee_id')
        self.assertEqual(preview['effective_mapping']['full_name'], 'full_name')
        self.assertEqual(preview['effective_mapping']['supervisor_id'], 'supervisor_id')
        self.assertEqual(preview['effective_mapping']['projects'], 'projects')
        self.assertEqual(preview['effective_mapping']['title'], 'title')
        self.assertEqual(preview['sample_rows'][0]['full_name'], 'Tad Asher')

    def test_chunk_text_creates_multiple_chunks_with_overlap(self):
        text = ('Paragraph one. ' * 100) + '\n\n' + ('Paragraph two. ' * 100)
        chunks = list(chunk_text(text, max_chars=400, overlap=50))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.strip() for chunk in chunks))

    def test_clean_supervisor_label_parses_department_lead_marker(self):
        self.assertEqual(clean_supervisor_label('Да — CEO'), 'CEO')
        self.assertEqual(clean_supervisor_label('Jane Doe'), 'Jane Doe')

    def test_is_department_lead_marker_detects_yes_values(self):
        self.assertTrue(is_department_lead_marker('Да'))
        self.assertTrue(is_department_lead_marker('Да — CEO'))
        self.assertFalse(is_department_lead_marker(''))
        self.assertFalse(is_department_lead_marker('Jane Doe'))

    def test_split_projects_handles_semicolon_lists(self):
        projects = split_projects('Alpha; Beta; Gamma')
        self.assertEqual(projects, ['Alpha', 'Beta', 'Gamma'])

    def test_extract_html_text_strips_tags(self):
        extracted = extract_html_text(
            '<html><head><title>Role</title></head><body><nav>Menu</nav><main><h1>Role</h1><p>Hello <b>world</b>.</p><a href="/jd/backend">Backend</a></main></body></html>',
            url='https://example.com',
        )
        self.assertIn('Hello world.', extracted.text)
        self.assertEqual(extracted.metadata['title'], 'Role')
        self.assertEqual(extracted.metadata['link_count'], 1)
        self.assertEqual(extracted.metadata['links'], ['/jd/backend'])

    def test_build_chunk_payloads_uses_page_texts_for_pdf_provenance(self):
        source = WorkspaceSource(
            title='Roadmap PDF',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
        )
        extracted = ExtractedContent(
            text='Page 1 text\n\nPage 2 text',
            content_type='application/pdf',
            page_count=2,
            metadata={
                'title': 'Roadmap PDF',
                'page_texts': [
                    {'page_number': 1, 'text': 'Q2 roadmap\n\nPage one roadmap'},
                    {'page_number': 2, 'text': 'Rollout plan\n\nPage two rollout'},
                ],
            },
        )

        chunk_payloads = build_chunk_payloads(
            source=source,
            extracted=extracted,
            source_origin={'transport': WorkspaceSourceTransport.INLINE_TEXT},
            language_code='en',
        )

        self.assertEqual(len(chunk_payloads), 2)
        self.assertEqual(chunk_payloads[0]['metadata']['page_number'], 1)
        self.assertEqual(chunk_payloads[0]['metadata']['section_heading'], 'Q2 roadmap')
        self.assertEqual(chunk_payloads[1]['metadata']['page_number'], 2)
        self.assertEqual(chunk_payloads[1]['metadata']['section_heading'], 'Rollout plan')


class QdrantManagerSearchTests(SimpleTestCase):
    def test_search_sync_respects_explicit_zero_thresholds(self):
        manager = QdrantManager(
            config={},
            context_config={
                'COLLECTION_NAME': 'test_collection',
                'VECTOR_SIZE': 3,
                'DEFAULT_TOP_K': 10,
                'MIN_SCORE': 0.3,
            },
        )
        client = Mock()
        client.query_points.return_value = SimpleNamespace(points=[])
        manager._sync_client = client

        manager.search_sync(
            org_id='workspace-1',
            query_vector=[0.1, 0.2, 0.3],
            top_k=0,
            min_score=0.0,
        )

        kwargs = client.query_points.call_args.kwargs
        self.assertEqual(kwargs['limit'], 0)
        self.assertEqual(kwargs['score_threshold'], 0.0)

    def test_search_async_respects_explicit_zero_thresholds(self):
        manager = QdrantManager(
            config={},
            context_config={
                'COLLECTION_NAME': 'test_collection',
                'VECTOR_SIZE': 3,
                'DEFAULT_TOP_K': 10,
                'MIN_SCORE': 0.3,
            },
        )
        client = AsyncMock()
        client.query_points = AsyncMock(return_value=SimpleNamespace(points=[]))
        manager._async_client = client

        async_to_sync(manager.search)(
            org_id='workspace-1',
            query_vector=[0.1, 0.2, 0.3],
            top_k=0,
            min_score=0.0,
        )

        kwargs = client.query_points.call_args.kwargs
        self.assertEqual(kwargs['limit'], 0)
        self.assertEqual(kwargs['score_threshold'], 0.0)


class OrgContextOrgCsvFixtureTests(TestCase):
    def test_build_chunk_document_id_is_deterministic(self):
        chunk_id = build_chunk_document_id(
            workspace_uuid='workspace-123',
            source_uuid='source-456',
            chunk_index=3,
        )
        self.assertEqual(
            chunk_id,
            'workspace:workspace-123:source:source-456:chunk:3',
        )

    def test_parse_workspace_source_records_vector_index_metadata(self):
        workspace = IntakeWorkspace.objects.create(
            name='Index Co',
            slug='index-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Q2 roadmap initiative.\n\nShip new growth features.',
        )

        with patch(
            'org_context.services.index_parsed_source_chunks_sync',
            return_value={
                'status': 'indexed',
                'doc_type': 'roadmap_context',
                'indexed_chunk_count': 1,
                'index_version': 'stage1.1-v1',
            },
        ):
            result = async_to_sync(parse_workspace_source)(source)

        source.refresh_from_db()
        parsed_source = ParsedSource.objects.get(source=source)
        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.parse_metadata.get('vector_index', {}).get('status'), 'indexed')
        self.assertEqual(parsed_source.metadata.get('vector_index', {}).get('doc_type'), 'roadmap_context')

    def test_parse_workspace_source_records_stage_two_metadata_contract(self):
        workspace = IntakeWorkspace.objects.create(
            name='Metadata Co',
            slug='metadata-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Launch expansion roadmap.\n\nImprove activation flows.',
        )

        result = async_to_sync(parse_workspace_source)(source)
        source.refresh_from_db()
        parsed_source = ParsedSource.objects.get(source=source)

        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.parse_metadata.get('metadata_schema_version'), 'stage2-v1')
        self.assertEqual(source.parse_metadata.get('parser', {}).get('version'), '2.0')
        self.assertEqual(source.parse_metadata.get('parser', {}).get('extraction_method'), 'builtin_extractor')
        self.assertEqual(source.parse_metadata.get('source', {}).get('kind'), WorkspaceSourceKind.ROADMAP)
        self.assertEqual(source.parse_metadata.get('content', {}).get('chunk_count'), 1)
        self.assertEqual(source.parse_metadata.get('content', {}).get('section_count'), 1)
        self.assertIn('parsed_at', source.parse_metadata.get('timing', {}))
        self.assertEqual(parsed_source.parser_name, 'builtin_text')
        self.assertEqual(parsed_source.parser_version, '2.0')

    def test_index_parsed_source_chunks_sync_fails_fast_on_dimension_mismatch(self):
        workspace = IntakeWorkspace.objects.create(
            name='Vector Co',
            slug='vector-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Launch pricing experiments.',
        )
        parsed_source = ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=3,
            char_count=27,
            extracted_text='Launch pricing experiments.',
            metadata={},
        )
        SourceChunk.objects.create(
            parsed_source=parsed_source,
            chunk_index=1,
            text='Launch pricing experiments.',
            char_count=27,
            metadata={'chunk_family': 'roadmap_context'},
        )

        fake_embedding_manager = Mock()
        fake_embedding_manager.model_name = 'text-embedding-3-small'
        fake_embedding_manager.dimensions = 2
        fake_embedding_manager.embed_batch_sync.return_value = [[0.1, 0.2]]

        fake_qdrant = Mock()
        fake_qdrant.vector_size = 3
        fake_qdrant.collection_name = 'org_context_documents'
        fake_qdrant.delete_by_filters_sync.return_value = True

        with patch('org_context.vector_indexing.get_embedding_manager_sync', return_value=fake_embedding_manager), patch(
            'org_context.vector_indexing.get_qdrant_manager_sync',
            return_value=fake_qdrant,
        ):
            result = index_parsed_source_chunks_sync(parsed_source.pk)

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'embedding_dimension_mismatch')
        self.assertEqual(result['expected_vector_size'], 3)
        self.assertEqual(result['actual_vector_sizes'], [2])
        fake_qdrant.upsert_documents_batch_sync.assert_not_called()

    def test_index_parsed_source_chunks_sync_includes_page_and_section_payload(self):
        workspace = IntakeWorkspace.objects.create(
            name='Payload Co',
            slug='payload-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Launch pricing experiments.',
        )
        parsed_source = ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=3,
            char_count=27,
            extracted_text='Launch pricing experiments.',
            metadata={},
        )
        SourceChunk.objects.create(
            parsed_source=parsed_source,
            chunk_index=1,
            text='Launch pricing experiments.',
            char_count=27,
            metadata={
                'chunk_family': 'roadmap_context',
                'section_index': 2,
                'section_heading': 'Q2 roadmap',
                'page_number': 4,
            },
        )

        fake_embedding_manager = Mock()
        fake_embedding_manager.model_name = 'text-embedding-3-small'
        fake_embedding_manager.dimensions = 3
        fake_embedding_manager.embed_batch_sync.return_value = [[0.1, 0.2, 0.3]]

        fake_qdrant = Mock()
        fake_qdrant.vector_size = 3
        fake_qdrant.collection_name = 'org_context_documents'
        fake_qdrant.delete_by_filters_sync.return_value = True
        fake_qdrant.upsert_documents_batch_sync.return_value = 1

        with patch('org_context.vector_indexing.get_embedding_manager_sync', return_value=fake_embedding_manager), patch(
            'org_context.vector_indexing.get_qdrant_manager_sync',
            return_value=fake_qdrant,
        ):
            result = index_parsed_source_chunks_sync(parsed_source.pk)

        self.assertEqual(result['status'], 'indexed')
        document = fake_qdrant.upsert_documents_batch_sync.call_args.args[0][0]
        self.assertEqual(document['payload']['section_index'], 2)
        self.assertEqual(document['payload']['section_heading'], 'Q2 roadmap')
        self.assertEqual(document['payload']['page_number'], 4)

    def test_parse_workspace_source_surfaces_failure_metadata(self):
        workspace = IntakeWorkspace.objects.create(
            name='Failure Co',
            slug='failure-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='legacy.doc',
            source_kind=WorkspaceSourceKind.OTHER,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
        )

        with patch(
            'org_context.services.load_source_bytes',
            new=AsyncMock(return_value=(b'bad-doc', 'application/msword', 'legacy.doc')),
        ):
            result = async_to_sync(parse_workspace_source)(source)

        source.refresh_from_db()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(source.status, 'failed')
        self.assertTrue(source.parse_metadata.get('failure', {}).get('retryable'))
        self.assertIn('convert the file to .docx or PDF', source.parse_error)

    def test_parse_workspace_source_inline_text_persists_rich_chunk_metadata(self):
        workspace = IntakeWorkspace.objects.create(
            name='Inline Co',
            slug='inline-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Q2 roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Launch platform refresh.\n\nImprove onboarding analytics.',
            language_code='ru',
        )

        result = async_to_sync(parse_workspace_source)(source)
        source.refresh_from_db()
        chunk = SourceChunk.objects.get(parsed_source__source=source, chunk_index=1)

        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.status, 'parsed')
        self.assertEqual(source.parse_metadata.get('language_code'), 'ru')
        self.assertEqual(chunk.metadata.get('chunk_family'), 'roadmap_context')
        self.assertEqual(chunk.metadata.get('language_code'), 'ru')
        self.assertEqual(chunk.metadata.get('transport'), WorkspaceSourceTransport.INLINE_TEXT)
        self.assertEqual(chunk.metadata.get('section_index'), 1)
        self.assertEqual(chunk.metadata.get('section_heading'), 'Q2 roadmap')

    def test_parse_workspace_source_persists_org_csv_provenance(self):
        workspace = IntakeWorkspace.objects.create(
            name='Provenance Co',
            slug='provenance-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    'Логин,Имя,Роль,Подразделение,Лид подразделения,Проекты',
                    'ceo,Alice CEO,CEO,Leadership,Да,North Star',
                    'dev,Carol Dev,Backend Engineer,Leadership,,North Star',
                ]
            ),
            language_code='ru',
        )

        result = async_to_sync(parse_workspace_source)(source)

        self.assertEqual(result['status'], 'parsed')
        employee = Employee.objects.get(workspace=workspace, full_name='Carol Dev')
        org_unit = OrgUnit.objects.get(workspace=workspace, name='Leadership')
        project = Project.objects.get(workspace=workspace, name='North Star')
        reporting_line = ReportingLine.objects.get(workspace=workspace, report=employee)

        self.assertEqual(employee.metadata['org_csv_provenance']['row_index'], 3)
        self.assertEqual(employee.metadata['org_csv_provenance']['fields']['full_name'], 'Carol Dev')
        self.assertEqual(org_unit.metadata['org_csv_row_examples'][0]['fields']['department'], 'Leadership')
        self.assertEqual(project.metadata['org_csv_row_examples'][0]['fields']['projects'], 'North Star')
        self.assertEqual(reporting_line.metadata['org_csv_provenance']['fields']['department'], 'Leadership')

    def test_parse_workspace_source_external_url_uses_fetcher(self):
        workspace = IntakeWorkspace.objects.create(
            name='URL Co',
            slug='url-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy page',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.EXTERNAL_URL,
            external_url='https://example.com/strategy',
        )

        with patch(
            'org_context.services.fetch_external_url',
            new=AsyncMock(
                return_value=ExtractedContent(
                    text='Platform strategy with three bets.',
                    content_type='text/html',
                    metadata={
                        'title': 'Strategy',
                        'final_url': 'https://example.com/final-strategy',
                    },
                )
            ),
        ):
            result = async_to_sync(parse_workspace_source)(source)

        source.refresh_from_db()
        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.parse_metadata.get('title'), 'Strategy')
        # Prefer the structured path; fall back to flat key for backward compatibility.
        source_origin = (
            (source.parse_metadata.get('source') or {}).get('origin')
            or source.parse_metadata.get('source_origin')
            or {}
        )
        self.assertEqual(
            source_origin.get('external_url'),
            'https://example.com/strategy',
        )

    def test_parse_workspace_source_accepts_org_csv_mapping_override(self):
        workspace = IntakeWorkspace.objects.create(
            name='Override Co',
            slug='override-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    'Name,Employee Name,Department',
                    'ignored,Alice CEO,Leadership',
                ]
            ),
        )

        result = async_to_sync(parse_workspace_source)(
            source,
            mapping_override={'full_name': 'Employee Name'},
        )
        source.refresh_from_db()

        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.parse_metadata['org_import']['override_applied']['full_name'], 'Employee Name')
        self.assertTrue(Employee.objects.filter(workspace=workspace, full_name='Alice CEO').exists())

    def test_parse_workspace_source_imports_headerless_org_csv(self):
        workspace = IntakeWorkspace.objects.create(
            name='Headerless Org Co',
            slug='headerless-org-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    '166,Tad Asher,121,BigProject1,Frontend Developer',
                    '121,Ronald Dahl,112,BigProject1,Product Lead',
                    '112,Nicolas Hansen,,CEO,CEO',
                ]
            ),
        )

        result = async_to_sync(parse_workspace_source)(source)
        source.refresh_from_db()

        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(Employee.objects.filter(workspace=workspace).count(), 3)
        tad = Employee.objects.get(workspace=workspace, external_employee_id='166')
        self.assertEqual(tad.full_name, 'Tad Asher')
        self.assertEqual(tad.current_title, 'Frontend Developer')
        self.assertEqual(ReportingLine.objects.filter(source=source).count(), 2)
        self.assertEqual(source.parse_metadata['org_import']['column_mapping']['full_name'], 'full_name')

    def test_force_reparse_cleans_stale_source_owned_entities(self):
        initial_csv = '\n'.join(
            [
                'Логин,Имя,Роль,Подразделение,Лид подразделения,Проекты',
                'ceo,Alice CEO,CEO,Leadership,Да,Legacy',
                'lead,Bob Lead,Head of Engineering,Engineering,Да — CEO,Legacy',
                'dev,Carol Dev,Backend Engineer,Engineering,,Legacy',
            ]
        )
        updated_csv = '\n'.join(
            [
                'Логин,Имя,Роль,Подразделение,Лид подразделения,Проекты',
                'ceo,Alice CEO,CEO,Leadership,Да,',
                'dev,Carol Dev,Backend Engineer,Platform,Да — CEO,',
            ]
        )

        workspace = IntakeWorkspace.objects.create(
            name='Reparse Co',
            slug='reparse-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=initial_csv,
            language_code='ru',
        )

        first_result = async_to_sync(parse_workspace_source)(source)
        self.assertEqual(first_result['status'], 'parsed')
        self.assertEqual(Employee.objects.filter(workspace=workspace).count(), 3)
        self.assertTrue(OrgUnit.objects.filter(workspace=workspace, name='Engineering').exists())
        self.assertTrue(Project.objects.filter(workspace=workspace, name='Legacy').exists())

        source.inline_text = updated_csv
        source.save(update_fields=['inline_text', 'updated_at'])

        second_result = async_to_sync(parse_workspace_source)(source, force=True)
        source.refresh_from_db()

        self.assertEqual(second_result['status'], 'parsed')
        self.assertEqual(source.status, 'parsed')
        self.assertEqual(source.parse_metadata.get('org_import', {}).get('employees_deleted'), 1)
        self.assertEqual(Employee.objects.filter(workspace=workspace).count(), 2)
        self.assertFalse(Employee.objects.filter(workspace=workspace, full_name='Bob Lead').exists())
        self.assertFalse(OrgUnit.objects.filter(workspace=workspace, name='Engineering').exists())
        self.assertFalse(Project.objects.filter(workspace=workspace, name='Legacy').exists())
        self.assertTrue(OrgUnit.objects.filter(workspace=workspace, name='Platform').exists())

        carol = Employee.objects.get(workspace=workspace, full_name='Carol Dev')
        leadership = OrgUnit.objects.get(workspace=workspace, name='Leadership')
        self.assertEqual(carol.source, source)
        self.assertEqual(carol.metadata.get('language_code'), 'ru')
        self.assertEqual(leadership.metadata.get('language_code'), 'ru')

    def test_org_csv_row_count_ignores_blank_rows(self):
        workspace = IntakeWorkspace.objects.create(
            name='Row Count Co',
            slug='row-count-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    'Логин,Имя,Роль,Подразделение,Лид подразделения,Проекты',
                    'ceo,Alice CEO,CEO,Leadership,Да,',
                    ',,,,,',
                    '',
                ]
            ),
        )

        result = async_to_sync(parse_workspace_source)(source)
        source.refresh_from_db()
        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.parse_metadata.get('org_import', {}).get('row_count'), 1)

    def test_parse_workspace_source_imports_real_org_fixture(self):
        fixture_path = Path(__file__).resolve().parent / 'testdata' / 'org_final.csv'
        csv_text = fixture_path.read_text(encoding='utf-8')

        workspace = IntakeWorkspace.objects.create(
            name='Fixture Co',
            slug='fixture-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org_final.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=csv_text,
        )

        result = async_to_sync(parse_workspace_source)(source)
        source.refresh_from_db()

        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(source.status, 'parsed')
        self.assertTrue(ParsedSource.objects.filter(source=source).exists())

        org_import = source.parse_metadata.get('org_import', {})
        self.assertEqual(org_import.get('row_count'), 143)
        self.assertEqual(org_import.get('org_unit_count'), 11)
        self.assertEqual(org_import.get('department_lead_count'), 10)
        self.assertGreater(org_import.get('reporting_lines_created', 0), 100)
        self.assertGreater(org_import.get('inferred_reporting_lines_created', 0), 100)

        self.assertEqual(Employee.objects.filter(workspace=workspace).count(), 143)
        self.assertEqual(OrgUnit.objects.filter(workspace=workspace).count(), 11)
        self.assertGreater(ReportingLine.objects.filter(workspace=workspace).count(), 100)

        backend = OrgUnit.objects.get(workspace=workspace, name='Бэкенд-разработка')
        self.assertEqual(backend.metadata.get('leader_name'), 'Дмитрий Шишкин')


class OrgContextPrototypeApiTests(TestCase):
    def test_list_parsed_sources_returns_workspace_entries(self):
        workspace = IntakeWorkspace.objects.create(
            name='Parsed List Co',
            slug='parsed-list-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Grow enterprise adoption.\n\nExpand onboarding support.',
        )
        async_to_sync(parse_workspace_source)(source)

        response = async_to_sync(list_workspace_parsed_sources)(workspace.slug)

        self.assertEqual(response.workspace_slug, workspace.slug)
        self.assertEqual(len(response.parsed_sources), 1)
        self.assertEqual(response.parsed_sources[0].source_kind, WorkspaceSourceKind.STRATEGY)
        self.assertEqual(response.parsed_sources[0].chunk_count, 1)
        self.assertEqual(response.parsed_sources[0].parser_version, '2.0')

    def test_get_parsed_source_detail_returns_chunks_and_source(self):
        workspace = IntakeWorkspace.objects.create(
            name='Parsed Detail Co',
            slug='parsed-detail-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Launch marketplace integration.\n\nImprove reporting.',
        )
        async_to_sync(parse_workspace_source)(source)
        parsed_source = ParsedSource.objects.get(source=source)

        response = async_to_sync(get_parsed_source_detail)(workspace.slug, parsed_source.uuid)

        self.assertEqual(response.workspace_slug, workspace.slug)
        self.assertEqual(response.parsed_source.uuid, parsed_source.uuid)
        self.assertEqual(response.source.uuid, source.uuid)
        self.assertIn('Launch marketplace integration', response.extracted_text)
        self.assertEqual(len(response.chunks), 1)
        self.assertEqual(response.chunks[0].metadata.get('chunk_family'), 'roadmap_context')

    def test_reparse_workspace_source_refreshes_parsed_payload(self):
        workspace = IntakeWorkspace.objects.create(
            name='Reparse API Co',
            slug='reparse-api-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Old strategy draft.',
        )
        async_to_sync(parse_workspace_source)(source)

        source.inline_text = 'New strategy draft with enterprise expansion.'
        source.save(update_fields=['inline_text', 'updated_at'])

        response = async_to_sync(reparse_workspace_source_view)(workspace.slug, source.uuid)
        parsed_source = ParsedSource.objects.get(source=source)

        self.assertEqual(response.status, 'parsed')
        self.assertEqual(response.source.uuid, source.uuid)
        self.assertIsNotNone(response.parsed_source)
        self.assertIn('enterprise expansion', parsed_source.extracted_text)
        self.assertEqual(response.parsed_source.parser_version, '2.0')

    def test_preview_workspace_org_csv_source_returns_mapping_diagnostics(self):
        workspace = IntakeWorkspace.objects.create(
            name='Preview API Co',
            slug='preview-api-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    'Name,Employee Name,Department',
                    'ignored,Alice CEO,Leadership',
                ]
            ),
        )

        response = async_to_sync(preview_workspace_org_csv_source)(
            workspace.slug,
            source.uuid,
            OrgCsvPreviewRequest(mapping_override={'full_name': 'Employee Name'}),
        )

        self.assertEqual(response.workspace_slug, workspace.slug)
        self.assertEqual(response.effective_mapping['full_name'], 'Employee Name')
        self.assertEqual(response.override_applied['full_name'], 'Employee Name')
        self.assertTrue(response.can_parse)

    def test_reparse_workspace_source_applies_mapping_override(self):
        workspace = IntakeWorkspace.objects.create(
            name='Reparse Override API Co',
            slug='reparse-override-api-co',
        )
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='org.csv',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='\n'.join(
                [
                    'Name,Employee Name,Department',
                    'ignored,Alice CEO,Leadership',
                ]
            ),
        )

        response = async_to_sync(reparse_workspace_source_view)(
            workspace.slug,
            source.uuid,
            ParsedSourceReparseRequest(mapping_override={'full_name': 'Employee Name'}),
        )

        self.assertEqual(response.status, 'parsed')
        self.assertEqual(response.parse_metadata['org_import']['override_applied']['full_name'], 'Employee Name')
        self.assertTrue(Employee.objects.filter(workspace=workspace, full_name='Alice CEO').exists())


class CVEvidenceStageTests(TestCase):
    def setUp(self):
        super().setUp()
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='Python',
            normalized_term='python',
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            aliases=['py'],
            status='approved',
            source='test_override',
            metadata={},
        )
        SkillResolutionOverride.objects.create(
            workspace=None,
            raw_term='Analytics',
            normalized_term='analytics',
            canonical_key='product-analytics',
            display_name_en='Product Analytics',
            display_name_ru='',
            aliases=['product metrics'],
            status='approved',
            source='test_override',
            metadata={},
        )

    def _create_cv_source(self, workspace, *, title='Employee CV', inline_text='CV text', language_code='en'):
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title=title,
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=inline_text,
            language_code=language_code,
            status=WorkspaceSourceStatus.PARSED,
        )
        ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=len(inline_text.split()),
            char_count=len(inline_text),
            extracted_text=inline_text,
            metadata={'language_code': language_code},
        )
        return source

    def _cv_payload(self, **overrides):
        payload = {
            'candidate_name': 'Alice Doe',
            'email': 'alice@example.com',
            'headline': 'Senior Backend Engineer',
            'summary': 'Built APIs and analytics instrumentation.',
            'seniority': 'senior',
            'current_role': 'Senior Backend Engineer',
            'role_family': 'backend_engineer',
            'current_department': 'Engineering',
            'languages': ['English'],
            'warnings': [],
            'sparse_cv': False,
            'sparse_reason': '',
            'skills': [
                {
                    'skill_name_en': 'Python',
                    'skill_name_ru': 'Python',
                    'original_term': 'Python',
                    'level': 4,
                    'confidence': 90,
                    'category': 'core',
                    'aliases': ['py'],
                    'evidence': 'Built backend APIs and services in Python.',
                }
            ],
            'role_history': [
                {
                    'company_name': 'Example Corp',
                    'role_title': 'Backend Engineer',
                    'start_date': '2022',
                    'end_date': '2025',
                    'responsibilities': ['Built internal APIs'],
                    'achievements': ['Reduced API latency'],
                    'domains': ['B2B SaaS'],
                    'leadership_signals': ['Mentored peers'],
                    'evidence': 'Owned service APIs and delivery.',
                    'confidence': 85,
                }
            ],
            'achievements': [
                {
                    'summary': 'Reduced API latency',
                    'evidence': 'Optimized bottlenecks in API services.',
                    'confidence': 84,
                }
            ],
            'domain_experience': [
                {
                    'domain': 'B2B SaaS',
                    'evidence': 'Worked on B2B platform integrations.',
                    'confidence': 78,
                }
            ],
            'leadership_signals': [
                {
                    'signal': 'Mentoring',
                    'evidence': 'Mentored junior developers.',
                    'confidence': 72,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_build_cv_evidence_matches_existing_employee_by_email_without_duplicates(self):
        workspace = IntakeWorkspace.objects.create(name='CV Match Co', slug='cv-match-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe CV')

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 4},
        ):
            result = async_to_sync(build_cv_evidence_for_workspace)(workspace)

        employee.refresh_from_db()
        source.refresh_from_db()
        profile = EmployeeCVProfile.objects.get(source=source)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(Employee.objects.filter(workspace=workspace).count(), 1)
        self.assertEqual(profile.status, EmployeeCVProfile.Status.MATCHED)
        self.assertEqual(profile.employee, employee)
        self.assertEqual(profile.matched_by, 'email_exact')
        self.assertEqual(EmployeeSkillEvidence.objects.filter(employee=employee, source=source).count(), 1)
        self.assertEqual(source.parse_metadata['cv_evidence']['status'], EmployeeCVProfile.Status.MATCHED)
        self.assertEqual(source.parse_metadata['cv_evidence']['vector_index']['status'], 'indexed')

    def test_build_cv_evidence_flags_ambiguous_match_and_attaches_no_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='CV Ambiguous Co', slug='cv-ambiguous-co')
        Employee.objects.create(workspace=workspace, full_name='Alex Kim', email='alex.one@example.com', current_title='Engineer')
        Employee.objects.create(workspace=workspace, full_name='Alex Kim', email='alex.two@example.com', current_title='Engineer')
        source = self._create_cv_source(workspace, title='Alex Kim CV')

        ambiguous_payload = self._cv_payload(
            candidate_name='Alex Kim',
            email='',
            current_role='Engineer',
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=ambiguous_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed'},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile = EmployeeCVProfile.objects.get(source=source)
        self.assertEqual(profile.status, EmployeeCVProfile.Status.AMBIGUOUS)
        self.assertIsNone(profile.employee)
        self.assertEqual(EmployeeSkillEvidence.objects.filter(source=source).count(), 0)
        self.assertGreaterEqual(len(profile.metadata.get('candidate_matches', [])), 2)

    def test_build_cv_evidence_normalizes_skill_aliases_and_preserves_provenance(self):
        workspace = IntakeWorkspace.objects.create(name='CV Skill Co', slug='cv-skill-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Analyst',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe analytics CV')
        existing_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='product-analytics',
            display_name_en='Product Analytics',
            display_name_ru='Продуктовая аналитика',
            source='role_library_seed',
            metadata={'seeded': True},
        )

        analytics_payload = self._cv_payload(
            current_role='Product Analyst',
            role_family='data_product_analyst',
            skills=[
                {
                    'skill_name_en': 'Analytics',
                    'skill_name_ru': 'Продуктовая аналитика',
                    'original_term': 'Analytics',
                    'level': 3,
                    'confidence': 80,
                    'category': 'analytics',
                    'aliases': ['product metrics'],
                    'evidence': 'Owned funnel analytics and product metrics.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=analytics_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        existing_skill.refresh_from_db()
        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)
        self.assertEqual(existing_skill.source, 'role_library_seed')
        self.assertEqual(evidence.skill.canonical_key, 'product-analytics')
        self.assertIn('Analytics', evidence.metadata['original_terms'])
        self.assertEqual(evidence.metadata['source_uuid'], str(source.uuid))
        self.assertFalse(
            SkillAlias.objects.filter(skill=existing_skill, alias='product metrics').exists()
        )

    def test_build_cv_evidence_persists_esco_skill_ids_as_strings_in_json_payloads(self):
        workspace = IntakeWorkspace.objects.create(name='CV ESCO Co', slug='cv-esco-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Architect',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe solution architecture CV')
        esco_skill = EscoSkill.objects.create(
            concept_uri='http://data.europa.eu/esco/skill/solution-architecture',
            concept_type='KnowledgeSkillCompetence',
            skill_type='skill/competence',
            preferred_label='Solution architecture',
            normalized_preferred_label='solution architecture',
            status='released',
            metadata={},
        )
        esco_payload = self._cv_payload(
            current_role='Architect',
            headline='Solution Architect',
            skills=[
                {
                    'skill_name_en': 'Solution Architecture',
                    'skill_name_ru': '',
                    'original_term': 'Solution Architecture',
                    'level': 4,
                    'confidence': 88,
                    'category': 'core',
                    'aliases': ['architecture design'],
                    'evidence': 'Designed service boundaries and target architectures.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=esco_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile = EmployeeCVProfile.objects.get(source=source)
        skill_payload = profile.extracted_payload['skills'][0]
        self.assertEqual(skill_payload['esco_skill_id'], str(esco_skill.pk))
        self.assertIsInstance(skill_payload['esco_skill_id'], str)
        self.assertEqual(skill_payload['esco_skill_uri'], esco_skill.concept_uri)
        self.assertEqual(EmployeeSkillEvidence.objects.filter(employee=employee, source=source).count(), 1)

    def test_build_cv_evidence_reuses_fresh_profile_without_reextracting(self):
        workspace = IntakeWorkspace.objects.create(name='CV Fresh Co', slug='cv-fresh-co')
        Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        self._create_cv_source(workspace, title='Alice Doe CV')

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={
                'status': 'indexed',
                'indexed_document_count': 1,
                'index_version': 'stage6-v1',
                'active_generation_id': 'generation-1',
            },
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        with patch(
            'org_context.cv_services._extract_cv_payload',
            new=AsyncMock(side_effect=AssertionError('fresh profile should be reused')),
        ), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            side_effect=AssertionError('fresh profile should not reindex when generation is current'),
        ):
            result = async_to_sync(build_cv_evidence_for_workspace)(workspace)

        self.assertEqual(result['reused_count'], 1)
        self.assertEqual(result['rebuilt_count'], 0)

    def test_build_cv_evidence_retries_previous_extraction_failures(self):
        workspace = IntakeWorkspace.objects.create(name='CV Retry Co', slug='cv-retry-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe CV')
        profile = EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=None,
            status=EmployeeCVProfile.Status.EXTRACTION_FAILED,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.FAILED,
            match_confidence=0.0,
            matched_by='',
            language_code='en',
            input_revision='',
            headline='',
            current_role='',
            seniority='',
            role_family='',
            extracted_payload={},
            metadata={'schema_version': 'stage6-v1', 'warnings': ['old failure']},
        )
        profile.input_revision = profile.input_revision or ''
        profile.save(update_fields=['input_revision', 'updated_at'])

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1},
        ):
            result = async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile.refresh_from_db()
        self.assertEqual(result['rebuilt_count'], 1)
        self.assertEqual(result['reused_count'], 0)
        self.assertEqual(profile.status, EmployeeCVProfile.Status.MATCHED)
        self.assertEqual(profile.employee, employee)

    def test_build_cv_evidence_rebuilds_matched_profiles_with_stale_async_warning(self):
        workspace = IntakeWorkspace.objects.create(name='CV Async Warning Co', slug='cv-async-warning-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Product Manager',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe product CV')
        profile = EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=employee,
            status=EmployeeCVProfile.Status.MATCHED,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.USABLE,
            match_confidence=0.95,
            matched_by='email_exact',
            language_code='en',
            input_revision='',
            headline='Product Manager',
            current_role='Product Manager',
            seniority='senior',
            role_family='product_manager',
            extracted_payload=self._cv_payload(current_role='Product Manager'),
            metadata={
                'schema_version': 'stage6-v1',
                'warnings': ['You cannot call this from an async context - use a thread or sync_to_async.'],
                'fact_counts': {'skill_evidence_rows': 0},
            },
        )
        profile.input_revision = _build_cv_input_revision(source)
        profile.save(update_fields=['input_revision', 'updated_at'])

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-async-fix'},
        ):
            result = async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile.refresh_from_db()
        self.assertEqual(result['rebuilt_count'], 1)
        self.assertEqual(result['reused_count'], 0)
        self.assertEqual(EmployeeSkillEvidence.objects.filter(employee=employee, source=source).count(), 1)
        self.assertNotIn('You cannot call this from an async context - use a thread or sync_to_async.', profile.metadata.get('warnings', []))

    def test_cv_status_and_unmatched_endpoints_surface_unresolved_cases(self):
        workspace = IntakeWorkspace.objects.create(name='CV Status Co', slug='cv-status-co')
        Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        matched_source = self._create_cv_source(workspace, title='Alice Doe CV')
        unmatched_source = self._create_cv_source(workspace, title='Unknown Candidate CV')

        payloads = [
            self._cv_payload(),
            self._cv_payload(candidate_name='Unknown Candidate', email='', current_role='Designer'),
        ]

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(side_effect=payloads)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 2},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        status_response = async_to_sync(get_workspace_cv_evidence_status)(workspace.slug)
        unmatched_response = async_to_sync(get_workspace_unmatched_cvs)(workspace.slug)

        self.assertEqual(status_response.total_cv_sources, 2)
        self.assertEqual(status_response.matched_count, 1)
        self.assertEqual(status_response.unmatched_count, 1)
        self.assertEqual(status_response.unresolved_source_count, 1)
        self.assertEqual(len(unmatched_response.items), 1)
        self.assertEqual(unmatched_response.items[0].source_uuid, unmatched_source.uuid)

    def test_get_employee_evidence_detail_returns_profiles_and_evidence_rows(self):
        workspace = IntakeWorkspace.objects.create(name='CV Detail Co', slug='cv-detail-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        self._create_cv_source(workspace, title='Alice Doe CV')

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 4},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)

        self.assertEqual(detail.employee_uuid, employee.uuid)
        self.assertEqual(len(detail.cv_profiles), 1)
        self.assertEqual(len(detail.candidate_cv_profiles), 0)
        self.assertEqual(len(detail.evidence_rows), 1)
        self.assertEqual(detail.evidence_rows[0].skill_key, 'python')
        self.assertIsNone(detail.coverage_gap)

    def test_get_employee_evidence_detail_surfaces_gap_context_when_no_cv_is_matched(self):
        workspace = IntakeWorkspace.objects.create(name='CV Gap Detail Co', slug='cv-gap-detail-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Abramson',
            email='alex@example.com',
            current_title='SRE',
            metadata={},
        )

        detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)

        self.assertEqual(detail.employee_uuid, employee.uuid)
        self.assertEqual(len(detail.cv_profiles), 0)
        self.assertEqual(len(detail.evidence_rows), 0)
        self.assertIsNotNone(detail.coverage_gap)
        self.assertEqual(detail.coverage_gap.review_reason, 'no_matched_cv_profile')
        self.assertIn('No matched CV profile exists for this employee yet.', detail.coverage_gap.warnings)

    def test_get_employee_evidence_detail_includes_employee_record_metadata(self):
        workspace = IntakeWorkspace.objects.create(name='CV Metadata Detail Co', slug='cv-metadata-detail-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Abramson',
            email='alex@example.com',
            current_title='SRE',
            external_employee_id='157',
            metadata={
                'source_kind': 'org_csv',
                'org_csv_provenance': {
                    'fields': {
                        'employee_id': '157',
                        'full_name': 'Alex Abramson',
                        'title': 'SRE',
                    },
                    'snippet': 'employee_id: 157 | full_name: Alex Abramson | title: SRE',
                },
            },
        )

        detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)

        self.assertEqual(detail.employee_uuid, employee.uuid)
        self.assertEqual(detail.external_employee_id, '157')
        self.assertEqual(detail.metadata.get('source_kind'), 'org_csv')
        self.assertEqual(
            detail.metadata.get('org_csv_provenance', {}).get('fields', {}).get('employee_id'),
            '157',
        )

    def test_marking_employee_no_cv_available_removes_gap_until_cleared(self):
        workspace = IntakeWorkspace.objects.create(name='CV Availability Co', slug='cv-availability-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Abramson',
            email='alex@example.com',
            current_title='SRE',
            metadata={},
        )

        initial_gap_response = async_to_sync(get_workspace_employees_without_cv_evidence)(workspace.slug)
        self.assertEqual(len(initial_gap_response.items), 1)

        mark_response = async_to_sync(mark_workspace_employee_no_cv)(
            workspace.slug,
            employee.uuid,
            EmployeeCvAvailabilityRequest(operator_name='Nikita', note='No CV has been collected yet.'),
        )
        detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)
        gap_response = async_to_sync(get_workspace_employees_without_cv_evidence)(workspace.slug)
        status_response = async_to_sync(get_workspace_cv_evidence_status)(workspace.slug)

        self.assertEqual(mark_response.status, 'no_cv_available')
        self.assertEqual(detail.cv_availability.status, 'no_cv_available')
        self.assertIsNone(detail.coverage_gap)
        self.assertEqual(len(gap_response.items), 0)
        self.assertEqual(status_response.employees_without_cv_evidence_count, 0)

        clear_response = async_to_sync(clear_workspace_employee_no_cv)(workspace.slug, employee.uuid)
        cleared_gap_response = async_to_sync(get_workspace_employees_without_cv_evidence)(workspace.slug)
        cleared_detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)

        self.assertEqual(clear_response.status, '')
        self.assertEqual(len(cleared_gap_response.items), 1)
        self.assertEqual(cleared_detail.coverage_gap.review_reason, 'no_matched_cv_profile')

    def test_get_employee_evidence_detail_includes_candidate_only_cv_profiles(self):
        workspace = IntakeWorkspace.objects.create(name='CV Candidate Detail Co', slug='cv-candidate-detail-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe uncertain CV')
        EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=None,
            status=EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.SPARSE,
            match_confidence=0.84,
            matched_by='low_confidence_name_match',
            language_code='en',
            input_revision='revision-1',
            headline='Backend Engineer',
            current_role='Backend Engineer',
            seniority='mid',
            role_family='backend_engineer',
            extracted_payload=self._cv_payload(),
            metadata={
                'candidate_matches': [
                    {
                        'employee_uuid': str(employee.uuid),
                        'full_name': employee.full_name,
                        'score': 0.84,
                    }
                ],
                'warnings': ['Needs operator review.'],
            },
        )

        detail = async_to_sync(get_employee_evidence_detail)(workspace.slug, employee.uuid)
        gaps = async_to_sync(get_workspace_employees_without_cv_evidence)(workspace.slug)

        self.assertEqual(len(detail.cv_profiles), 0)
        self.assertEqual(len(detail.candidate_cv_profiles), 1)
        self.assertEqual(detail.candidate_cv_profiles[0].source_uuid, source.uuid)
        self.assertEqual(gaps.items[0].review_reason, 'candidate_cv_pending_review')

    def test_approving_pending_skill_candidate_creates_override_and_evidence(self):
        workspace = IntakeWorkspace.objects.create(name='CV Pending Skill Co', slug='cv-pending-skill-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Growth Product Manager',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe growth CV')
        pending_skill_payload = self._cv_payload(
            current_role='Growth Product Manager',
            role_family='product_manager',
            skills=[
                {
                    'skill_name_en': 'PLG',
                    'skill_name_ru': '',
                    'original_term': 'PLG',
                    'level': 3,
                    'confidence': 78,
                    'category': 'product',
                    'aliases': ['growth loops'],
                    'evidence': 'Owned product-led growth experiments and activation funnels.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=pending_skill_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'skipped', 'reason': 'no_structured_cv_evidence', 'active_generation_id': ''},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile = EmployeeCVProfile.objects.get(source=source)
        pending_candidate = profile.metadata['pending_skill_candidates'][0]
        review_items_before = async_to_sync(get_workspace_cv_review_items)(workspace.slug)
        provisional_evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)

        self.assertEqual(EmployeeSkillEvidence.objects.filter(employee=employee, source=source).count(), 1)
        self.assertEqual(provisional_evidence.skill.resolution_status, Skill.ResolutionStatus.PENDING_REVIEW)
        self.assertEqual(provisional_evidence.metadata.get('resolution_status'), Skill.ResolutionStatus.PENDING_REVIEW)
        self.assertIn(source.uuid, {item.source_uuid for item in review_items_before.items})

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-approved'},
        ):
            response = async_to_sync(approve_workspace_pending_skill)(
                workspace.slug,
                source.uuid,
                PendingSkillApprovalRequest(
                    candidate_key=str(pending_candidate.get('proposed_key') or pending_candidate.get('display_name_en') or 'plg'),
                    approved_name_en='Product-led growth',
                    alias_terms=['PLG', 'growth loops'],
                ),
            )

        profile.refresh_from_db()
        review_items_after = async_to_sync(get_workspace_cv_review_items)(workspace.slug)
        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)

        self.assertEqual(response.pending_skill_candidates, [])
        self.assertEqual(profile.metadata['pending_skill_candidates'], [])
        self.assertEqual(evidence.skill.display_name_en, 'Product-led growth')
        self.assertEqual(evidence.skill.resolution_status, Skill.ResolutionStatus.RESOLVED)
        self.assertTrue(evidence.skill.is_operator_confirmed)
        self.assertTrue(evidence.is_operator_confirmed)
        self.assertEqual(evidence.operator_action, EmployeeSkillEvidence.OperatorAction.ACCEPTED)
        self.assertTrue(
            SkillResolutionOverride.objects.filter(
                workspace=workspace,
                normalized_term='plg',
                status='approved',
                canonical_key='product-led-growth',
            ).exists()
        )
        self.assertTrue(
            SkillReviewDecision.objects.filter(
                workspace=workspace,
                employee=employee,
                skill_canonical_key='product-led-growth',
                action=EmployeeSkillEvidence.OperatorAction.ACCEPTED,
            ).exists()
        )
        self.assertTrue(
            CatalogResolutionReviewItem.objects.filter(
                workspace=workspace,
                term_kind=CatalogResolutionReviewItem.TermKind.SKILL,
                normalized_term='plg',
                status='resolved',
            ).exists()
        )
        self.assertNotIn(source.uuid, {item.source_uuid for item in review_items_after.items})

    def test_approving_pending_skill_candidate_does_not_recreate_candidate_after_refresh(self):
        workspace = IntakeWorkspace.objects.create(name='CV API Override Co', slug='cv-api-override-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Tad Asher',
            email='tad@example.com',
            current_title='Frontend Developer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Tad Asher CV')
        pending_skill_payload = self._cv_payload(
            candidate_name='Tad Asher',
            email='tad@example.com',
            current_role='Frontend Developer',
            role_family='frontend_engineer',
            skills=[
                {
                    'skill_name_en': 'API Development',
                    'skill_name_ru': 'Разработка API',
                    'original_term': 'developed API requirements',
                    'level': 3,
                    'confidence': 80,
                    'category': 'core',
                    'aliases': [],
                    'evidence': 'Developed API requirements, and conducted code reviews.',
                    'canonical_key': 'api-development',
                    'match_source': 'review_pending',
                    'needs_review': True,
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=pending_skill_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'skipped', 'reason': 'no_structured_cv_evidence', 'active_generation_id': ''},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        profile = EmployeeCVProfile.objects.get(source=source)
        pending_candidate = profile.metadata['pending_skill_candidates'][0]

        self.assertEqual(pending_candidate['candidate_key'], 'api development')
        self.assertEqual(pending_candidate['proposed_key'], 'api-development')

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-api-approved'},
        ):
            response = async_to_sync(approve_workspace_pending_skill)(
                workspace.slug,
                source.uuid,
                PendingSkillApprovalRequest(
                    candidate_key=str(pending_candidate.get('candidate_key') or ''),
                    approved_name_en='API Design',
                    approved_name_ru='Разработка API',
                    alias_terms=list(pending_candidate.get('original_terms') or []),
                    approval_note='approved',
                ),
            )

        profile.refresh_from_db()
        review_items_after = async_to_sync(get_workspace_cv_review_items)(workspace.slug)
        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)

        self.assertEqual(response.pending_skill_candidates, [])
        self.assertEqual(profile.metadata['pending_skill_candidates'], [])
        self.assertEqual(evidence.skill.canonical_key, 'api-design')
        self.assertEqual(evidence.skill.display_name_en, 'API Design')
        self.assertEqual(evidence.operator_action, EmployeeSkillEvidence.OperatorAction.ACCEPTED)
        self.assertNotIn(source.uuid, {item.source_uuid for item in review_items_after.items})

    def test_bulk_review_employee_skill_reject_marks_evidence_and_decision(self):
        workspace = IntakeWorkspace.objects.create(name='CV Bulk Review Co', slug='cv-bulk-review-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Growth Product Manager',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe growth CV')
        pending_skill_payload = self._cv_payload(
            current_role='Growth Product Manager',
            role_family='product_manager',
            skills=[
                {
                    'skill_name_en': 'PLG Strategy',
                    'skill_name_ru': '',
                    'original_term': 'PLG Strategy',
                    'level': 3,
                    'confidence': 82,
                    'category': 'product',
                    'aliases': ['growth loops'],
                    'evidence': 'Owned product-led growth strategy and activation experiments.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=pending_skill_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-bulk-review'},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)
        self.assertEqual(evidence.skill.resolution_status, Skill.ResolutionStatus.PENDING_REVIEW)

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 0, 'active_generation_id': 'gen-bulk-review-2'},
        ):
            response = async_to_sync(bulk_review_employee_skill_evidence)(
                workspace.slug,
                employee.uuid,
                EmployeeSkillBulkReviewRequest(
                    actions=[
                        EmployeeSkillBulkReviewActionRequest(
                            evidence_uuid=evidence.uuid,
                            action='reject',
                            note='Not useful for this workspace.',
                        )
                    ]
                ),
            )

        evidence.refresh_from_db()
        self.assertEqual(response.processed, 1)
        self.assertEqual(response.rejected, 1)
        self.assertEqual(float(evidence.weight), 0.0)
        self.assertEqual(evidence.operator_action, EmployeeSkillEvidence.OperatorAction.REJECTED)
        self.assertTrue(
            SkillReviewDecision.objects.filter(
                workspace=workspace,
                employee=employee,
                skill_canonical_key=evidence.skill.canonical_key,
                action=EmployeeSkillEvidence.OperatorAction.REJECTED,
            ).exists()
        )
        pending_queue = async_to_sync(list_workspace_pending_skill_queue)(workspace.slug)
        self.assertEqual(pending_queue.total_pending, 0)

    def test_workspace_pending_skill_queue_and_accept_all_high_confidence_flow(self):
        workspace = IntakeWorkspace.objects.create(name='CV Pending Queue Co', slug='cv-pending-queue-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Growth Product Manager',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe growth CV')
        pending_skill_payload = self._cv_payload(
            current_role='Growth Product Manager',
            role_family='product_manager',
            skills=[
                {
                    'skill_name_en': 'PLG Strategy',
                    'skill_name_ru': '',
                    'original_term': 'PLG Strategy',
                    'level': 3,
                    'confidence': 84,
                    'category': 'product',
                    'aliases': ['growth loops'],
                    'evidence': 'Owned product-led growth strategy and activation experiments.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=pending_skill_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-pending-queue'},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        pending_queue_before = async_to_sync(list_workspace_pending_skill_queue)(workspace.slug)
        self.assertEqual(pending_queue_before.total_pending, 1)
        self.assertEqual(pending_queue_before.pending_skills[0].employee_count, 1)

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-pending-queue-2'},
        ):
            accept_response = async_to_sync(accept_high_confidence_employee_skills)(
                workspace.slug,
                employee.uuid,
                EmployeeSkillAcceptAllRequest(confidence_threshold=0.7),
            )

        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)
        pending_queue_after = async_to_sync(list_workspace_pending_skill_queue)(workspace.slug)

        self.assertEqual(accept_response.accepted_count, 1)
        self.assertEqual(evidence.operator_action, EmployeeSkillEvidence.OperatorAction.ACCEPTED)
        self.assertTrue(evidence.is_operator_confirmed)
        self.assertEqual(evidence.skill.resolution_status, Skill.ResolutionStatus.RESOLVED)
        self.assertEqual(pending_queue_after.total_pending, 0)

    def test_workspace_bulk_resolve_merge_relinks_pending_skill(self):
        workspace = IntakeWorkspace.objects.create(name='CV Workspace Resolve Co', slug='cv-workspace-resolve-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Growth Product Manager',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe growth CV')
        target_skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='growth-strategy',
            display_name_en='Growth Strategy',
            display_name_ru='',
            source='seed',
            metadata={},
        )
        pending_skill_payload = self._cv_payload(
            current_role='Growth Product Manager',
            role_family='product_manager',
            skills=[
                {
                    'skill_name_en': 'PLG Strategy',
                    'skill_name_ru': '',
                    'original_term': 'PLG Strategy',
                    'level': 3,
                    'confidence': 84,
                    'category': 'product',
                    'aliases': ['growth loops'],
                    'evidence': 'Owned product-led growth strategy and activation experiments.',
                }
            ],
        )

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=pending_skill_payload)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-workspace-resolve'},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        evidence = EmployeeSkillEvidence.objects.get(employee=employee, source=source)
        original_skill = evidence.skill

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-workspace-resolve-2'},
        ):
            response = async_to_sync(bulk_resolve_workspace_skill_queue)(
                workspace.slug,
                WorkspaceSkillResolutionRequest(
                    resolutions=[
                        WorkspaceSkillResolutionRequestItem(
                            skill_uuid=original_skill.uuid,
                            action='merge',
                            target_skill_uuid=target_skill.uuid,
                        )
                    ]
                ),
            )

        evidence.refresh_from_db()
        original_skill.refresh_from_db()
        self.assertEqual(response.processed, 1)
        self.assertEqual(response.merged, 1)
        self.assertEqual(evidence.skill, target_skill)
        self.assertEqual(evidence.operator_action, EmployeeSkillEvidence.OperatorAction.MERGED)
        self.assertEqual(original_skill.resolution_status, Skill.ResolutionStatus.REJECTED)
        self.assertTrue(
            SkillResolutionOverride.objects.filter(
                workspace=workspace,
                canonical_key='growth-strategy',
                status=CatalogOverrideStatus.APPROVED,
            ).exists()
        )

    def test_resolve_workspace_cv_match_attaches_evidence_to_selected_employee(self):
        workspace = IntakeWorkspace.objects.create(name='CV Resolve Co', slug='cv-resolve-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe uncertain CV')
        profile = EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=None,
            status=EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.USABLE,
            match_confidence=0.84,
            matched_by='low_confidence_name_match',
            language_code='en',
            input_revision='revision-1',
            headline='Backend Engineer',
            current_role='Backend Engineer',
            seniority='mid',
            role_family='backend_engineer',
            extracted_payload=self._cv_payload(),
            metadata={
                'candidate_matches': [
                    {'employee_uuid': str(employee.uuid), 'full_name': employee.full_name, 'score': 0.84}
                ],
                'warnings': ['Needs operator review.'],
            },
        )
        EmployeeCVMatchCandidate.objects.create(
            workspace=workspace,
            profile=profile,
            employee=employee,
            rank=1,
            score=0.84,
            name_score=0.84,
            title_score=0.7,
            department_score=0.0,
            exact_name_match=False,
            email_match=False,
            metadata={},
        )

        with patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-1'},
        ):
            response = async_to_sync(resolve_workspace_cv_match)(
                workspace.slug,
                source.uuid,
                CVMatchResolutionRequest(
                    employee_uuid=employee.uuid,
                    operator_name='Nikita',
                    resolution_note='Confirmed by operator.',
                ),
            )

        profile.refresh_from_db()
        self.assertEqual(response.status, EmployeeCVProfile.Status.MATCHED)
        self.assertEqual(profile.employee, employee)
        self.assertEqual(profile.matched_by, 'operator_override')
        self.assertEqual(EmployeeSkillEvidence.objects.filter(employee=employee, source=source).count(), 1)

    def test_delete_employee_detaches_linked_cv_profiles_and_removes_record(self):
        workspace = IntakeWorkspace.objects.create(name='CV Delete Co', slug='cv-delete-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Top Skills evidence',
            email='topskills@example.com',
            current_title='',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Top Skills bogus CV')

        with patch(
            'org_context.cv_services._extract_cv_payload',
            new=AsyncMock(
                return_value=self._cv_payload(
                    candidate_name='Top Skills evidence',
                    email='topskills@example.com',
                    current_role='',
                    headline='',
                )
            ),
        ), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 1, 'active_generation_id': 'gen-delete'},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        delete_response = async_to_sync(delete_workspace_employee_view)(workspace.slug, employee.uuid)
        profile = EmployeeCVProfile.objects.get(source=source)
        unmatched_response = async_to_sync(get_workspace_unmatched_cvs)(workspace.slug)

        self.assertEqual(delete_response.full_name, 'Top Skills evidence')
        self.assertFalse(Employee.objects.filter(workspace=workspace, uuid=employee.uuid).exists())
        self.assertIsNone(profile.employee)
        self.assertEqual(profile.status, EmployeeCVProfile.Status.UNMATCHED)
        self.assertEqual(profile.matched_by, 'employee_deleted')
        self.assertEqual(EmployeeSkillEvidence.objects.filter(source=source).count(), 0)
        self.assertIn(source.uuid, {item.source_uuid for item in unmatched_response.items})

    def test_build_cv_evidence_reports_requested_sources_that_are_not_processable(self):
        workspace = IntakeWorkspace.objects.create(name='CV Request Co', slug='cv-request-co')
        wrong_kind_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap text',
        )
        not_parsed_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Unparsed CV',
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Raw CV text',
        )
        missing_uuid = uuid4()

        result = async_to_sync(build_cv_evidence_for_workspace)(
            workspace,
            source_uuids=[
                str(wrong_kind_source.uuid),
                str(not_parsed_source.uuid),
                str(missing_uuid),
            ],
        )

        statuses_by_source = {item['source_uuid']: item['status'] for item in result['results']}
        self.assertEqual(statuses_by_source[str(wrong_kind_source.uuid)], 'wrong_kind')
        self.assertEqual(statuses_by_source[str(not_parsed_source.uuid)], 'not_parsed')
        self.assertEqual(statuses_by_source[str(missing_uuid)], 'missing')

    def test_review_items_and_employee_gap_endpoints_split_unmatched_and_sparse_cases(self):
        workspace = IntakeWorkspace.objects.create(name='CV Review Co', slug='cv-review-co')
        matched_employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        Employee.objects.create(
            workspace=workspace,
            full_name='Bob Missing',
            email='bob@example.com',
            current_title='Designer',
            metadata={},
        )
        matched_sparse_source = self._create_cv_source(workspace, title='Alice Doe CV')
        unmatched_source = self._create_cv_source(workspace, title='Unknown Candidate CV')

        payloads = [
            self._cv_payload(
                skills=[],
                role_history=[],
                achievements=[],
                domain_experience=[],
                leadership_signals=[],
                sparse_cv=True,
                sparse_reason='The CV only includes a short headline.',
            ),
            self._cv_payload(candidate_name='Unknown Candidate', email='', current_role='Designer'),
        ]

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(side_effect=payloads)), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'skipped', 'reason': 'no_structured_cv_evidence', 'active_generation_id': ''},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)

        unmatched_response = async_to_sync(get_workspace_unmatched_cvs)(workspace.slug)
        review_items_response = async_to_sync(get_workspace_cv_review_items)(workspace.slug)
        employee_gap_response = async_to_sync(get_workspace_employees_without_cv_evidence)(workspace.slug)

        self.assertEqual([item.source_uuid for item in unmatched_response.items], [unmatched_source.uuid])
        self.assertEqual(
            {item.source_uuid for item in review_items_response.items},
            {matched_sparse_source.uuid, unmatched_source.uuid},
        )
        gap_reasons = {item.employee_uuid: item.review_reason for item in employee_gap_response.items}
        self.assertEqual(gap_reasons[matched_employee.uuid], 'sparse_cv')
        matched_gap = next(item for item in employee_gap_response.items if item.employee_uuid == matched_employee.uuid)
        self.assertIn('sparse_cv', matched_gap.review_reasons)
        self.assertEqual(matched_gap.related_source_uuids, [matched_sparse_source.uuid])
        self.assertIn('no_matched_cv_profile', gap_reasons.values())

    def test_rebuild_cv_evidence_does_not_duplicate_rows(self):
        workspace = IntakeWorkspace.objects.create(name='CV Rebuild Co', slug='cv-rebuild-co')
        Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        self._create_cv_source(workspace, title='Alice Doe CV')

        with patch('org_context.cv_services._extract_cv_payload', new=AsyncMock(return_value=self._cv_payload())), patch(
            'org_context.cv_services.index_employee_cv_profile_sync',
            return_value={'status': 'indexed', 'indexed_document_count': 4},
        ):
            async_to_sync(build_cv_evidence_for_workspace)(workspace)
            async_to_sync(rebuild_cv_evidence_for_workspace)(workspace)

        self.assertEqual(EmployeeSkillEvidence.objects.filter(workspace=workspace).count(), 1)

    def test_index_employee_cv_profile_sync_builds_employee_and_skill_payloads(self):
        workspace = IntakeWorkspace.objects.create(name='CV Vector Co', slug='cv-vector-co')
        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alice Doe',
            email='alice@example.com',
            current_title='Backend Engineer',
            metadata={},
        )
        source = self._create_cv_source(workspace, title='Alice Doe CV')
        profile = EmployeeCVProfile.objects.create(
            workspace=workspace,
            source=source,
            employee=employee,
            status=EmployeeCVProfile.Status.MATCHED,
            evidence_quality=EmployeeCVProfile.EvidenceQuality.USABLE,
            match_confidence=0.97,
            matched_by='email_exact',
            language_code='en',
            headline='Senior Backend Engineer',
            current_role='Senior Backend Engineer',
            seniority='senior',
            role_family='backend_engineer',
            active_vector_generation_id='previous-generation',
            extracted_payload=self._cv_payload(),
            metadata={},
        )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            source='catalog_seed',
            metadata={},
        )
        evidence_row = EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
            source=source,
            current_level=4,
            confidence=0.9,
            weight=0.8,
            evidence_text='Built backend APIs and services in Python.',
            metadata={
                'evidence_category': 'core',
                'source_uuid': str(source.uuid),
                'snippet': 'Built backend APIs and services in Python.',
            },
        )

        fake_embedding_manager = Mock()
        fake_embedding_manager.model_name = 'text-embedding-3-small'
        fake_embedding_manager.dimensions = 3
        fake_embedding_manager.embed_batch_sync.return_value = [[0.1, 0.2, 0.3] for _ in range(5)]

        fake_qdrant = Mock()
        fake_qdrant.vector_size = 3
        fake_qdrant.collection_name = 'org_context_documents'
        fake_qdrant.delete_by_filters_sync.return_value = True
        fake_qdrant.upsert_documents_batch_sync.return_value = 5

        with patch('org_context.vector_indexing.get_embedding_manager_sync', return_value=fake_embedding_manager), patch(
            'org_context.vector_indexing.get_qdrant_manager_sync',
            return_value=fake_qdrant,
        ):
            result = index_employee_cv_profile_sync(profile.pk)

        self.assertEqual(result['status'], 'indexed')
        documents = fake_qdrant.upsert_documents_batch_sync.call_args.args[0]
        skill_document = next(doc for doc in documents if doc['payload']['doc_type'] == 'cv_skill_evidence')
        self.assertEqual(skill_document['payload']['employee_uuid'], str(employee.uuid))
        self.assertEqual(skill_document['payload']['skill_key'], 'python')
        self.assertEqual(skill_document['payload']['evidence_row_uuid'], str(evidence_row.uuid))
        self.assertEqual(skill_document['payload']['source_kind'], WorkspaceSourceKind.EMPLOYEE_CV)
        self.assertTrue(skill_document['payload']['generation_id'])
        self.assertIn(':generation:', skill_document['id'])
        self.assertNotEqual(result['active_generation_id'], 'previous-generation')
        self.assertTrue(
            any(
                call.kwargs.get('additional_filters', {}).get('generation_id') == 'previous-generation'
                for call in fake_qdrant.delete_by_filters_sync.call_args_list
            )
        )


class RoadmapAnalysisTests(TestCase):
    def _create_parsed_source(self, workspace: IntakeWorkspace, *, source_kind: str, title: str, text: str) -> ParsedSource:
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title=title,
            source_kind=source_kind,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text=text,
            status=WorkspaceSourceStatus.PARSED,
        )
        parsed = ParsedSource.objects.create(
            workspace=workspace,
            source=source,
            content_type='text/plain',
            word_count=len(text.split()),
            char_count=len(text),
            extracted_text=text,
            metadata={},
        )
        SourceChunk.objects.create(
            parsed_source=parsed,
            chunk_index=0,
            text=text,
            char_count=len(text),
            metadata={},
        )
        return parsed

    def test_run_roadmap_analysis_persists_structured_outputs(self):
        workspace = IntakeWorkspace.objects.create(
            name='Roadmap Co',
            slug='roadmap-co',
            metadata={
                'company_profile': {
                    'company_name': 'Roadmap Co',
                    'company_description': 'Builds B2B workflow software.',
                    'main_products': ['Core'],
                    'target_customers': ['Operations teams'],
                },
                'pilot_scope': {},
            },
        )
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Roadmap',
            text='Launch AI marketplace integrations in Q2 with new APIs and analytics.',
        )

        llm_results = [
            SimpleNamespace(
                parsed={
                    'source_initiatives': [
                        {
                            'name': 'Marketplace Launch',
                            'goal': 'Launch marketplace integrations',
                            'criticality': 'high',
                            'planned_window': 'Q2 2026',
                            'key_deliverables': ['Partner APIs'],
                            'tech_references': ['Python'],
                            'team_references': ['Engineering'],
                            'success_metrics': ['Marketplace GMV'],
                            'evidence_quote': 'Launch integrations in Q2',
                            'confidence': 0.9,
                        }
                    ]
                }
            ),
            SimpleNamespace(
                parsed={
                    'initiatives': [
                        {
                            'name': 'Marketplace Launch',
                            'goal': 'Launch marketplace integrations',
                            'criticality': 'high',
                            'planned_window': 'Q2 2026',
                            'source_refs': ['roadmap-source'],
                            'confidence': 0.9,
                        }
                    ],
                    'workstreams': [
                        {
                            'name': 'Marketplace API',
                            'initiative_id': 'Marketplace Launch',
                            'scope': 'Build partner-facing APIs',
                            'delivery_type': 'backend_service',
                            'affected_systems': ['api'],
                            'team_shape': {'estimated_headcount': 2, 'roles_needed': ['Backend Engineer'], 'duration_months': 3},
                            'required_capabilities': [{'capability': 'Python', 'level': 'advanced', 'criticality': 'high'}],
                            'estimated_effort': '3 engineer-months',
                            'confidence': 0.8,
                            'source_refs': ['roadmap-source'],
                        }
                    ],
                }
            ),
            SimpleNamespace(
                parsed={
                    'capability_bundles': [
                        {
                            'capability_name': 'Backend platform delivery',
                            'criticality': 'high',
                            'capability_type': 'technical',
                            'workstream_ids': ['Marketplace API'],
                            'inferred_role_families': ['backend_engineer'],
                            'skill_hints': ['Python'],
                            'evidence_refs': ['roadmap-source'],
                            'confidence': 0.85,
                        }
                    ],
                    'prd_summaries': [
                        {
                            'initiative_id': 'Marketplace Launch',
                            'problem_statement': 'Need partner integrations',
                            'proposed_solution': 'Build APIs',
                            'success_metrics': ['GMV'],
                            'technical_approach': 'Backend service',
                            'open_questions': ['Who owns partner onboarding?'],
                        }
                    ],
                }
            ),
            SimpleNamespace(
                parsed={
                    'dependencies': [
                        {
                            'from_workstream_id': 'Marketplace API',
                            'to_workstream_id': 'Marketplace API',
                            'dependency_type': 'shared_service',
                            'description': 'Billing service must be reused.',
                            'criticality': 'soft',
                        }
                    ],
                    'delivery_risks': [
                        {
                            'risk_type': 'scope_ambiguity',
                            'description': 'Partner support scope is unclear.',
                            'affected_workstreams': ['Marketplace API'],
                            'severity': 'medium',
                            'mitigation_hint': 'Clarify support ownership.',
                            'confidence': 0.72,
                        }
                    ],
                }
            ),
        ]

        with patch('org_context.roadmap_services.call_openai_structured', side_effect=llm_results):
            run = async_to_sync(run_roadmap_analysis)(workspace)

        run.refresh_from_db()
        self.assertEqual(run.status, RoadmapAnalysisRun.Status.COMPLETED)
        self.assertEqual(len(run.initiatives), 1)
        self.assertEqual(run.initiatives[0]['id'], 'init-marketplace-launch')
        self.assertEqual(run.workstreams[0]['id'], 'ws-marketplace-api')
        self.assertEqual(run.capability_bundles[0]['bundle_id'], 'bundle-backend-platform-delivery')
        self.assertEqual(run.clarification_questions[0]['question'], 'Who owns partner onboarding?')

    def test_run_roadmap_analysis_reuses_matching_fingerprint(self):
        workspace = IntakeWorkspace.objects.create(name='Fingerprint Co', slug='fingerprint-co')
        self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Roadmap',
            text='Launch AI marketplace integrations in Q2.',
        )
        existing = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            input_snapshot={'analysis_fingerprint': 'same-fingerprint'},
        )

        with patch('org_context.roadmap_services._build_analysis_fingerprint', return_value='same-fingerprint'), patch(
            'org_context.roadmap_services.call_openai_structured'
        ) as mocked_llm:
            run = async_to_sync(run_roadmap_analysis)(workspace)

        self.assertEqual(run.pk, existing.pk)
        mocked_llm.assert_not_called()

    def test_roadmap_schemas_mark_all_defined_properties_as_required_for_strict_mode(self):
        def assert_strict_required(schema: dict):
            if not isinstance(schema, dict):
                return
            if schema.get('type') == 'object':
                self.assertIs(schema.get('additionalProperties'), False)
                properties = schema.get('properties')
                self.assertIsInstance(properties, dict)
                required = schema.get('required')
                self.assertIsInstance(required, list)
                self.assertEqual(sorted(required), sorted(properties.keys()))
                for subschema in properties.values():
                    assert_strict_required(subschema)
            if schema.get('type') == 'array' and isinstance(schema.get('items'), dict):
                assert_strict_required(schema['items'])

        for schema in (
            INITIATIVE_EXTRACTION_SCHEMA,
            WORKSTREAM_SYNTHESIS_SCHEMA,
            CAPABILITY_BUNDLE_SCHEMA,
            RISK_ANALYSIS_SCHEMA,
        ):
            assert_strict_required(schema)

    def test_run_roadmap_analysis_with_planning_context_resolves_profile_snapshot_safely(self):
        workspace = IntakeWorkspace.objects.create(
            name='Scoped Roadmap Co',
            slug='scoped-roadmap-co',
            metadata={
                'company_profile': {
                    'company_name': 'Scoped Roadmap Co',
                },
                'pilot_scope': {},
            },
        )
        baseline = PlanningContext.objects.create(
            workspace=workspace,
            name='Org baseline',
            slug='org-baseline',
            kind=PlanningContext.Kind.ORG,
        )
        ContextProfile.objects.create(
            planning_context=baseline,
            company_profile={'company_name': 'Scoped Roadmap Co'},
            tech_stack=['Python'],
            override_fields=['company_profile', 'tech_stack'],
        )
        project = Project.objects.create(workspace=workspace, name='Hyperskill')
        project_context = PlanningContext.objects.create(
            workspace=workspace,
            name='Hyperskill',
            slug='hyperskill',
            kind=PlanningContext.Kind.PROJECT,
            parent_context=baseline,
            project=project,
        )
        ContextProfile.objects.create(
            planning_context=project_context,
            company_profile={'company_name': 'Scoped Roadmap Co'},
            tech_stack=['TypeScript'],
            override_fields=['tech_stack'],
            inherit_from_parent=True,
        )
        parsed = self._create_parsed_source(
            workspace,
            source_kind=WorkspaceSourceKind.ROADMAP,
            title='Roadmap',
            text='Launch AI marketplace integrations in Q2 with new APIs and analytics.',
        )
        PlanningContextSource.objects.create(
            planning_context=baseline,
            workspace_source=parsed.source,
            usage_type='roadmap',
            include_in_roadmap_analysis=True,
            include_in_blueprint=True,
            is_active=True,
        )

        llm_results = [
            SimpleNamespace(
                parsed={
                    'source_initiatives': [
                        {
                            'name': 'Marketplace Launch',
                            'goal': 'Launch marketplace integrations',
                            'criticality': 'high',
                            'planned_window': 'Q2 2026',
                            'key_deliverables': ['Partner APIs'],
                            'tech_references': ['TypeScript'],
                            'team_references': ['Engineering'],
                            'success_metrics': ['Marketplace GMV'],
                            'evidence_quote': 'Launch integrations in Q2',
                            'confidence': 0.9,
                        }
                    ]
                }
            ),
            SimpleNamespace(
                parsed={
                    'initiatives': [
                        {
                            'name': 'Marketplace Launch',
                            'goal': 'Launch marketplace integrations',
                            'criticality': 'high',
                            'planned_window': 'Q2 2026',
                            'source_refs': ['roadmap-source'],
                            'confidence': 0.9,
                        }
                    ],
                    'workstreams': [
                        {
                            'name': 'Marketplace API',
                            'initiative_id': 'Marketplace Launch',
                            'scope': 'Build partner-facing APIs',
                            'delivery_type': 'backend_service',
                            'affected_systems': ['api'],
                            'team_shape': {'estimated_headcount': 2, 'roles_needed': ['Backend Engineer'], 'duration_months': 3},
                            'required_capabilities': [{'capability': 'TypeScript', 'level': 'advanced', 'criticality': 'high'}],
                            'estimated_effort': '3 engineer-months',
                            'confidence': 0.8,
                            'source_refs': ['roadmap-source'],
                        }
                    ],
                }
            ),
            SimpleNamespace(
                parsed={
                    'capability_bundles': [
                        {
                            'capability_name': 'Backend platform delivery',
                            'criticality': 'high',
                            'capability_type': 'technical',
                            'workstream_ids': ['Marketplace API'],
                            'inferred_role_families': ['backend_engineer'],
                            'skill_hints': ['TypeScript'],
                            'evidence_refs': ['roadmap-source'],
                            'confidence': 0.85,
                        }
                    ],
                    'prd_summaries': [
                        {
                            'initiative_id': 'Marketplace Launch',
                            'problem_statement': 'Need partner integrations',
                            'proposed_solution': 'Build APIs',
                            'success_metrics': ['GMV'],
                            'technical_approach': 'Backend service',
                            'open_questions': ['Who owns partner onboarding?'],
                        }
                    ],
                }
            ),
            SimpleNamespace(
                parsed={
                    'dependencies': [],
                    'delivery_risks': [],
                }
            ),
        ]

        uncached_context = PlanningContext.objects.get(pk=project_context.pk)

        with patch('org_context.roadmap_services.call_openai_structured', side_effect=llm_results):
            run = async_to_sync(run_roadmap_analysis)(workspace, planning_context=uncached_context)

        self.assertEqual(run.status, RoadmapAnalysisRun.Status.COMPLETED)
        self.assertEqual(run.planning_context_id, project_context.pk)
        self.assertEqual(run.input_snapshot.get('planning_context_uuid'), str(project_context.uuid))

    def test_roadmap_analysis_views_return_status_and_latest_run(self):
        workspace = IntakeWorkspace.objects.create(name='Roadmap View Co', slug='roadmap-view-co')
        run = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            source_summary={'source_count': 1},
            initiatives=[{'id': 'init-marketplace', 'name': 'Marketplace'}],
            workstreams=[{'id': 'ws-marketplace', 'name': 'Marketplace API'}],
            capability_bundles=[],
            delivery_risks=[],
        )

        status_payload = async_to_sync(get_roadmap_analysis_status)(workspace.slug)
        latest_payload = async_to_sync(get_latest_roadmap_analysis)(workspace.slug)
        service_status = async_to_sync(build_roadmap_analysis_status_payload)(workspace)

        self.assertTrue(status_payload.has_analysis)
        self.assertEqual(status_payload.latest_run.uuid, run.uuid)
        self.assertEqual(latest_payload.uuid, run.uuid)
        self.assertEqual(latest_payload.status, RoadmapAnalysisRun.Status.COMPLETED)
        self.assertTrue(service_status['has_analysis'])

    def test_trigger_roadmap_analysis_view_runs_service(self):
        workspace = IntakeWorkspace.objects.create(name='Roadmap Trigger Co', slug='roadmap-trigger-co')
        run = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
        )

        with patch('org_context.prototype_fastapi_views.assert_workspace_ready_for_stage', new=AsyncMock()), patch(
            'org_context.prototype_fastapi_views.run_roadmap_analysis',
            new=AsyncMock(return_value=run),
        ):
            response = async_to_sync(trigger_roadmap_analysis)(
                workspace.slug,
                RoadmapAnalysisRunRequest(force_rebuild=True),
            )

        self.assertEqual(response.run_uuid, run.uuid)
        self.assertEqual(response.status, RoadmapAnalysisRun.Status.COMPLETED)

    def test_trigger_roadmap_analysis_view_returns_failed_message_when_run_fails(self):
        workspace = IntakeWorkspace.objects.create(name='Roadmap Failed Co', slug='roadmap-failed-co')
        run = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.FAILED,
            error_message='Bad structured schema.',
        )

        with patch('org_context.prototype_fastapi_views.assert_workspace_ready_for_stage', new=AsyncMock()), patch(
            'org_context.prototype_fastapi_views.run_roadmap_analysis',
            new=AsyncMock(return_value=run),
        ):
            response = async_to_sync(trigger_roadmap_analysis)(
                workspace.slug,
                RoadmapAnalysisRunRequest(force_rebuild=True),
            )

        self.assertEqual(response.run_uuid, run.uuid)
        self.assertEqual(response.status, RoadmapAnalysisRun.Status.FAILED)
        self.assertIn('failed', response.message.lower())
        self.assertIn('Bad structured schema.', response.message)


class PlanningContextPrototypeViewTests(TestCase):
    def _create_workspace(self, slug: str = 'planning-context-co') -> IntakeWorkspace:
        return IntakeWorkspace.objects.create(name='Planning Context Co', slug=slug)

    def _create_baseline_context(self, workspace: IntakeWorkspace) -> PlanningContext:
        context = PlanningContext.objects.create(
            workspace=workspace,
            name='Org baseline',
            slug='org-baseline',
            kind=PlanningContext.Kind.ORG,
        )
        ContextProfile.objects.create(
            planning_context=context,
            company_profile={'company_name': workspace.name},
            tech_stack=['Python', 'Django'],
            override_fields=['company_profile', 'tech_stack'],
        )
        return context

    def test_create_workspace_project_lists_and_trims_name(self):
        workspace = self._create_workspace(slug='workspace-projects-co')

        created = async_to_sync(create_workspace_project)(
            workspace.slug,
            ProjectCreateRequest(name='  AI Features  '),
        )
        listing = async_to_sync(list_workspace_projects)(workspace.slug)

        self.assertEqual(created.name, 'AI Features')
        self.assertEqual([item.name for item in listing.projects], ['AI Features'])
        self.assertTrue(Project.objects.filter(workspace=workspace, name='AI Features').exists())

    def test_create_workspace_project_rejects_duplicate_name_case_insensitive(self):
        workspace = self._create_workspace(slug='workspace-project-duplicate-co')
        Project.objects.create(workspace=workspace, name='AI Features')

        with self.assertRaises(HTTPException) as exc:
            async_to_sync(create_workspace_project)(
                workspace.slug,
                ProjectCreateRequest(name='ai features'),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('already exists', exc.exception.detail.lower())

    def test_create_list_and_detail_planning_context(self):
        workspace = self._create_workspace()
        baseline = self._create_baseline_context(workspace)
        project = Project.objects.create(workspace=workspace, name='AI Features')

        created = async_to_sync(create_planning_context)(
            workspace.slug,
            PlanningContextCreateRequest(
                name='AI Features',
                slug='ai-features',
                kind='project',
                parent_context_uuid=baseline.uuid,
                project_uuid=project.uuid,
                description='Planning scope for AI features.',
                profile={
                    'tech_stack': ['PyTorch'],
                    'override_fields': ['tech_stack'],
                },
            ),
        )

        listing = async_to_sync(list_planning_contexts)(workspace.slug)
        detail = async_to_sync(get_planning_context_detail)(workspace.slug, 'ai-features')

        self.assertEqual(created.slug, 'ai-features')
        self.assertEqual(created.parent_context.slug, 'org-baseline')
        self.assertEqual({item.slug for item in listing.contexts}, {'org-baseline', 'ai-features'})
        self.assertEqual(detail.parent_context.slug, 'org-baseline')
        self.assertIn('PyTorch', detail.profile.tech_stack)

    def test_create_root_org_context_forces_non_inheriting_profile(self):
        workspace = self._create_workspace(slug='root-context-co')

        created = async_to_sync(create_planning_context)(
            workspace.slug,
            PlanningContextCreateRequest(
                name='Org root',
                slug='org-root',
                kind='org',
                profile={
                    'company_profile': {'company_name': 'Org root'},
                    'tech_stack': ['Python'],
                    'override_fields': ['company_profile', 'tech_stack'],
                },
            ),
        )

        self.assertFalse(created.profile.inherit_from_parent)
        self.assertEqual(created.effective_profile['company_profile']['company_name'], 'Org root')
        self.assertEqual(created.effective_profile['tech_stack'], ['Python'])

    def test_create_planning_context_rejects_invalid_hierarchy(self):
        workspace = self._create_workspace(slug='invalid-context-co')
        baseline = self._create_baseline_context(workspace)
        scenario_parent = PlanningContext.objects.create(
            workspace=workspace,
            name='Scenario parent',
            slug='scenario-parent',
            kind=PlanningContext.Kind.SCENARIO,
            parent_context=baseline,
        )
        ContextProfile.objects.create(planning_context=scenario_parent, override_fields=[])
        project = Project.objects.create(workspace=workspace, name='AI Features')

        with self.assertRaises(HTTPException) as exc:
            async_to_sync(create_planning_context)(
                workspace.slug,
                PlanningContextCreateRequest(
                    name='Invalid project',
                    slug='invalid-project',
                    kind='project',
                    parent_context_uuid=scenario_parent.uuid,
                    project_uuid=project.uuid,
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('org context', exc.exception.detail.lower())

    def test_add_planning_context_source_infers_roadmap_defaults(self):
        workspace = self._create_workspace(slug='source-defaults-co')
        baseline = self._create_baseline_context(workspace)
        roadmap_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap content',
            status=WorkspaceSourceStatus.PARSED,
        )

        link = async_to_sync(add_planning_context_source)(
            workspace.slug,
            baseline.slug,
            PlanningContextSourceCreateRequest(workspace_source_uuid=roadmap_source.uuid),
        )

        self.assertEqual(link.usage_type, 'roadmap')
        self.assertTrue(link.include_in_roadmap_analysis)

    def test_detail_marks_inherited_inactive_links_as_excluded(self):
        workspace = self._create_workspace(slug='excluded-source-co')
        baseline = self._create_baseline_context(workspace)
        project_context = PlanningContext.objects.create(
            workspace=workspace,
            name='Project AI',
            slug='project-ai',
            kind=PlanningContext.Kind.PROJECT,
            parent_context=baseline,
            project=Project.objects.create(workspace=workspace, name='AI Features'),
        )
        ContextProfile.objects.create(planning_context=project_context, override_fields=[])
        scenario_context = PlanningContext.objects.create(
            workspace=workspace,
            name='Scenario',
            slug='scenario',
            kind=PlanningContext.Kind.SCENARIO,
            parent_context=project_context,
        )
        ContextProfile.objects.create(planning_context=scenario_context, override_fields=[])
        roadmap_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap content',
            status=WorkspaceSourceStatus.PARSED,
        )
        PlanningContextSource.objects.create(
            planning_context=baseline,
            workspace_source=roadmap_source,
            usage_type='roadmap',
            include_in_roadmap_analysis=True,
        )
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=roadmap_source,
            usage_type='roadmap',
            is_active=False,
            include_in_roadmap_analysis=False,
        )

        detail = async_to_sync(get_planning_context_detail)(workspace.slug, scenario_context.slug)

        self.assertEqual(detail.sources[0].origin, 'excluded')
        self.assertEqual(detail.sources[0].inherited_from_context_slug, project_context.slug)

    def test_trigger_roadmap_analysis_view_passes_planning_context(self):
        workspace = self._create_workspace(slug='roadmap-context-co')
        planning_context = self._create_baseline_context(workspace)
        run = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            planning_context=planning_context,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
        )

        with patch('org_context.prototype_fastapi_views.assert_workspace_ready_for_stage', new=AsyncMock()) as mock_ready, patch(
            'org_context.prototype_fastapi_views.run_roadmap_analysis',
            new=AsyncMock(return_value=run),
        ) as mock_run:
            response = async_to_sync(trigger_roadmap_analysis)(
                workspace.slug,
                RoadmapAnalysisRunRequest(force_rebuild=True),
                planning_context_uuid=planning_context.uuid,
            )

        mock_ready.assert_awaited_once_with(
            workspace,
            'roadmap_analysis',
            planning_context=planning_context,
        )
        mock_run.assert_awaited_once_with(
            workspace,
            planning_context=planning_context,
            force_rebuild=True,
        )
        self.assertEqual(response.run_uuid, run.uuid)

    def test_get_roadmap_analysis_status_filters_by_planning_context(self):
        workspace = self._create_workspace(slug='roadmap-status-co')
        baseline = self._create_baseline_context(workspace)
        project = PlanningContext.objects.create(
            workspace=workspace,
            name='Project AI',
            slug='project-ai',
            kind=PlanningContext.Kind.ORG,
        )
        ContextProfile.objects.create(planning_context=project, override_fields=[])
        RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            planning_context=baseline,
            title='Baseline analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
        )
        project_run = RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            planning_context=project,
            title='Project analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
        )

        payload = async_to_sync(get_roadmap_analysis_status)(
            workspace.slug,
            planning_context_uuid=project.uuid,
        )

        self.assertTrue(payload.has_analysis)
        self.assertEqual(payload.latest_run.uuid, project_run.uuid)
