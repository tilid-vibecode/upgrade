from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class IntakeWorkspaceResponse(BaseModel):
    uuid: UUID
    name: str
    slug: str
    notes: str = ''
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WorkspaceCompanyProfilePayload(BaseModel):
    company_name: str = ''
    website_url: str = ''
    company_description: str = ''
    main_products: List[str] = Field(default_factory=list)
    primary_market_geography: str = ''
    locations: List[str] = Field(default_factory=list)
    target_customers: List[str] = Field(default_factory=list)
    current_tech_stack: List[str] = Field(default_factory=list)
    planned_tech_stack: List[str] = Field(default_factory=list)
    rough_employee_count: Optional[int] = Field(default=None, ge=1)
    pilot_scope_notes: str = ''
    notable_constraints_or_growth_plans: str = ''


class WorkspacePilotScopePayload(BaseModel):
    scope_mode: str = ''
    departments_in_scope: List[str] = Field(default_factory=list)
    roles_in_scope: List[str] = Field(default_factory=list)
    products_in_scope: List[str] = Field(default_factory=list)
    employee_count_in_scope: Optional[int] = Field(default=None, ge=1)
    stakeholder_contact: str = ''
    analyst_notes: str = ''


class WorkspaceSourceChecklistPayload(BaseModel):
    existing_matrix_available: Optional[bool] = None
    sales_growth_plan_available: Optional[bool] = None
    architecture_overview_available: Optional[bool] = None
    product_notes_available: Optional[bool] = None
    hr_notes_available: Optional[bool] = None
    notes: str = ''


class IntakeWorkspaceDetailResponse(IntakeWorkspaceResponse):
    metadata_schema_version: str = ''
    company_profile: WorkspaceCompanyProfilePayload = Field(
        default_factory=WorkspaceCompanyProfilePayload
    )
    pilot_scope: WorkspacePilotScopePayload = Field(
        default_factory=WorkspacePilotScopePayload
    )
    source_checklist: WorkspaceSourceChecklistPayload = Field(
        default_factory=WorkspaceSourceChecklistPayload
    )
    operator_notes: str = ''
    operator_token: str = ''


class SourceDocumentResponse(BaseModel):
    uuid: UUID
    workspace_slug: str
    original_filename: str
    content_type: str
    file_size: int
    document_kind: str
    status: str
    persistent_key: str
    processing_key: str
    download_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CompanyDocumentUploadResponse(BaseModel):
    workspace: IntakeWorkspaceResponse
    document: SourceDocumentResponse


class WorkspaceDocumentListResponse(BaseModel):
    workspace: IntakeWorkspaceResponse
    documents: List[SourceDocumentResponse]


class WorkspaceCreateRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    notes: str = ''
    company_profile: Optional[WorkspaceCompanyProfilePayload] = None
    pilot_scope: Optional[WorkspacePilotScopePayload] = None
    source_checklist: Optional[WorkspaceSourceChecklistPayload] = None
    operator_notes: Optional[str] = None


class WorkspaceProfileUpdateRequest(BaseModel):
    company_profile: Optional[WorkspaceCompanyProfilePayload] = None
    pilot_scope: Optional[WorkspacePilotScopePayload] = None
    source_checklist: Optional[WorkspaceSourceChecklistPayload] = None
    operator_notes: Optional[str] = None
    notes: Optional[str] = None


class WorkspaceSourceCreateRequest(BaseModel):
    source_kind: str
    transport: str
    media_file_uuid: Optional[UUID] = None
    external_url: Optional[str] = None
    inline_text: Optional[str] = None
    title: str = ''
    notes: str = ''
    language_code: str = ''


class WorkspaceSourceUpdateRequest(BaseModel):
    source_kind: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    language_code: Optional[str] = None
    external_url: Optional[str] = None
    inline_text: Optional[str] = None


class WorkspaceSourceResponse(BaseModel):
    uuid: UUID
    workspace_slug: str
    title: str
    notes: str = ''
    source_kind: str
    transport: str
    media_file_uuid: Optional[UUID] = None
    media_filename: Optional[str] = None
    external_url: str = ''
    inline_text: str = ''
    language_code: str = ''
    status: str
    parse_error: str = ''
    parse_metadata: dict = Field(default_factory=dict)
    archived_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class WorkspaceSourceListResponse(BaseModel):
    workspace: IntakeWorkspaceResponse
    sources: List[WorkspaceSourceResponse]


class WorkspaceSectionCompletenessResponse(BaseModel):
    completed_fields: int
    total_fields: int
    completion_ratio: float
    missing_required_fields: List[str] = Field(default_factory=list)
    missing_recommended_fields: List[str] = Field(default_factory=list)
    is_complete: bool


class WorkspaceSourceRequirementResponse(BaseModel):
    key: str
    label: str
    required: bool = True
    required_for_parse: bool = False
    required_for_roadmap_analysis: bool = False
    required_for_blueprint: bool = False
    required_for_evidence: bool = False
    source_kinds: List[str] = Field(default_factory=list)
    required_min_count: int = 1
    attached_count: int = 0
    parsed_count: int = 0
    is_satisfied: bool
    is_parsed_ready: bool
    notes: List[str] = Field(default_factory=list)


class WorkspaceReadinessFlagsResponse(BaseModel):
    ready_for_parse: bool
    ready_for_roadmap_analysis: bool = False
    ready_for_blueprint: bool
    ready_for_evidence: bool
    ready_for_assessments: bool
    ready_for_matrix: bool = False
    ready_for_plans: bool = False


class WorkspaceBlueprintStateResponse(BaseModel):
    review_ready: bool = False
    published: bool = False


class WorkspaceStageBlockersResponse(BaseModel):
    context: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    parse: List[str] = Field(default_factory=list)
    roadmap_analysis: List[str] = Field(default_factory=list)
    blueprint: List[str] = Field(default_factory=list)
    clarifications: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    assessments: List[str] = Field(default_factory=list)
    matrix: List[str] = Field(default_factory=list)
    plans: List[str] = Field(default_factory=list)


class WorkspaceReadinessResponse(BaseModel):
    workspace: IntakeWorkspaceDetailResponse
    company_profile_completeness: WorkspaceSectionCompletenessResponse
    pilot_scope_completeness: WorkspaceSectionCompletenessResponse
    source_requirements: List[WorkspaceSourceRequirementResponse]
    source_counts: dict = Field(default_factory=dict)
    parsed_source_counts: dict = Field(default_factory=dict)
    total_attached_sources: int = 0
    total_parsed_sources: int = 0
    current_stage: str = 'parse'
    blueprint_state: WorkspaceBlueprintStateResponse = Field(
        default_factory=WorkspaceBlueprintStateResponse
    )
    stage_blockers: WorkspaceStageBlockersResponse = Field(
        default_factory=WorkspaceStageBlockersResponse
    )
    blocking_items: List[str] = Field(default_factory=list)
    readiness: WorkspaceReadinessFlagsResponse


class WorkspaceWorkflowStageResponse(BaseModel):
    key: str
    label: str
    status: str
    dependencies: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    recommended_action: str = ''
    latest_run_uuid: Optional[UUID] = None
    metadata: dict = Field(default_factory=dict)


class WorkspaceWorkflowSummaryResponse(BaseModel):
    current_stage_key: str = ''
    next_stage_key: str = ''
    total_blocker_count: int = 0
    latest_blueprint_status: str = ''
    blueprint_published: bool = False
    latest_assessment_status: str = ''
    assessment_completion_rate: float = 0.0
    latest_matrix_status: str = ''
    latest_plan_status: str = ''
    latest_blueprint_run_uuid: Optional[UUID] = None
    current_published_blueprint_run_uuid: Optional[UUID] = None
    latest_assessment_cycle_uuid: Optional[UUID] = None
    latest_matrix_run_uuid: Optional[UUID] = None
    latest_team_plan_uuid: Optional[UUID] = None


class WorkspaceWorkflowStatusResponse(BaseModel):
    workspace: IntakeWorkspaceDetailResponse
    stages: List[WorkspaceWorkflowStageResponse] = Field(default_factory=list)
    summary: WorkspaceWorkflowSummaryResponse = Field(default_factory=WorkspaceWorkflowSummaryResponse)


class ParseSourcesRequest(BaseModel):
    source_uuids: List[UUID] = Field(default_factory=list)
    force: bool = False


class ParsedSourceResult(BaseModel):
    source_uuid: UUID
    source_kind: str
    status: str
    parse_error: str = ''
    parse_metadata: dict = Field(default_factory=dict)


class ParseSourcesResponse(BaseModel):
    workspace: IntakeWorkspaceResponse
    processed: int
    results: List[ParsedSourceResult]
