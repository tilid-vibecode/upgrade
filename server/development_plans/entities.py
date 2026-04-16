from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DevelopmentPlanGenerateRequest(BaseModel):
    team_title: str = 'Final development plan'


class DevelopmentPlanRunResponse(BaseModel):
    uuid: UUID
    workspace_uuid: UUID
    employee_uuid: Optional[UUID] = None
    blueprint_run_uuid: Optional[UUID] = None
    matrix_run_uuid: Optional[UUID] = None
    planning_context_uuid: Optional[UUID] = None
    generation_batch_uuid: Optional[UUID] = None
    title: str
    scope: str
    status: str
    is_current: bool = False
    plan_version: str = 'stage9-v1'
    input_snapshot: dict = Field(default_factory=dict)
    recommendation_payload: dict = Field(default_factory=dict)
    final_report_key: str = ''
    summary: dict = Field(default_factory=dict)
    plan_payload: dict = Field(default_factory=dict)
    created_at: datetime
    completed_at: Optional[datetime] = None
    updated_at: datetime

    class Config:
        from_attributes = True


class DevelopmentPlanBatchResponse(BaseModel):
    workspace_slug: str
    team_plan: DevelopmentPlanRunResponse
    individual_plans: List[DevelopmentPlanRunResponse]


class DevelopmentPlanSummaryResponse(BaseModel):
    workspace_slug: str
    blueprint_run_uuid: Optional[UUID] = None
    matrix_run_uuid: Optional[UUID] = None
    planning_context_uuid: Optional[UUID] = None
    generation_batch_uuid: Optional[UUID] = None
    team_plan_uuid: Optional[UUID] = None
    team_plan_status: str = ''
    batch_status: str = ''
    is_current: bool = False
    individual_plan_count: int = 0
    employee_count_in_scope: int = 0
    completed_individual_plan_count: int = 0
    failed_individual_plan_count: int = 0
    missing_individual_plan_count: int = 0
    action_counts: dict = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class DevelopmentPlanSliceResponse(BaseModel):
    plan_uuid: UUID
    scope: str
    title: str
    payload: dict = Field(default_factory=dict)
    updated_at: datetime


class DevelopmentPlanArtifactResponse(BaseModel):
    uuid: UUID
    workspace_uuid: UUID
    plan_run_uuid: UUID
    employee_uuid: Optional[UUID] = None
    blueprint_run_uuid: Optional[UUID] = None
    matrix_run_uuid: Optional[UUID] = None
    planning_context_uuid: Optional[UUID] = None
    generation_batch_uuid: Optional[UUID] = None
    artifact_scope: str
    artifact_format: str
    artifact_version: str = 'stage10-v1'
    is_current: bool = False
    title: str
    metadata: dict = Field(default_factory=dict)
    file_uuid: UUID
    original_filename: str
    content_type: str
    file_size: int
    signed_url: Optional[str] = None
    expires_in_seconds: Optional[int] = None
    source_run_completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class DevelopmentPlanArtifactBundleResponse(BaseModel):
    workspace_slug: str
    plan_uuid: UUID
    employee_uuid: Optional[UUID] = None
    generation_batch_uuid: Optional[UUID] = None
    scope: str
    title: str
    status: str
    is_current: bool = False
    selected_as_current: bool = False
    artifacts: List[DevelopmentPlanArtifactResponse] = Field(default_factory=list)
    updated_at: datetime


class DevelopmentPlanArtifactListResponse(BaseModel):
    workspace_slug: str
    artifacts: List[DevelopmentPlanArtifactResponse] = Field(default_factory=list)
    total: int = 0
