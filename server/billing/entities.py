# File location: /server/billing/entities.py
from typing import Dict, List, Optional
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, field_validator



class PlanPriceResponse(BaseModel):

    uuid: UUID
    currency: str
    price_cents: int
    is_active: bool

    class Config:
        from_attributes = True


class PlanResponse(BaseModel):

    uuid: UUID
    name: str
    slug: str
    monthly_feature_chats: int
    max_free_members_per_discussion: int
    billing_interval: str
    prices: List[PlanPriceResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class PlanListResponse(BaseModel):

    plans: List[PlanResponse]
    total: int



class SubscriptionResponse(BaseModel):

    uuid: UUID
    plan_name: str
    plan_slug: str
    status: str
    currency: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    canceled_at: Optional[datetime] = None
    paid_by_email: Optional[str] = None


class SubscriptionStatusResponse(BaseModel):

    has_subscription: bool
    subscription: Optional[SubscriptionResponse] = None
    usage: Optional['UsageResponse'] = None



class UsageResponse(BaseModel):

    period_start: datetime
    period_end: datetime
    feature_chats_created: int
    feature_chats_limit: int
    remaining: int



class FreeAllowanceInfo(BaseModel):

    allowed: int
    used: int
    remaining: int


class PromoCreditInfo(BaseModel):

    code: str
    granted: int
    used: int
    remaining: int


class SubscriptionCreditInfo(BaseModel):

    plan_name: str
    plan_slug: str
    status: str
    cancel_at_period_end: bool
    current_period_end: str
    chats_limit: int
    chats_used: int
    chats_remaining: int


class CreditsSummaryResponse(BaseModel):

    free_allowance: Optional[FreeAllowanceInfo] = None
    promo_credits: List[PromoCreditInfo] = Field(default_factory=list)
    subscription: Optional[SubscriptionCreditInfo] = None
    total_available: int = 0



class PromoRedeemRequest(BaseModel):

    code: str = Field(..., min_length=1, max_length=50)

    @field_validator('code')
    @classmethod
    def normalize_code(cls, v: str) -> str:
        return v.upper().strip()


class PromoRedeemResponse(BaseModel):

    success: bool
    message: str
    chats_granted: Optional[int] = None



class CheckoutRequest(BaseModel):

    plan_slug: str = Field(..., min_length=1, max_length=100)
    currency: str = Field(default='usd', max_length=8)

    @field_validator('currency')
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.lower().strip()


class CheckoutResponse(BaseModel):

    checkout_url: str


class PortalResponse(BaseModel):

    portal_url: str



class PaymentRecordResponse(BaseModel):

    uuid: UUID
    amount_cents: int
    currency: str
    status: str
    description: str
    created_at: datetime

    class Config:
        from_attributes = True


class PaymentHistoryResponse(BaseModel):

    payments: List[PaymentRecordResponse]
    total: int



class MemberCheckoutRequest(BaseModel):

    plan_slug: str = Field(..., min_length=1, max_length=100)
    currency: str = Field(default='usd', max_length=8)

    @field_validator('currency')
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.lower().strip()
