"""Tests for Stripe payment integration and Supabase scope management.

protects: webhook signature verification, checkout session creation,
          scope grant/revoke, error handling
fails_when: invalid signatures accepted, scopes not updated, checkout fails
no_claim: actual Stripe API calls, real Supabase writes
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# -- payments.py (Supabase scope management) --------------------------------


@pytest.mark.asyncio
async def test_grant_premium_updates_scopes():
    """grant_premium adds mcp:tools:premium to user app_metadata."""
    from mcp_server.payments import grant_premium

    mock_client = AsyncMock()

    # Mock GET (current user) → user has only free scope
    get_response = MagicMock()
    get_response.json.return_value = {
        "id": "user-123",
        "app_metadata": {"mcp_scopes": ["mcp:tools:free"]},
    }
    get_response.raise_for_status = MagicMock()

    # Mock PUT (update user) → success
    put_response = MagicMock()
    put_response.raise_for_status = MagicMock()

    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=get_response)
    mock_client.put = AsyncMock(return_value=put_response)

    with (
        patch("mcp_server.payments.settings") as mock_settings,
        patch("mcp_server.payments.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.supabase_jwt_secret = "test-service-role-key"
        mock_settings.supabase_project_url = "https://test.supabase.co"

        result = await grant_premium("user-123")

    assert result is True
    # Verify PUT was called with premium scope added
    put_call = mock_client.put.call_args
    body = put_call.kwargs["json"]
    assert "mcp:tools:premium" in body["app_metadata"]["mcp_scopes"]
    assert "mcp:tools:free" in body["app_metadata"]["mcp_scopes"]


@pytest.mark.asyncio
async def test_revoke_premium_removes_scope():
    """revoke_premium removes mcp:tools:premium but keeps free."""
    from mcp_server.payments import revoke_premium

    mock_client = AsyncMock()

    get_response = MagicMock()
    get_response.json.return_value = {
        "id": "user-123",
        "app_metadata": {"mcp_scopes": ["mcp:tools:free", "mcp:tools:premium"]},
    }
    get_response.raise_for_status = MagicMock()

    put_response = MagicMock()
    put_response.raise_for_status = MagicMock()

    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=get_response)
    mock_client.put = AsyncMock(return_value=put_response)

    with (
        patch("mcp_server.payments.settings") as mock_settings,
        patch("mcp_server.payments.httpx.AsyncClient", return_value=mock_client),
    ):
        mock_settings.supabase_jwt_secret = "test-service-role-key"
        mock_settings.supabase_project_url = "https://test.supabase.co"

        result = await revoke_premium("user-123")

    assert result is True
    put_call = mock_client.put.call_args
    body = put_call.kwargs["json"]
    assert "mcp:tools:premium" not in body["app_metadata"]["mcp_scopes"]
    assert "mcp:tools:free" in body["app_metadata"]["mcp_scopes"]


@pytest.mark.asyncio
async def test_grant_premium_fails_without_secret():
    """grant_premium returns False when JWT secret is not configured."""
    from mcp_server.payments import grant_premium

    with patch("mcp_server.payments.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = ""
        result = await grant_premium("user-123")
    assert result is False


# -- stripe_webhook.py (webhook handler) -----------------------------------


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature():
    """Webhook returns 400 for invalid Stripe signature."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_server.stripe_webhook import stripe_webhook_endpoint

    test_app = Starlette(routes=[
        Route("/webhook/stripe", stripe_webhook_endpoint, methods=["POST"]),
    ])
    client = TestClient(test_app)

    with patch("mcp_server.stripe_webhook.settings") as mock_settings:
        mock_settings.stripe_secret_key = "sk_test_xxx"
        mock_settings.stripe_webhook_secret = "whsec_test_xxx"
        resp = client.post(
            "/webhook/stripe",
            content=b'{"type": "test"}',
            headers={"stripe-signature": "bad-sig"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_checkout_completed_grants_premium():
    """checkout.session.completed event triggers grant_premium."""
    from mcp_server.stripe_webhook import _handle_checkout_completed

    with patch("mcp_server.stripe_webhook.grant_premium", new_callable=AsyncMock) as mock_grant:
        mock_grant.return_value = True
        await _handle_checkout_completed({
            "id": "cs_test_123",
            "client_reference_id": "user-abc",
            "customer_details": {"email": "test@example.com"},
        })
        mock_grant.assert_awaited_once_with("user-abc")


@pytest.mark.asyncio
async def test_webhook_checkout_without_user_id():
    """checkout.session.completed without client_reference_id is skipped."""
    from mcp_server.stripe_webhook import _handle_checkout_completed

    with patch("mcp_server.stripe_webhook.grant_premium", new_callable=AsyncMock) as mock_grant:
        await _handle_checkout_completed({
            "id": "cs_test_123",
            "customer_details": {"email": "test@example.com"},
        })
        mock_grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_subscription_deleted_revokes_premium():
    """customer.subscription.deleted event triggers revoke_premium."""
    from mcp_server.stripe_webhook import _handle_subscription_deleted

    with patch("mcp_server.stripe_webhook.revoke_premium", new_callable=AsyncMock) as mock_revoke:
        mock_revoke.return_value = True
        await _handle_subscription_deleted({
            "id": "sub_test_123",
            "metadata": {"supabase_user_id": "user-abc"},
        })
        mock_revoke.assert_awaited_once_with("user-abc")


@pytest.mark.asyncio
async def test_webhook_subscription_deleted_without_user_id():
    """subscription.deleted without metadata.supabase_user_id is skipped."""
    from mcp_server.stripe_webhook import _handle_subscription_deleted

    with patch("mcp_server.stripe_webhook.revoke_premium", new_callable=AsyncMock) as mock_revoke:
        await _handle_subscription_deleted({
            "id": "sub_test_123",
            "metadata": {},
        })
        mock_revoke.assert_not_awaited()


# -- stripe_webhook.py (checkout session creation) -------------------------


@pytest.mark.asyncio
async def test_checkout_returns_503_without_price():
    """Checkout endpoint returns 503 when STRIPE_PRICE_ID is not set."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_server.stripe_webhook import create_checkout_endpoint

    test_app = Starlette(routes=[
        Route("/checkout", create_checkout_endpoint, methods=["POST"]),
    ])
    client = TestClient(test_app)

    with patch("mcp_server.stripe_webhook.settings") as mock_settings:
        mock_settings.stripe_secret_key = "sk_test_xxx"
        mock_settings.stripe_price_id = ""
        resp = client.post("/checkout", json={"user_id": "u1", "email": "a@b.com"})

    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_checkout_requires_user_id():
    """Checkout endpoint returns 400 without user_id."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_server.stripe_webhook import create_checkout_endpoint

    test_app = Starlette(routes=[
        Route("/checkout", create_checkout_endpoint, methods=["POST"]),
    ])
    client = TestClient(test_app)

    with patch("mcp_server.stripe_webhook.settings") as mock_settings:
        mock_settings.stripe_secret_key = "sk_test_xxx"
        mock_settings.stripe_price_id = "price_test_123"
        resp = client.post("/checkout", json={"email": "a@b.com"})

    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_user_id"


# -- stripe_enabled --------------------------------------------------------


def test_stripe_enabled_with_key():
    from mcp_server.stripe_webhook import stripe_enabled

    with patch("mcp_server.stripe_webhook.settings") as mock_settings:
        mock_settings.stripe_secret_key = "sk_test_xxx"
        assert stripe_enabled() is True


def test_stripe_disabled_without_key():
    from mcp_server.stripe_webhook import stripe_enabled

    with patch("mcp_server.stripe_webhook.settings") as mock_settings:
        mock_settings.stripe_secret_key = ""
        assert stripe_enabled() is False
