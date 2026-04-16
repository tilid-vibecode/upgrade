from datetime import datetime
from typing import List
from uuid import UUID

from pydantic import BaseModel, Field


class AssessmentGenerateRequest(BaseModel):
    title: str = 'Initial assessment cycle'
    selected_employee_uuids: List[UUID] = Field(default_factory=list)


class TargetedAssessmentAnswerInput(BaseModel):
    question_id: str
    skill_key: str = ''
    self_rated_level: int = Field(default=0, ge=0, le=5)
    answer_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    example_text: str = ''
    notes: str = ''


class HiddenSkillAnswerInput(BaseModel):
    skill_name_en: str
    skill_name_ru: str = ''
    self_rated_level: int = Field(default=3, ge=0, le=5)
    answer_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    example_text: str = ''


class AspirationAnswerInput(BaseModel):
    target_role_family: str = ''
    notes: str = ''
    interest_signal: str = ''


class AssessmentPackSubmitRequest(BaseModel):
    final_submit: bool = True
    targeted_answers: List[TargetedAssessmentAnswerInput] = Field(default_factory=list)
    hidden_skills: List[HiddenSkillAnswerInput] = Field(default_factory=list)
    aspiration: AspirationAnswerInput = Field(default_factory=AspirationAnswerInput)
    confidence_statement: str = ''


class AssessmentCycleResponse(BaseModel):
    uuid: UUID
    title: str
    status: str
    blueprint_run_uuid: UUID | None = None
    planning_context_uuid: UUID | None = None
    uses_self_report: bool
    uses_performance_reviews: bool
    uses_feedback_360: bool
    uses_skill_tests: bool
    configuration: dict = Field(default_factory=dict)
    result_summary: dict = Field(default_factory=dict)
    pack_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmployeeAssessmentPackResponse(BaseModel):
    uuid: UUID
    cycle_uuid: UUID
    employee_uuid: UUID
    employee_name: str
    status: str
    title: str = ''
    questionnaire_version: str = ''
    questionnaire_payload: dict = Field(default_factory=dict)
    selection_summary: dict = Field(default_factory=dict)
    response_payload: dict = Field(default_factory=dict)
    fused_summary: dict = Field(default_factory=dict)
    opened_at: datetime | None = None
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssessmentPackListResponse(BaseModel):
    workspace_slug: str
    cycle_uuid: UUID
    packs: List[EmployeeAssessmentPackResponse]


class AssessmentStatusResponse(BaseModel):
    workspace_slug: str
    latest_attempt_uuid: UUID | None = None
    latest_attempt_status: str = ''
    current_cycle_uuid: UUID | None = None
    current_cycle_status: str = ''
    blueprint_run_uuid: UUID | None = None
    planning_context_uuid: UUID | None = None
    total_employees: int = 0
    total_packs: int = 0
    generated_packs: int = 0
    opened_packs: int = 0
    submitted_packs: int = 0
    completed_packs: int = 0
    superseded_packs: int = 0
    completion_rate: float = 0.0
    employees_missing_packs: List[dict] = Field(default_factory=list)
    employees_with_submitted_self_assessment: int = 0
    cycle_summary: dict = Field(default_factory=dict)
