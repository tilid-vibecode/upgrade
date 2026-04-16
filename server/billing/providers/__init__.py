# File location: /server/billing/providers/__init__.py
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass
class CheckoutResult:

    checkout_url: str
    provider_session_id: str


@dataclass
class PortalResult:

    portal_url: str


@dataclass
class CancelResult:

    canceled: bool
    effective_end: Optional[str] = None
    provider_subscription_id: str = ''


class AbstractPaymentProvider(abc.ABC):

    @abc.abstractmethod
    async def create_checkout_session(
        self,
        *,
        user,
        organization,
        plan,
        plan_price,
        paid_by=None,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        ...

    @abc.abstractmethod
    async def create_portal_session(
        self,
        *,
        user,
        return_url: str,
    ) -> PortalResult:
        ...

    @abc.abstractmethod
    async def cancel_subscription(
        self,
        *,
        provider_subscription_id: str,
    ) -> CancelResult:
        ...

    @abc.abstractmethod
    async def handle_webhook(self, *, payload: bytes, signature: str) -> dict:
        ...
