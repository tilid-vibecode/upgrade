from datetime import datetime
from typing import List, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from company_intake.entities import WorkspaceSourceResponse


PlanningContextKind = Literal['org', 'project', 'scenario']
PlanningContextStatus = Literal['active', 'archived', 'draft']
PlanningContextSourceUsage = Literal['roadmap', 'strategy', 'role_reference', 'org_structure', 'employee_cv', 'other']


class ParsedSourceResponse(BaseModel):
    uuid: UUID
    source_uuid: UUID
    source_kind: str = ''
    source_title: str = ''
    source_status: str = ''
    parse_error: str = ''
    parser_name: str
    parser_version: str
    content_type: str
    page_count: int | None = None
    word_count: int
    char_count: int
    chunk_count: int = 0
    warning_count: int = 0
    language_code: str = ''
    vector_index_status: str = ''
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SourceChunkResponse(BaseModel):
    chunk_index: int
    char_count: int
    text: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ParsedSourceListResponse(BaseModel):
    workspace_slug: str
    parsed_sources: List[ParsedSourceResponse]


class ParsedSourceDetailResponse(BaseModel):
    workspace_slug: str
    parsed_source: ParsedSourceResponse
    source: WorkspaceSourceResponse
    extracted_text: str = ''
    chunks: List[SourceChunkResponse] = Field(default_factory=list)


class ParsedSourceReparseResponse(BaseModel):
    workspace_slug: str
    source: WorkspaceSourceResponse
    parsed_source: ParsedSourceResponse | None = None
    status: str
    parse_error: str = ''
    parse_metadata: dict = Field(default_factory=dict)


class ParsedSourceReparseRequest(BaseModel):
    mapping_override: dict[str, str] = Field(default_factory=dict)


class OrgCsvPreviewRequest(BaseModel):
    mapping_override: dict[str, str] = Field(default_factory=dict)
    sample_row_count: int = Field(default=5, ge=1, le=20)


class OrgCsvPreviewResponse(BaseModel):
    workspace_slug: str
    source_uuid: UUID
    delimiter: str = ','
    row_count: int = 0
    headers: List[str] = Field(default_factory=list)
    inferred_mapping: dict = Field(default_factory=dict)
    effective_mapping: dict = Field(default_factory=dict)
    ambiguous_targets: dict = Field(default_factory=dict)
    missing_targets: List[str] = Field(default_factory=list)
    override_applied: dict = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    sample_rows: List[dict] = Field(default_factory=list)
    can_parse: bool = False


class EmployeeResponse(BaseModel):
    uuid: UUID
    full_name: str
    email: str = ''
    current_title: str = ''
    external_employee_id: str = ''
    metadata: dict = Field(default_factory=dict)
    cv_availability: 'EmployeeCvAvailabilityResponse' = Field(default_factory=lambda: EmployeeCvAvailabilityResponse())

    class Config:
        from_attributes = True


class OrgContextSummaryResponse(BaseModel):
    workspace_slug: str
    employee_count: int = 0
    org_unit_count: int = 0
    project_count: int = 0
    reporting_line_count: int = 0
    parsed_source_count: int = 0
    role_match_count: int = 0
    skill_evidence_count: int = 0


class PlanningContextProfilePayload(BaseModel):
    company_profile: dict = Field(default_factory=dict)
    tech_stack: List[str] = Field(default_factory=list)
    tech_stack_remove: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    growth_goals: List[str] = Field(default_factory=list)
    inherit_from_parent: bool = True
    override_fields: List[str] = Field(default_factory=list)


class PlanningContextSourceCreateRequest(BaseModel):
    workspace_source_uuid: UUID
    usage_type: PlanningContextSourceUsage | None = None
    include_in_blueprint: bool = True
    include_in_roadmap_analysis: bool | None = None
    is_active: bool = True


class PlanningContextCreateRequest(BaseModel):
    name: str
    slug: str
    kind: PlanningContextKind = 'project'
    parent_context_uuid: UUID | None = None
    project_uuid: UUID | None = None
    description: str = ''
    metadata: dict = Field(default_factory=dict)
    profile: PlanningContextProfilePayload = Field(default_factory=PlanningContextProfilePayload)


class PlanningContextUpdateRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    status: PlanningContextStatus | None = None
    description: str | None = None
    metadata: dict | None = None
    profile: PlanningContextProfilePayload | None = None


class PlanningContextSummaryResponse(BaseModel):
    uuid: UUID
    name: str
    slug: str
    kind: PlanningContextKind
    status: PlanningContextStatus
    parent_context_uuid: UUID | None = None
    child_count: int = 0
    source_count: int = 0
    has_blueprint: bool = False
    has_roadmap_analysis: bool = False

    class Config:
        from_attributes = True


class PlanningContextParentResponse(BaseModel):
    uuid: UUID
    name: str
    slug: str


class PlanningContextProjectResponse(BaseModel):
    uuid: UUID
    name: str


class PlanningContextSourceLinkResponse(BaseModel):
    uuid: UUID | None = None
    workspace_source_uuid: UUID
    title: str = ''
    source_kind: str = ''
    usage_type: PlanningContextSourceUsage = 'other'
    is_active: bool = True
    include_in_blueprint: bool = True
    include_in_roadmap_analysis: bool = False
    origin: str = 'direct'
    inherited_from_context_uuid: UUID | None = None
    inherited_from_context_slug: str = ''
    excluded_reason: str = ''


class PlanningContextDetailResponse(BaseModel):
    uuid: UUID
    name: str
    slug: str
    kind: PlanningContextKind
    status: PlanningContextStatus
    description: str = ''
    metadata: dict = Field(default_factory=dict)
    parent_context: PlanningContextParentResponse | None = None
    project: PlanningContextProjectResponse | None = None
    profile: PlanningContextProfilePayload = Field(default_factory=PlanningContextProfilePayload)
    effective_profile: dict = Field(default_factory=dict)
    sources: List[PlanningContextSourceLinkResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PlanningContextListResponse(BaseModel):
    workspace_slug: str
    contexts: List[PlanningContextSummaryResponse] = Field(default_factory=list)


class ProjectCreateRequest(BaseModel):
    name: str


class ProjectSummaryResponse(BaseModel):
    uuid: UUID
    name: str


class ProjectListResponse(BaseModel):
    workspace_slug: str
    projects: List[ProjectSummaryResponse] = Field(default_factory=list)


class RoadmapAnalysisRunRequest(BaseModel):
    force_rebuild: bool = False


class RoadmapAnalysisTriggerResponse(BaseModel):
    run_uuid: UUID
    status: str
    message: str = ''


class RoadmapAnalysisRunSummaryResponse(BaseModel):
    uuid: UUID
    title: str = ''
    status: str
    planning_context_uuid: UUID | None = None
    created_at: datetime
    updated_at: datetime
    initiative_count: int = 0
    workstream_count: int = 0
    bundle_count: int = 0
    risk_count: int = 0
    source_count: int = 0


class RoadmapAnalysisStatusResponse(BaseModel):
    has_analysis: bool = False
    latest_run: RoadmapAnalysisRunSummaryResponse | None = None


class RoadmapAnalysisRunResponse(BaseModel):
    uuid: UUID
    title: str
    status: str
    planning_context_uuid: UUID | None = None
    analysis_version: str = ''
    source_summary: dict = Field(default_factory=dict)
    input_snapshot: dict = Field(default_factory=dict)
    initiatives: List[dict] = Field(default_factory=list)
    workstreams: List[dict] = Field(default_factory=list)
    dependencies: List[dict] = Field(default_factory=list)
    delivery_risks: List[dict] = Field(default_factory=list)
    capability_bundles: List[dict] = Field(default_factory=list)
    prd_summaries: List[dict] = Field(default_factory=list)
    clarification_questions: List[dict] = Field(default_factory=list)
    error_message: str = ''
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmployeeListResponse(BaseModel):
    workspace_slug: str
    employees: List[EmployeeResponse]


class CVEvidenceBuildRequest(BaseModel):
    source_uuids: List[UUID] = Field(default_factory=list)


class CVMatchResolutionRequest(BaseModel):
    employee_uuid: UUID | None = None
    operator_name: str = ''
    resolution_note: str = ''


class EmployeeCvAvailabilityRequest(BaseModel):
    operator_name: str = ''
    note: str = ''


class EmployeeCvAvailabilityResponse(BaseModel):
    status: str = ''
    note: str = ''
    confirmed_by: str = ''
    confirmed_at: str = ''


class PendingSkillApprovalRequest(BaseModel):
    candidate_key: str
    approved_name_en: str
    approved_name_ru: str = ''
    alias_terms: List[str] = Field(default_factory=list)
    operator_name: str = ''
    approval_note: str = ''


class EmployeeSkillBulkReviewActionRequest(BaseModel):
    evidence_uuid: UUID
    action: str
    merge_target_skill_uuid: UUID | None = None
    note: str = ''


class EmployeeSkillBulkReviewRequest(BaseModel):
    actions: List[EmployeeSkillBulkReviewActionRequest] = Field(default_factory=list)


class EmployeeSkillBulkReviewResponse(BaseModel):
    processed: int = 0
    accepted: int = 0
    rejected: int = 0
    merged: int = 0
    errors: List[dict] = Field(default_factory=list)


class EmployeeSkillAcceptAllRequest(BaseModel):
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


class EmployeeSkillAcceptAllResponse(BaseModel):
    accepted_count: int = 0
    skipped_count: int = 0
    errors: List[dict] = Field(default_factory=list)


class PendingWorkspaceSkillResponse(BaseModel):
    skill_uuid: UUID
    canonical_key: str
    display_name_en: str = ''
    display_name_ru: str = ''
    employee_count: int = 0
    total_evidence_count: int = 0
    avg_confidence: float = 0.0
    sample_evidence_texts: List[str] = Field(default_factory=list)
    sample_employees: List[str] = Field(default_factory=list)
    similar_resolved_skills: List[dict] = Field(default_factory=list)


class WorkspacePendingSkillsResponse(BaseModel):
    pending_skills: List[PendingWorkspaceSkillResponse] = Field(default_factory=list)
    total_pending: int = 0
    total_resolved: int = 0


class WorkspaceSkillResolutionRequestItem(BaseModel):
    skill_uuid: UUID
    action: str
    target_skill_uuid: UUID | None = None
    target_esco_uri: str = ''
    display_name_en: str = ''
    display_name_ru: str = ''
    alias_terms: List[str] = Field(default_factory=list)
    note: str = ''


class WorkspaceSkillResolutionRequest(BaseModel):
    resolutions: List[WorkspaceSkillResolutionRequestItem] = Field(default_factory=list)


class WorkspaceSkillResolutionResponse(BaseModel):
    processed: int = 0
    approved: int = 0
    rejected: int = 0
    merged: int = 0
    errors: List[dict] = Field(default_factory=list)


class EmployeeDeleteResponse(BaseModel):
    workspace_slug: str
    employee_uuid: UUID
    full_name: str
    detached_cv_profile_count: int = 0


class CVEvidenceSourceResult(BaseModel):
    source_uuid: UUID
    source_title: str = ''
    status: str
    evidence_quality: str = ''
    employee_uuid: UUID | None = None
    full_name: str = ''
    current_title: str = ''
    matched_by: str = ''
    match_confidence: float = 0.0
    skill_evidence_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    vector_index_status: str = ''
    reused: bool = False


class CVEvidenceBuildResponse(BaseModel):
    workspace_slug: str
    processed: int
    rebuilt_count: int = 0
    reused_count: int = 0
    status_counts: dict = Field(default_factory=dict)
    results: List[CVEvidenceSourceResult] = Field(default_factory=list)
    employees: List[CVEvidenceSourceResult] = Field(default_factory=list)


class EmployeeCVProfileResponse(BaseModel):
    source_uuid: UUID
    source_title: str = ''
    status: str
    evidence_quality: str = ''
    employee_uuid: UUID | None = None
    full_name: str = ''
    current_title: str = ''
    matched_by: str = ''
    match_confidence: float = 0.0
    headline: str = ''
    profile_current_role: str = ''
    seniority: str = ''
    role_family: str = ''
    warnings: List[str] = Field(default_factory=list)
    candidate_matches: List[dict] = Field(default_factory=list)
    fact_counts: dict = Field(default_factory=dict)
    review_reasons: List[str] = Field(default_factory=list)
    pending_skill_candidates: List[dict] = Field(default_factory=list)
    vector_index_status: str = ''
    created_at: datetime
    updated_at: datetime


class CVEvidenceStatusResponse(BaseModel):
    workspace_slug: str
    total_cv_sources: int = 0
    parsed_cv_sources: int = 0
    pending_source_count: int = 0
    parse_failed_count: int = 0
    processed_profile_count: int = 0
    matched_count: int = 0
    ambiguous_count: int = 0
    unmatched_count: int = 0
    low_confidence_count: int = 0
    extraction_failed_count: int = 0
    strong_profile_count: int = 0
    usable_profile_count: int = 0
    sparse_profile_count: int = 0
    empty_profile_count: int = 0
    employees_with_cv_evidence_count: int = 0
    employees_without_cv_evidence_count: int = 0
    skill_evidence_count: int = 0
    low_confidence_evidence_count: int = 0
    unresolved_source_count: int = 0
    vector_indexed_source_count: int = 0


class UnmatchedCVListResponse(BaseModel):
    workspace_slug: str
    items: List[EmployeeCVProfileResponse] = Field(default_factory=list)


class CVEvidenceReviewListResponse(BaseModel):
    workspace_slug: str
    items: List[EmployeeCVProfileResponse] = Field(default_factory=list)


class EmployeeWithoutCVEvidenceResponse(BaseModel):
    employee_uuid: UUID
    full_name: str
    current_title: str = ''
    review_reason: str = ''
    review_reasons: List[str] = Field(default_factory=list)
    related_source_uuids: List[UUID] = Field(default_factory=list)
    cv_profile_count: int = 0
    cv_evidence_row_count: int = 0
    latest_profile_status: str = ''
    warnings: List[str] = Field(default_factory=list)


class EmployeesWithoutCVEvidenceListResponse(BaseModel):
    workspace_slug: str
    items: List[EmployeeWithoutCVEvidenceResponse] = Field(default_factory=list)


class EmployeeCoverageGapResponse(BaseModel):
    employee_uuid: UUID
    review_reason: str = ''
    review_reasons: List[str] = Field(default_factory=list)
    related_source_uuids: List[UUID] = Field(default_factory=list)
    cv_profile_count: int = 0
    cv_evidence_row_count: int = 0
    latest_profile_status: str = ''
    warnings: List[str] = Field(default_factory=list)


class EmployeeSkillEvidenceResponse(BaseModel):
    skill_uuid: UUID
    skill_key: str
    skill_name_en: str = ''
    skill_name_ru: str = ''
    resolution_status: str = ''
    current_level: float = 0.0
    confidence: float = 0.0
    weight: float = 0.0
    evidence_text: str = ''
    source_uuid: UUID | None = None
    is_operator_confirmed: bool = False
    operator_action: str = ''
    operator_note: str = ''
    metadata: dict = Field(default_factory=dict)


class EmployeeEvidenceDetailResponse(BaseModel):
    workspace_slug: str
    employee_uuid: UUID
    full_name: str
    current_title: str = ''
    external_employee_id: str = ''
    metadata: dict = Field(default_factory=dict)
    cv_availability: EmployeeCvAvailabilityResponse = Field(default_factory=EmployeeCvAvailabilityResponse)
    coverage_gap: EmployeeCoverageGapResponse | None = None
    cv_profiles: List[EmployeeCVProfileResponse] = Field(default_factory=list)
    candidate_cv_profiles: List[EmployeeCVProfileResponse] = Field(default_factory=list)
    evidence_rows: List[EmployeeSkillEvidenceResponse] = Field(default_factory=list)


class EmployeeRoleMatchResponse(BaseModel):
    employee_uuid: UUID
    full_name: str
    matches: list = Field(default_factory=list)


class EmployeeRoleMatchListResponse(BaseModel):
    workspace_slug: str
    employees: List[EmployeeRoleMatchResponse]
