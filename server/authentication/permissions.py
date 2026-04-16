"""Temporary permission shims for the prototype phase.

The current prototype is intentionally unprotected. These helpers keep legacy
routers importable without enforcing auth/org checks. Once the protected flows
return, this module should be replaced with the real permission logic.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


async def is_authenticated() -> Any:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail='Authentication is disabled in the prototype flow.',
    )


async def has_org_access(*args, **kwargs) -> Any:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail='Organization-gated routes are disabled in the prototype flow.',
    )


async def require_member(*args, **kwargs) -> Any:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail='Member-only routes are disabled in the prototype flow.',
    )
