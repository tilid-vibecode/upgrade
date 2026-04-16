# File location: /server/billing/permissions.py
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException

from authentication.models import User
from authentication.permissions import has_org_access, is_authenticated
from organization.models import Organization

from .managers import BillingAllowance, BillingManager

logger = logging.getLogger(__name__)


class _BillingGate:

    async def __call__(
        self,
        org: Organization = Depends(has_org_access),
        current_user: User = Depends(is_authenticated),
    ) -> BillingAllowance:
        allowed, allowance = await BillingManager.can_create_feature_chat(
            current_user, org,
        )
        if not allowed:
            raise HTTPException(status_code=402, detail=allowance.reason)
        return allowance


require_feature_chat_budget = _BillingGate()
