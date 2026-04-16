from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class RoleLibrarySyncRequest(BaseModel):
    base_urls: List[str] = Field(default_factory=list)
    max_pages: int = Field(40, ge=1, le=200)


class RoleLibrarySnapshotResponse(BaseModel):
    uuid: UUID
    provider: str
    status: str
    base_urls: List[str] = Field(default_factory=list)
    discovery_payload: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)
    canonical_family_counts: dict = Field(default_factory=dict)
    normalized_skill_count: int = 0
    alias_count: int = 0
    seed_urls_used: List[str] = Field(default_factory=list)
    quality_flags: List[str] = Field(default_factory=list)
    missing_role_families: List[str] = Field(default_factory=list)
    error_message: str = ''
    entry_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BlueprintGenerateRequest(BaseModel):
    role_library_snapshot_uuid: Optional[UUID] = None


class BlueprintClarificationUpdateRequest(BaseModel):
    clarification_id: str
    answer: str = ''
    status: str = 'accepted'
    note: str = ''


class BlueprintPatchRequest(BaseModel):
    title: Optional[str] = None
    company_context: Optional[dict] = None
    roadmap_context: Optional[list] = None
    role_candidates: Optional[list] = None
    clarification_questions: Optional[list] = None
    automation_candidates: Optional[list] = None
    occupation_map: Optional[list] = None
    assessment_plan: Optional[dict] = None
    patch_reason: str = ''
    operator_name: str = ''
    review_notes: str = ''
    skip_employee_matching: bool = False


class BlueprintReviewRequest(BaseModel):
    reviewer_name: str = ''
    review_notes: str = ''
    clarification_updates: List[BlueprintClarificationUpdateRequest] = Field(default_factory=list)


class BlueprintApproveRequest(BaseModel):
    approver_name: str = ''
    approval_notes: str = ''
    clarification_updates: List[BlueprintClarificationUpdateRequest] = Field(default_factory=list)


class ClarificationAnswerItemRequest(BaseModel):
    question_uuid: Optional[UUID] = None
    clarification_id: str = ''
    answer_text: str = ''
    status: str = ''
    status_note: str = ''
    changed_target_model: bool = False


class ClarificationAnswerRequest(BaseModel):
    operator_name: str = ''
    items: List[ClarificationAnswerItemRequest] = Field(default_factory=list)


class BlueprintRefreshRequest(BaseModel):
    operator_name: str = ''
    refresh_note: str = ''
    skip_employee_matching: bool = False


class BlueprintRevisionRequest(BaseModel):
    operator_name: str = ''
    revision_reason: str = ''
    skip_employee_matching: bool = True


class BlueprintPublishRequest(BaseModel):
    publisher_name: str = ''
    publish_notes: str = ''


class SkillBlueprintRunResponse(BaseModel):
    uuid: UUID
    title: str
    status: str
    role_library_snapshot_uuid: Optional[UUID] = None
    derived_from_run_uuid: Optional[UUID] = None
    roadmap_analysis_uuid: Optional[UUID] = None
    planning_context_uuid: Optional[UUID] = None
    generation_mode: str = 'generation'
    source_summary: dict = Field(default_factory=dict)
    input_snapshot: dict = Field(default_factory=dict)
    company_context: dict = Field(default_factory=dict)
    roadmap_context: list = Field(default_factory=list)
    role_candidates: list = Field(default_factory=list)
    clarification_questions: list = Field(default_factory=list)
    employee_role_matches: list = Field(default_factory=list)
    required_skill_set: list = Field(default_factory=list)
    automation_candidates: list = Field(default_factory=list)
    occupation_map: list = Field(default_factory=list)
    gap_summary: dict = Field(default_factory=dict)
    redundancy_summary: dict = Field(default_factory=dict)
    assessment_plan: dict = Field(default_factory=dict)
    review_summary: dict = Field(default_factory=dict)
    change_log: list = Field(default_factory=list)
    reviewed_by: str = ''
    review_notes: str = ''
    reviewed_at: Optional[datetime] = None
    approved_by: str = ''
    approval_notes: str = ''
    approved_at: Optional[datetime] = None
    is_published: bool = False
    published_by: str = ''
    published_notes: str = ''
    published_at: Optional[datetime] = None
    clarification_cycle_uuid: Optional[UUID] = None
    clarification_cycle_status: str = ''
    clarification_cycle_summary: dict = Field(default_factory=dict)
    approval_blocked: bool = False
    latest_for_workspace: bool = False
    latest_review_ready_for_workspace: bool = False
    latest_approved_for_workspace: bool = False
    latest_published_for_workspace: bool = False
    default_for_workspace: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SkillBlueprintRunListResponse(BaseModel):
    workspace_slug: str
    runs: List[SkillBlueprintRunResponse] = Field(default_factory=list)


class BlueprintRoadmapResponse(BaseModel):
    workspace_slug: str
    blueprint_uuid: UUID
    roadmap_context: list = Field(default_factory=list)


class BlueprintRoleDetailResponse(BaseModel):
    workspace_slug: str
    blueprint_uuid: UUID
    role_key: str
    role_candidate: dict = Field(default_factory=dict)


class ClarificationQuestionResponse(BaseModel):
    uuid: UUID
    cycle_uuid: UUID
    blueprint_uuid: UUID
    question_key: str
    question_text: str
    scope: str
    priority: str
    intended_respondent_type: str = ''
    rationale: str = ''
    evidence_refs: list = Field(default_factory=list)
    impacted_roles: list = Field(default_factory=list)
    impacted_initiatives: list = Field(default_factory=list)
    status: str
    answer_text: str = ''
    answered_by: str = ''
    answered_at: Optional[datetime] = None
    status_note: str = ''
    changed_target_model: bool = False
    effect_metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClarificationCycleResponse(BaseModel):
    uuid: UUID
    blueprint_uuid: UUID
    title: str
    status: str
    summary: dict = Field(default_factory=dict)
    questions: List[ClarificationQuestionResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClarificationQuestionListResponse(BaseModel):
    workspace_slug: str
    blueprint_uuid: Optional[UUID] = None
    questions: List[ClarificationQuestionResponse] = Field(default_factory=list)
