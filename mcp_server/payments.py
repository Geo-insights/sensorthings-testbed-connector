"""Supabase user scope management for premium subscriptions.

Uses the Supabase Auth Admin API (service_role key) to set/remove
``mcp_scopes`` in a user's ``app_metadata``. Called by the Stripe webhook
handler when subscriptions change.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_server.auth import SCOPE_FREE, SCOPE_PREMIUM
from mcp_server.config import settings

logger = logging.getLogger(__name__)


async def grant_premium(user_id: str) -> bool:
    """Add mcp:tools:premium to a user's app_metadata.mcp_scopes.

    Returns True on success, False on failure (logged, never raises).
    """
    return await _update_scopes(user_id, add={SCOPE_PREMIUM})


async def revoke_premium(user_id: str) -> bool:
    """Remove mcp:tools:premium from a user's app_metadata.mcp_scopes.

    Returns True on success, False on failure (logged, never raises).
    """
    return await _update_scopes(user_id, remove={SCOPE_PREMIUM})


async def _update_scopes(
    user_id: str,
    *,
    add: set[str] | None = None,
    remove: set[str] | None = None,
) -> bool:
    """Read-modify-write mcp_scopes in the user's app_metadata.

    Uses the Supabase Auth Admin API: ``PUT /auth/v1/admin/users/{id}``.
    Requires the service_role key (not the anon key).
    """
    service_key = settings.supabase_service_role_key
    if not service_key:
        logger.error("Cannot update user scopes: SUPABASE_SERVICE_ROLE_KEY not set")
        return False

    base_url = settings.supabase_project_url.rstrip("/")
    url = f"{base_url}/auth/v1/admin/users/{user_id}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            # Step 1: Get current user to read existing scopes
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            user: dict[str, Any] = resp.json()
            app_metadata: dict[str, Any] = user.get("app_metadata", {})
            current_scopes = set(app_metadata.get("mcp_scopes", [SCOPE_FREE]))

            # Step 2: Modify scopes
            if add:
                current_scopes |= add
            if remove:
                current_scopes -= remove
            # Always keep free scope
            current_scopes.add(SCOPE_FREE)

            # Step 3: Write back
            app_metadata["mcp_scopes"] = sorted(current_scopes)
            resp = await client.put(
                url,
                headers=headers,
                json={"app_metadata": app_metadata},
            )
            resp.raise_for_status()
            logger.info(
                "Updated user %s scopes: %s (add=%s, remove=%s)",
                user_id, sorted(current_scopes), add, remove,
            )
            return True

    except httpx.HTTPStatusError as exc:
        logger.error(
            "Supabase Admin API error updating user %s: %s %s",
            user_id, exc.response.status_code, exc.response.text[:200],
        )
        return False
    except Exception as exc:
        logger.error("Failed to update user %s scopes: %s", user_id, exc)
        return False
