from asgiref.sync import async_to_sync
from django.test import TestCase
from fastapi import HTTPException
from unittest.mock import AsyncMock, patch

from media_storage.models import MediaFile

from .entities import (
    WorkspaceCompanyProfilePayload,
    WorkspacePilotScopePayload,
    WorkspaceProfileUpdateRequest,
    WorkspaceSourceChecklistPayload,
    WorkspaceSourceCreateRequest,
    WorkspaceSourceUpdateRequest,
)
from .models import (
    IntakeWorkspace,
    WorkspaceSource,
    WorkspaceSourceKind,
    WorkspaceSourceStatus,
    WorkspaceSourceTransport,
)
from .services import (
    archive_workspace_source,
    build_workspace_detail_response,
    build_workspace_readiness_response,
    build_workspace_source_download_response,
    build_workspace_source_response,
    build_workspace_workflow_status_response,
    create_workspace_source,
    get_or_create_workspace,
    list_workspace_sources,
    update_workspace_source,
    update_workspace_profile,
    validate_workspace_source_payload,
)


class CompanyIntakeStageOneTests(TestCase):
    def _create_media_file(self, *, filename: str, file_category: str, content_type: str) -> MediaFile:
        return MediaFile.objects.create(
            original_filename=filename,
            content_type=content_type,
            file_size=1024,
            file_category=file_category,
            persistent_key=f'test/{filename}',
            status=MediaFile.Status.UPLOADED,
        )

    def _complete_profile_kwargs(self) -> dict:
        return {
            'company_profile': WorkspaceCompanyProfilePayload(
                company_name='Acme Cloud',
                website_url='https://acme.example.com',
                company_description='B2B workflow software for mid-market teams.',
                main_products=['Acme Core', 'Acme Insights'],
                primary_market_geography='EU and North America',
                locations=['Remote', 'Europe'],
                target_customers=['Operations leaders', 'Mid-market SaaS companies'],
                current_tech_stack=['Python', 'React', 'Postgres'],
                planned_tech_stack=['FastAPI', 'Qdrant'],
                rough_employee_count=54,
                pilot_scope_notes='Pilot starts with product and engineering.',
            ),
            'pilot_scope': WorkspacePilotScopePayload(
                scope_mode='selected_functions',
                departments_in_scope=['Engineering', 'Product'],
                roles_in_scope=['Backend Engineer', 'Product Manager'],
                products_in_scope=['Acme Core'],
                employee_count_in_scope=18,
                stakeholder_contact='Alex Product Ops',
                analyst_notes='Keep the first wave focused on roadmap execution.',
            ),
            'source_checklist': WorkspaceSourceChecklistPayload(
                existing_matrix_available=False,
                architecture_overview_available=True,
                product_notes_available=True,
            ),
            'operator_notes': 'Initial pilot intake created by analyst.',
        }

    def _create_completed_roadmap_analysis(self, workspace: IntakeWorkspace):
        from org_context.models import RoadmapAnalysisRun

        return RoadmapAnalysisRun.objects.create(
            workspace=workspace,
            title='Roadmap analysis',
            status=RoadmapAnalysisRun.Status.COMPLETED,
            initiatives=[{'id': 'init-marketplace', 'name': 'Marketplace', 'goal': 'Launch marketplace', 'criticality': 'high', 'planned_window': 'Q2', 'source_refs': [], 'confidence': 0.9}],
            workstreams=[{'id': 'ws-marketplace-api', 'initiative_id': 'init-marketplace', 'name': 'Marketplace API', 'scope': 'API work', 'delivery_type': 'backend_service', 'affected_systems': [], 'team_shape': {'roles_needed': ['Backend Engineer']}, 'required_capabilities': [], 'confidence': 0.8}],
            capability_bundles=[],
            dependencies=[],
            delivery_risks=[],
            prd_summaries=[],
            clarification_questions=[],
        )

    def test_workspace_create_persists_structured_profile_metadata(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='legacy note',
            **self._complete_profile_kwargs(),
        )

        workspace.refresh_from_db()
        detail = build_workspace_detail_response(workspace)

        self.assertEqual(detail.company_profile.company_name, 'Acme Cloud')
        self.assertEqual(detail.company_profile.rough_employee_count, 54)
        self.assertEqual(detail.company_profile.locations, ['Remote', 'Europe'])
        self.assertEqual(detail.company_profile.current_tech_stack, ['Python', 'React', 'Postgres'])
        self.assertEqual(detail.pilot_scope.stakeholder_contact, 'Alex Product Ops')
        self.assertFalse(detail.source_checklist.existing_matrix_available)
        self.assertEqual(detail.metadata_schema_version, 'stage1-v1')
        self.assertEqual(workspace.metadata['company_profile']['company_name'], 'Acme Cloud')

    def test_workspace_profile_patch_updates_metadata(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')

        updated = async_to_sync(update_workspace_profile)(
            workspace,
            WorkspaceProfileUpdateRequest(
                company_profile=WorkspaceCompanyProfilePayload(
                    company_name='Acme Cloud',
                    company_description='Updated profile',
                    main_products=['Acme Core'],
                    primary_market_geography='EU',
                    locations=['Berlin', 'Remote'],
                    target_customers=['Enterprise buyers'],
                    current_tech_stack=['Python'],
                    planned_tech_stack=['FastAPI', 'OpenAI API'],
                    rough_employee_count=60,
                ),
                pilot_scope=WorkspacePilotScopePayload(
                    scope_mode='whole_company',
                    stakeholder_contact='Dana Founder',
                ),
                operator_notes='Updated after kickoff call.',
            ),
        )

        updated.refresh_from_db()
        detail = build_workspace_detail_response(updated)
        self.assertEqual(detail.company_profile.company_description, 'Updated profile')
        self.assertEqual(detail.company_profile.locations, ['Berlin', 'Remote'])
        self.assertEqual(detail.company_profile.planned_tech_stack, ['FastAPI', 'OpenAI API'])
        self.assertEqual(detail.operator_notes, 'Updated after kickoff call.')
        self.assertEqual(detail.pilot_scope.scope_mode, 'whole_company')

    def test_validate_workspace_source_payload_requires_exactly_one_transport(self):
        body = WorkspaceSourceCreateRequest(
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file_uuid='11111111-1111-1111-1111-111111111111',
            external_url='https://example.com/roadmap',
        )

        with self.assertRaises(HTTPException) as exc:
            validate_workspace_source_payload(body)

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('Exactly one transport payload', exc.exception.detail)

    def test_workspace_source_status_includes_parsing_state(self):
        self.assertIn(('parsing', 'Parsing'), WorkspaceSourceStatus.choices)

    def test_validate_workspace_source_payload_rejects_non_spreadsheet_org_source(self):
        media_file = self._create_media_file(
            filename='org.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        body = WorkspaceSourceCreateRequest(
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file_uuid=media_file.uuid,
        )

        with self.assertRaises(HTTPException) as exc:
            validate_workspace_source_payload(body, media_file=media_file)

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('compatible file category', exc.exception.detail)

    def test_create_workspace_source_rejects_non_spreadsheet_org_source(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        media_file = self._create_media_file(
            filename='org.pdf',
            file_category='document',
            content_type='application/pdf',
        )

        with self.assertRaises(HTTPException) as exc:
            async_to_sync(create_workspace_source)(
                workspace,
                WorkspaceSourceCreateRequest(
                    source_kind=WorkspaceSourceKind.ORG_CSV,
                    transport=WorkspaceSourceTransport.MEDIA_FILE,
                    media_file_uuid=media_file.uuid,
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('compatible file category', exc.exception.detail)

    def test_readiness_reports_missing_required_inputs(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)

        self.assertFalse(readiness.readiness.ready_for_parse)
        self.assertEqual(readiness.current_stage, 'context')
        self.assertIn(
            'short company description',
            readiness.company_profile_completeness.missing_required_fields,
        )
        self.assertIn('Missing company profile field: short company description.', readiness.blocking_items)
        self.assertEqual(readiness.blocking_items, readiness.stage_blockers.context)

    def test_readiness_turns_true_without_cvs_when_stage_two_inputs_exist(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **{
                **self._complete_profile_kwargs(),
                'source_checklist': WorkspaceSourceChecklistPayload(
                    existing_matrix_available=False,
                    architecture_overview_available=False,
                    product_notes_available=False,
                ),
            },
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )

        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=roadmap_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=org_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='JD',
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=jd_file,
            status=WorkspaceSourceStatus.PARSED,
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        employee_cv_requirement = next(
            item for item in readiness.source_requirements if item.key == 'employee_cv_set'
        )

        self.assertTrue(readiness.readiness.ready_for_parse)
        self.assertFalse(readiness.readiness.ready_for_blueprint)
        self.assertFalse(readiness.readiness.ready_for_evidence)
        self.assertEqual(readiness.current_stage, 'roadmap_analysis')
        self.assertFalse(employee_cv_requirement.required_for_parse)
        self.assertFalse(employee_cv_requirement.is_satisfied)
        self.assertEqual(readiness.parsed_source_counts[WorkspaceSourceKind.ROADMAP], 1)
        self.assertFalse(
            any('Employee CV set' in blocker for blocker in readiness.stage_blockers.parse)
        )
        self.assertFalse(
            any('Employee CV set' in blocker for blocker in readiness.stage_blockers.blueprint)
        )
        self.assertEqual(readiness.blocking_items, [])

    def test_workflow_keeps_parse_completed_when_only_blueprint_inputs_are_missing(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=roadmap_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=org_file,
            status=WorkspaceSourceStatus.PARSED,
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['parse'].status, 'completed')
        self.assertEqual(stages['parse'].blockers, [])
        self.assertEqual(stages['roadmap_analysis'].status, 'ready')
        self.assertEqual(stages['roadmap_analysis'].dependencies, ['parse'])
        self.assertEqual(stages['blueprint'].status, 'blocked')
        self.assertIn('Complete roadmap analysis before generating blueprint.', stages['blueprint'].blockers)
        self.assertFalse(any('Job descriptions' in blocker for blocker in stages['blueprint'].blockers))
        self.assertEqual(payload.summary.current_stage_key, 'roadmap_analysis')

    def test_readiness_requires_completed_roadmap_analysis_before_blueprint(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        for title, kind, media_file in [
            ('Roadmap', WorkspaceSourceKind.ROADMAP, roadmap_file),
            ('Org', WorkspaceSourceKind.ORG_CSV, org_file),
            ('JD', WorkspaceSourceKind.JOB_DESCRIPTION, jd_file),
        ]:
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=title,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        self.assertFalse(readiness.readiness.ready_for_blueprint)
        self.assertIn('Complete roadmap analysis before generating blueprint.', readiness.stage_blockers.blueprint)

        self._create_completed_roadmap_analysis(workspace)
        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        self.assertTrue(readiness.readiness.ready_for_blueprint)

    def test_assessments_stay_blocked_until_cv_evidence_exists(self):
        from org_context.models import Employee, EmployeeSkillEvidence, Skill
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        self._create_completed_roadmap_analysis(workspace)

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        cv_file = self._create_media_file(
            filename='cv.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        for title, kind, media_file in [
            ('Roadmap', WorkspaceSourceKind.ROADMAP, roadmap_file),
            ('Org', WorkspaceSourceKind.ORG_CSV, org_file),
            ('JD', WorkspaceSourceKind.JOB_DESCRIPTION, jd_file),
            ('CV', WorkspaceSourceKind.EMPLOYEE_CV, cv_file),
        ]:
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=title,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Example',
            email='alex@example.com',
            current_title='Backend Engineer',
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        self.assertTrue(readiness.readiness.ready_for_evidence)
        self.assertFalse(readiness.readiness.ready_for_assessments)
        self.assertIn('Assessments depend on completed CV evidence and role matching.', readiness.stage_blockers.assessments)

        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            display_name_ru='Python',
            resolution_status=Skill.ResolutionStatus.RESOLVED,
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.9,
            weight=0.9,
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        self.assertTrue(readiness.readiness.ready_for_assessments)

    def test_workflow_treats_existing_blueprint_run_as_active_even_if_upstream_blueprint_inputs_are_incomplete(self):
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=roadmap_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=org_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.NEEDS_CLARIFICATION,
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['blueprint'].status, 'action_required')
        self.assertEqual(stages['blueprint'].blockers, [])
        self.assertIn('clarifications', stages['blueprint'].recommended_action.lower())

    def test_workflow_marks_draft_blueprint_as_action_required_when_no_effective_run_exists(self):
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )

        for title, source_kind, media_file in [
            ('Roadmap', WorkspaceSourceKind.ROADMAP, roadmap_file),
            ('Org', WorkspaceSourceKind.ORG_CSV, org_file),
            ('JD', WorkspaceSourceKind.JOB_DESCRIPTION, jd_file),
        ]:
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=title,
                source_kind=source_kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Draft blueprint',
            status=BlueprintStatus.DRAFT,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['blueprint'].status, 'action_required')
        self.assertIn('review the generated blueprint run', stages['blueprint'].recommended_action.lower())

    def test_workflow_keeps_blueprint_completed_when_published_run_exists_alongside_newer_draft(self):
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )

        for title, source_kind, media_file in [
            ('Roadmap', WorkspaceSourceKind.ROADMAP, roadmap_file),
            ('Org', WorkspaceSourceKind.ORG_CSV, org_file),
            ('JD', WorkspaceSourceKind.JOB_DESCRIPTION, jd_file),
        ]:
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=title,
                source_kind=source_kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='New draft revision',
            status=BlueprintStatus.DRAFT,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['blueprint'].status, 'completed')
        self.assertIn('published blueprint', stages['blueprint'].recommended_action.lower())

    def test_readiness_exposes_published_blueprint_gate_for_downstream_stages(self):
        from org_context.models import Employee
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        jd_file = self._create_media_file(
            filename='jd.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        cv_file = self._create_media_file(
            filename='cv.pdf',
            file_category='document',
            content_type='application/pdf',
        )

        for title, kind, media_file in [
            ('Roadmap', WorkspaceSourceKind.ROADMAP, roadmap_file),
            ('Org', WorkspaceSourceKind.ORG_CSV, org_file),
            ('JD', WorkspaceSourceKind.JOB_DESCRIPTION, jd_file),
            ('CV', WorkspaceSourceKind.EMPLOYEE_CV, cv_file),
        ]:
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=title,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        Employee.objects.create(
            workspace=workspace,
            full_name='Alex Example',
            email='alex@example.com',
            current_title='Backend Engineer',
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Reviewed blueprint',
            status=BlueprintStatus.REVIEWED,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)

        self.assertTrue(readiness.readiness.ready_for_parse)
        self.assertFalse(readiness.readiness.ready_for_blueprint)
        self.assertFalse(readiness.readiness.ready_for_evidence)
        self.assertFalse(readiness.readiness.ready_for_assessments)
        self.assertTrue(readiness.blueprint_state.review_ready)
        self.assertFalse(readiness.blueprint_state.published)
        self.assertEqual(readiness.current_stage, 'clarifications')
        self.assertIn(
            'Blueprint review and publication must be completed before downstream evidence can begin.',
            readiness.stage_blockers.clarifications,
        )
        self.assertIn('CV evidence build requires a published blueprint.', readiness.stage_blockers.evidence)
        self.assertIn('Assessment generation requires a published blueprint.', readiness.stage_blockers.assessments)
        self.assertEqual(readiness.blocking_items, readiness.stage_blockers.clarifications)

    def test_optional_existing_matrix_is_satisfied_when_absent(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        existing_matrix_requirement = next(
            item for item in readiness.source_requirements if item.key == 'existing_matrix'
        )

        self.assertFalse(existing_matrix_requirement.required)
        self.assertTrue(existing_matrix_requirement.is_satisfied)
        self.assertTrue(existing_matrix_requirement.is_parsed_ready)

    def test_optional_role_references_do_not_block_blueprint_without_uploads(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        roadmap_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        org_file = self._create_media_file(
            filename='org.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        matrix_file = self._create_media_file(
            filename='matrix.xlsx',
            file_category='spreadsheet',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=roadmap_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=org_file,
            status=WorkspaceSourceStatus.PARSED,
        )
        WorkspaceSource.objects.create(
            workspace=workspace,
            title='Existing matrix',
            source_kind=WorkspaceSourceKind.EXISTING_MATRIX,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=matrix_file,
            status=WorkspaceSourceStatus.PARSED,
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        role_reference_requirement = next(
            item for item in readiness.source_requirements if item.key == 'role_references'
        )
        existing_matrix_requirement = next(
            item for item in readiness.source_requirements if item.key == 'existing_matrix'
        )

        self.assertFalse(role_reference_requirement.required)
        self.assertFalse(role_reference_requirement.required_for_blueprint)
        self.assertTrue(role_reference_requirement.is_satisfied)
        self.assertTrue(existing_matrix_requirement.is_satisfied)
        self.assertTrue(readiness.readiness.ready_for_parse)
        self.assertFalse(readiness.readiness.ready_for_blueprint)
        self.assertFalse(
            any('Job descriptions' in blocker for blocker in readiness.stage_blockers.blueprint)
        )

    def test_create_workspace_source_claims_media_ownership_and_rejects_cross_workspace_attach(self):
        workspace_a = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        workspace_b = async_to_sync(get_or_create_workspace)(company_name='Beta Cloud', notes='')
        media_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        # Pre-assign workspace ownership (simulates upload via workspace media endpoint)
        media_file.prototype_workspace = workspace_a
        media_file.save(update_fields=['prototype_workspace', 'updated_at'])

        source = async_to_sync(create_workspace_source)(
            workspace_a,
            WorkspaceSourceCreateRequest(
                source_kind=WorkspaceSourceKind.ROADMAP,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file_uuid=media_file.uuid,
            ),
        )

        media_file.refresh_from_db()
        self.assertEqual(media_file.prototype_workspace, workspace_a)
        self.assertEqual(source.media_file, media_file)

        with self.assertRaises(HTTPException) as exc:
            async_to_sync(create_workspace_source)(
                workspace_b,
                WorkspaceSourceCreateRequest(
                    source_kind=WorkspaceSourceKind.ROADMAP,
                    transport=WorkspaceSourceTransport.MEDIA_FILE,
                    media_file_uuid=media_file.uuid,
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('does not belong to this workspace', exc.exception.detail)

    def test_create_workspace_source_rejects_non_prototype_media(self):
        from organization.models import Organization

        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        organization = Organization.objects.create(name='Internal Org')
        media_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        media_file.organization = organization
        media_file.save(update_fields=['organization', 'updated_at'])

        with self.assertRaises(HTTPException) as exc:
            async_to_sync(create_workspace_source)(
                workspace,
                WorkspaceSourceCreateRequest(
                    source_kind=WorkspaceSourceKind.ROADMAP,
                    transport=WorkspaceSourceTransport.MEDIA_FILE,
                    media_file_uuid=media_file.uuid,
                ),
            )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn('Only prototype workspace media files', exc.exception.detail)

    def test_update_workspace_source_resets_parse_state_for_structural_changes(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        media_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        media_file.prototype_workspace = workspace
        media_file.save(update_fields=['prototype_workspace', 'updated_at'])
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=media_file,
            status=WorkspaceSourceStatus.PARSED,
            parse_metadata={'parser': 'prototype-v1'},
        )

        updated = async_to_sync(update_workspace_source)(
            source,
            WorkspaceSourceUpdateRequest(
                source_kind=WorkspaceSourceKind.STRATEGY,
                language_code='en',
            ),
        )

        self.assertEqual(updated.source_kind, WorkspaceSourceKind.STRATEGY)
        self.assertEqual(updated.language_code, 'en')
        self.assertEqual(updated.status, WorkspaceSourceStatus.ATTACHED)
        self.assertEqual(updated.parse_metadata, {})

    def test_archive_workspace_source_excludes_it_from_default_lists_and_readiness(self):
        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        media_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        media_file.prototype_workspace = workspace
        media_file.save(update_fields=['prototype_workspace', 'updated_at'])
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=media_file,
            status=WorkspaceSourceStatus.PARSED,
        )

        async_to_sync(archive_workspace_source)(source)

        active_sources = async_to_sync(list_workspace_sources)(workspace)
        archived_sources = async_to_sync(list_workspace_sources)(workspace, include_archived=True)
        readiness = async_to_sync(build_workspace_readiness_response)(workspace)

        self.assertEqual(active_sources, [])
        self.assertEqual(len(archived_sources), 1)
        self.assertEqual(archived_sources[0].status, WorkspaceSourceStatus.ARCHIVED)
        self.assertEqual(readiness.total_attached_sources, 0)

    def test_workflow_status_reports_completed_downstream_pipeline(self):
        from development_plans.models import DevelopmentPlanRun, PlanRunStatus, PlanScope
        from employee_assessment.models import AssessmentCycle, AssessmentPackStatus, AssessmentStatus, EmployeeAssessmentPack
        from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
        from org_context.models import Employee, EmployeeSkillEvidence, Skill
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        for filename, kind, category, content_type in [
            ('roadmap.pdf', WorkspaceSourceKind.ROADMAP, 'document', 'application/pdf'),
            ('org.xlsx', WorkspaceSourceKind.ORG_CSV, 'spreadsheet', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('jd.pdf', WorkspaceSourceKind.JOB_DESCRIPTION, 'document', 'application/pdf'),
            ('cv.pdf', WorkspaceSourceKind.EMPLOYEE_CV, 'document', 'application/pdf'),
        ]:
            media_file = self._create_media_file(
                filename=filename,
                file_category=category,
                content_type=content_type,
            )
            media_file.prototype_workspace = workspace
            media_file.save(update_fields=['prototype_workspace', 'updated_at'])
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=filename,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Example',
            email='alex@example.com',
            current_title='Backend Engineer',
        )
        blueprint = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Initial cycle',
            status=AssessmentStatus.COMPLETED,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Alex Example assessment',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_payload={'questions': []},
            response_payload={'final_submit': True},
            fused_summary={'submitted_skill_rows': []},
            submitted_at=cycle.updated_at,
        )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            source='test',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.8,
            weight=0.7,
            evidence_text='Python and APIs.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='self_assessment',
            current_level=3,
            confidence=0.7,
            weight=0.4,
            evidence_text='Built integrations.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )
        matrix_run = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            input_snapshot={'selected_assessment_cycle_uuid': str(cycle.uuid)},
            matrix_payload={'employees': []},
            summary_payload={},
            heatmap_payload={},
            risk_payload={},
            incompleteness_payload={},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=matrix_run,
            title='Final development plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            is_current=True,
            summary={},
            plan_payload={},
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['context'].status, 'completed')
        self.assertEqual(stages['sources'].status, 'completed')
        self.assertEqual(stages['parse'].status, 'completed')
        self.assertEqual(stages['blueprint'].status, 'completed')
        self.assertEqual(stages['clarifications'].status, 'completed')
        self.assertEqual(stages['evidence'].status, 'completed')
        self.assertEqual(stages['assessments'].status, 'completed')
        self.assertEqual(stages['matrix'].status, 'completed')
        self.assertEqual(stages['plans'].status, 'completed')
        self.assertEqual(payload.summary.current_published_blueprint_run_uuid, blueprint.uuid)
        self.assertEqual(payload.summary.latest_assessment_cycle_uuid, cycle.uuid)
        self.assertEqual(payload.summary.latest_matrix_run_uuid, matrix_run.uuid)

    def test_readiness_allows_matrix_build_once_evidence_and_submissions_exist(self):
        from employee_assessment.models import AssessmentCycle, AssessmentPackStatus, AssessmentStatus, EmployeeAssessmentPack
        from org_context.models import Employee, EmployeeSkillEvidence, Skill
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        for filename, kind, category, content_type in [
            ('roadmap.pdf', WorkspaceSourceKind.ROADMAP, 'document', 'application/pdf'),
            ('org.xlsx', WorkspaceSourceKind.ORG_CSV, 'spreadsheet', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('jd.pdf', WorkspaceSourceKind.JOB_DESCRIPTION, 'document', 'application/pdf'),
            ('cv.pdf', WorkspaceSourceKind.EMPLOYEE_CV, 'document', 'application/pdf'),
        ]:
            media_file = self._create_media_file(
                filename=filename,
                file_category=category,
                content_type=content_type,
            )
            media_file.prototype_workspace = workspace
            media_file.save(update_fields=['prototype_workspace', 'updated_at'])
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=filename,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Example',
            email='alex@example.com',
            current_title='Backend Engineer',
        )
        blueprint = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        cycle = AssessmentCycle.objects.create(
            workspace=workspace,
            title='Initial cycle',
            status=AssessmentStatus.RUNNING,
            blueprint_run=blueprint,
            uses_self_report=True,
            configuration={},
            result_summary={},
        )
        EmployeeAssessmentPack.objects.create(
            cycle=cycle,
            employee=employee,
            title='Alex Example assessment',
            status=AssessmentPackStatus.SUBMITTED,
            questionnaire_payload={'questions': []},
            response_payload={'final_submit': True},
            fused_summary={'submitted_skill_rows': []},
            submitted_at=cycle.updated_at,
        )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            source='test',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.8,
            weight=0.7,
            evidence_text='Python and APIs.',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='self_assessment',
            current_level=3,
            confidence=0.7,
            weight=0.4,
            evidence_text='Built integrations.',
            metadata={'assessment_cycle_uuid': str(cycle.uuid)},
        )

        readiness = async_to_sync(build_workspace_readiness_response)(workspace)

        self.assertTrue(readiness.readiness.ready_for_matrix)
        self.assertFalse(readiness.readiness.ready_for_plans)

    def test_workflow_marks_evidence_completed_once_cv_rows_exist(self):
        from org_context.models import Employee, EmployeeSkillEvidence, Skill
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )

        for filename, kind, category, content_type in [
            ('roadmap.pdf', WorkspaceSourceKind.ROADMAP, 'document', 'application/pdf'),
            ('org.xlsx', WorkspaceSourceKind.ORG_CSV, 'spreadsheet', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('jd.pdf', WorkspaceSourceKind.JOB_DESCRIPTION, 'document', 'application/pdf'),
        ]:
            media_file = self._create_media_file(
                filename=filename,
                file_category=category,
                content_type=content_type,
            )
            media_file.prototype_workspace = workspace
            media_file.save(update_fields=['prototype_workspace', 'updated_at'])
            WorkspaceSource.objects.create(
                workspace=workspace,
                title=filename,
                source_kind=kind,
                transport=WorkspaceSourceTransport.MEDIA_FILE,
                media_file=media_file,
                status=WorkspaceSourceStatus.PARSED,
            )

        employee = Employee.objects.create(
            workspace=workspace,
            full_name='Alex Example',
            email='alex@example.com',
            current_title='Backend Engineer',
        )
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            clarification_questions=[],
            review_summary={'clarification_summary': {'open': 0}},
        )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key='python',
            display_name_en='Python',
            source='test',
            metadata={},
        )
        EmployeeSkillEvidence.objects.create(
            workspace=workspace,
            employee=employee,
            skill=skill,
            source_kind='employee_cv',
            current_level=4,
            confidence=0.8,
            weight=0.7,
            evidence_text='Python and APIs.',
            metadata={},
        )

        payload = async_to_sync(build_workspace_workflow_status_response)(workspace)
        stages = {item.key: item for item in payload.stages}

        self.assertEqual(stages['evidence'].status, 'completed')
        self.assertEqual(payload.summary.current_stage_key, 'assessments')

    def test_workspace_source_download_response_uses_workspace_bound_media_file(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        media_file = self._create_media_file(
            filename='roadmap.pdf',
            file_category='document',
            content_type='application/pdf',
        )
        media_file.prototype_workspace = workspace
        media_file.save(update_fields=['prototype_workspace', 'updated_at'])
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.MEDIA_FILE,
            media_file=media_file,
            status=WorkspaceSourceStatus.ATTACHED,
        )

        with patch(
            'media_storage.services.generate_signed_url_for_file',
            new=AsyncMock(return_value='https://example.com/download'),
        ):
            response = async_to_sync(build_workspace_source_download_response)(source)

        self.assertEqual(response.url, 'https://example.com/download')
        self.assertEqual(response.file_uuid, media_file.uuid)

    def test_workspace_source_response_includes_inline_text(self):
        workspace = async_to_sync(get_or_create_workspace)(company_name='Acme Cloud', notes='')
        source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Inline note',
            source_kind=WorkspaceSourceKind.OTHER,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Important operator note',
            status=WorkspaceSourceStatus.ATTACHED,
        )

        response = build_workspace_source_response(source)

        self.assertEqual(response.inline_text, 'Important operator note')

    def test_readiness_scopes_source_requirements_to_effective_planning_context_sources(self):
        from org_context.models import ContextProfile, PlanningContext, PlanningContextSource

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        baseline = workspace.planning_contexts.get(slug='org-baseline')
        roadmap_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap content',
            status=WorkspaceSourceStatus.PARSED,
        )
        strategy_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Strategy content',
            status=WorkspaceSourceStatus.PARSED,
        )
        org_csv_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org CSV',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='employee_id,full_name\\n1,Alex',
            status=WorkspaceSourceStatus.PARSED,
        )
        PlanningContextSource.objects.create(
            planning_context=baseline,
            workspace_source=roadmap_source,
            usage_type='roadmap',
            include_in_blueprint=True,
            include_in_roadmap_analysis=True,
        )
        PlanningContextSource.objects.create(
            planning_context=baseline,
            workspace_source=strategy_source,
            usage_type='strategy',
            include_in_blueprint=True,
            include_in_roadmap_analysis=True,
        )
        PlanningContextSource.objects.create(
            planning_context=baseline,
            workspace_source=org_csv_source,
            usage_type='org_structure',
            include_in_blueprint=True,
            include_in_roadmap_analysis=False,
        )

        project_context = PlanningContext.objects.create(
            workspace=workspace,
            name='AI Features',
            slug='ai-features',
            kind=PlanningContext.Kind.SCENARIO,
            parent_context=baseline,
        )
        ContextProfile.objects.create(planning_context=project_context, override_fields=[])
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=roadmap_source,
            usage_type='roadmap',
            is_active=False,
            include_in_blueprint=False,
            include_in_roadmap_analysis=False,
        )
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=strategy_source,
            usage_type='strategy',
            is_active=False,
            include_in_blueprint=False,
            include_in_roadmap_analysis=False,
        )

        workspace_readiness = async_to_sync(build_workspace_readiness_response)(workspace)
        context_readiness = async_to_sync(build_workspace_readiness_response)(
            workspace,
            planning_context=project_context,
        )

        self.assertTrue(workspace_readiness.readiness.ready_for_roadmap_analysis)
        self.assertFalse(context_readiness.readiness.ready_for_roadmap_analysis)
        self.assertTrue(
            any('Roadmap' in blocker for blocker in context_readiness.stage_blockers.roadmap_analysis)
        )

    def test_readiness_respects_context_source_inclusion_flags(self):
        from org_context.models import ContextProfile, PlanningContext, PlanningContextSource

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        baseline = workspace.planning_contexts.get(slug='org-baseline')
        roadmap_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap content',
            status=WorkspaceSourceStatus.PARSED,
        )
        strategy_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Strategy content',
            status=WorkspaceSourceStatus.PARSED,
        )
        org_csv_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org CSV',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='employee_id,full_name\\n1,Alex',
            status=WorkspaceSourceStatus.PARSED,
        )
        jd_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='JD',
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Backend engineer role profile',
            status=WorkspaceSourceStatus.PARSED,
        )
        for source, usage_type, include_in_roadmap in [
            (roadmap_source, 'roadmap', True),
            (strategy_source, 'strategy', True),
            (org_csv_source, 'org_structure', False),
            (jd_source, 'role_reference', False),
        ]:
            PlanningContextSource.objects.create(
                planning_context=baseline,
                workspace_source=source,
                usage_type=usage_type,
                include_in_blueprint=True,
                include_in_roadmap_analysis=include_in_roadmap,
            )

        project_context = PlanningContext.objects.create(
            workspace=workspace,
            name='AI Features',
            slug='ai-features-flags',
            kind=PlanningContext.Kind.SCENARIO,
            parent_context=baseline,
        )
        ContextProfile.objects.create(planning_context=project_context, override_fields=[])
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=roadmap_source,
            usage_type='roadmap',
            include_in_blueprint=True,
            include_in_roadmap_analysis=False,
        )
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=strategy_source,
            usage_type='strategy',
            include_in_blueprint=True,
            include_in_roadmap_analysis=False,
        )
        PlanningContextSource.objects.create(
            planning_context=project_context,
            workspace_source=jd_source,
            usage_type='role_reference',
            include_in_blueprint=False,
            include_in_roadmap_analysis=False,
        )

        readiness = async_to_sync(build_workspace_readiness_response)(
            workspace,
            planning_context=project_context,
        )

        self.assertTrue(readiness.readiness.ready_for_parse)
        self.assertFalse(readiness.readiness.ready_for_roadmap_analysis)
        self.assertFalse(readiness.readiness.ready_for_blueprint)
        self.assertTrue(
            any('Roadmap or strategy' in blocker for blocker in readiness.stage_blockers.roadmap_analysis)
        )
        self.assertFalse(any('Job descriptions' in blocker for blocker in readiness.stage_blockers.blueprint))

    def test_context_workflow_requires_explicit_roadmap_lineage_even_when_blueprint_exists(self):
        from org_context.models import ContextProfile, PlanningContext, PlanningContextSource
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        baseline = workspace.planning_contexts.get(slug='org-baseline')
        roadmap_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Roadmap',
            source_kind=WorkspaceSourceKind.ROADMAP,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Roadmap content',
            status=WorkspaceSourceStatus.PARSED,
        )
        strategy_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Strategy',
            source_kind=WorkspaceSourceKind.STRATEGY,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Strategy content',
            status=WorkspaceSourceStatus.PARSED,
        )
        org_csv_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='Org CSV',
            source_kind=WorkspaceSourceKind.ORG_CSV,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='employee_id,full_name\\n1,Alex',
            status=WorkspaceSourceStatus.PARSED,
        )
        jd_source = WorkspaceSource.objects.create(
            workspace=workspace,
            title='JD',
            source_kind=WorkspaceSourceKind.JOB_DESCRIPTION,
            transport=WorkspaceSourceTransport.INLINE_TEXT,
            inline_text='Backend engineer role profile',
            status=WorkspaceSourceStatus.PARSED,
        )
        for source, usage_type, include_in_roadmap in [
            (roadmap_source, 'roadmap', True),
            (strategy_source, 'strategy', True),
            (org_csv_source, 'org_structure', False),
            (jd_source, 'role_reference', False),
        ]:
            PlanningContextSource.objects.create(
                planning_context=baseline,
                workspace_source=source,
                usage_type=usage_type,
                include_in_blueprint=True,
                include_in_roadmap_analysis=include_in_roadmap,
            )

        project_context = PlanningContext.objects.create(
            workspace=workspace,
            name='Project AI',
            slug='project-ai',
            kind=PlanningContext.Kind.SCENARIO,
            parent_context=baseline,
        )
        ContextProfile.objects.create(planning_context=project_context, override_fields=[])
        SkillBlueprintRun.objects.create(
            workspace=workspace,
            planning_context=project_context,
            title='Project blueprint',
            status=BlueprintStatus.DRAFT,
            is_published=False,
            role_candidates=[],
            required_skill_set=[],
        )

        readiness = async_to_sync(build_workspace_readiness_response)(
            workspace,
            planning_context=project_context,
        )
        workflow = async_to_sync(build_workspace_workflow_status_response)(
            workspace,
            planning_context=project_context,
        )
        roadmap_stage = next(stage for stage in workflow.stages if stage.key == 'roadmap_analysis')

        self.assertEqual(readiness.current_stage, 'roadmap_analysis')
        self.assertEqual(roadmap_stage.status, 'ready')
        self.assertFalse(roadmap_stage.metadata['legacy_blueprint_backfilled'])

    def test_workflow_plans_stage_requires_fresh_batch_for_current_matrix(self):
        from development_plans.models import DevelopmentPlanRun, PlanRunStatus, PlanScope
        from employee_assessment.models import AssessmentCycle, AssessmentStatus
        from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
        from skill_blueprint.models import BlueprintStatus, SkillBlueprintRun

        workspace = async_to_sync(get_or_create_workspace)(
            company_name='Acme Cloud',
            notes='',
            **self._complete_profile_kwargs(),
        )
        blueprint = SkillBlueprintRun.objects.create(
            workspace=workspace,
            title='Published blueprint',
            status=BlueprintStatus.APPROVED,
            is_published=True,
            role_candidates=[],
            required_skill_set=[],
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
        old_matrix = EvidenceMatrixRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            title='Old matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            input_snapshot={'selected_assessment_cycle_uuid': str(old_cycle.uuid)},
            matrix_payload={},
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
            title='Current matrix',
            status=EvidenceMatrixStatus.COMPLETED,
            source_type=EvidenceSourceType.MANUAL,
            input_snapshot={'selected_assessment_cycle_uuid': str(current_cycle.uuid)},
            matrix_payload={},
        )
        DevelopmentPlanRun.objects.create(
            workspace=workspace,
            blueprint_run=blueprint,
            matrix_run=old_matrix,
            title='Stale team plan',
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            is_current=False,
        )

        workflow = async_to_sync(build_workspace_workflow_status_response)(workspace)
        plans_stage = next(stage for stage in workflow.stages if stage.key == 'plans')

        self.assertEqual(plans_stage.status, 'ready')
        self.assertIn('fresh team and individual plan batch', plans_stage.recommended_action.lower())
