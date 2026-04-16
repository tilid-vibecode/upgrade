from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class EvidenceMatrixBuildRequest(BaseModel):
    title: str = 'Second-layer evidence matrix'
    assessment_cycle_uuid: UUID | None = None


class EvidenceMatrixRunResponse(BaseModel):
    uuid: UUID
    title: str
    status: str
    source_type: str
    blueprint_run_uuid: UUID | None = None
    planning_context_uuid: UUID | None = None
    connection_label: str = ''
    snapshot_key: str = ''
    matrix_version: str = 'stage8-v1'
    input_snapshot: dict = Field(default_factory=dict)
    summary_payload: dict = Field(default_factory=dict)
    heatmap_payload: dict = Field(default_factory=dict)
    risk_payload: dict = Field(default_factory=dict)
    incompleteness_payload: dict = Field(default_factory=dict)
    matrix_payload: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EvidenceMatrixSliceResponse(BaseModel):
    run_uuid: UUID
    title: str
    status: str
    matrix_version: str = 'stage8-v1'
    payload: dict = Field(default_factory=dict)
    updated_at: datetime
