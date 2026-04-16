from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class LLMUsageEntry(BaseModel):
    organization_uuid: str
    user_uuid: Optional[str] = None
    discussion_uuid: Optional[str] = None
    is_org_member: bool = True

    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    is_successful: bool = False
    error_type: str = ''

    call_type: str = 'completion'
    tool_names: List[str] = Field(default_factory=list)
    caller_function: str = ''
    iteration: int = 0
    attempt: int = 0

    estimated_cost_micro: int = 0
    provider_request_id: str = ''

    called_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DashboardFilter(BaseModel):
    organization_uuid: Optional[str] = None
    user_uuid: Optional[str] = None
    discussion_uuid: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    granularity: str = 'daily'
