"""Stripe webhook handler and checkout session creator.

Two HTTP endpoints mounted alongside the MCP server:

- ``POST /webhook/stripe`` — receives Stripe events (checkout completed,
  subscription deleted) and updates Supabase user scopes accordingly.
- ``POST /checkout`` — creates a Stripe Checkout session and returns the URL.
  Expects JSON body ``{"user_id": "...", "email": "..."}``.

Both endpoints are only mounted when ``STRIPE_SECRET_KEY`` is set.
"""

from __future__ import annotations

import logging

import stripe
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_server.config import settings
from mcp_server.payments import grant_premium, revoke_premium

logger = logging.getLogger(__name__)


def stripe_enabled() -> bool:
    """True when Stripe keys are configured."""
    return bool(settings.stripe_secret_key)


def _init_stripe() -> None:
    """Set the Stripe API key from config."""
    stripe.api_key = settings.stripe_secret_key


# -- Webhook handler -------------------------------------------------------


async def stripe_webhook_endpoint(request: Request) -> Response:
    """Handle incoming Stripe webhook events.

    Verifies the webhook signature, then dispatches by event type:
    - ``checkout.session.completed`` → grant premium scope
    - ``customer.subscription.deleted`` → revoke premium scope
    """
    _init_stripe()
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not settings.stripe_webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET not configured, rejecting webhook")
        return Response(status_code=500, content="Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            body, sig, settings.stripe_webhook_secret
        )
    except stripe.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        return Response(status_code=400, content="Invalid signature")
    except Exception as exc:
        logger.error("Stripe webhook parse error: %s", exc)
        return Response(status_code=400, content="Invalid payload")

    event_type = event["type"]
    logger.info("Stripe webhook received: %s (id=%s)", event_type, event["id"])

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(event["data"]["object"])
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(event["data"]["object"])
    else:
        logger.debug("Ignoring Stripe event type: %s", event_type)

    return Response(status_code=200, content="ok")


async def _handle_checkout_completed(session: dict) -> None:  # type: ignore[type-arg]
    """Grant premium scope when a checkout session completes.

    The ``client_reference_id`` field contains the Supabase user_id
    (set when creating the checkout session).
    """
    user_id = session.get("client_reference_id")
    if not user_id:
        logger.warning(
            "checkout.session.completed without client_reference_id, "
            "cannot grant premium (session=%s)", session.get("id"),
        )
        return

    customer_email = session.get("customer_details", {}).get("email", "unknown")
    success = await grant_premium(user_id)
    if success:
        logger.info(
            "Premium granted for user %s (%s) via checkout %s",
            user_id, customer_email, session.get("id"),
        )
    else:
        logger.error(
            "Failed to grant premium for user %s after checkout %s",
            user_id, session.get("id"),
        )


async def _handle_subscription_deleted(subscription: dict) -> None:  # type: ignore[type-arg]
    """Revoke premium scope when a subscription is cancelled.

    The ``metadata.supabase_user_id`` field contains the Supabase user_id
    (set on the subscription or customer object).
    """
    metadata = subscription.get("metadata", {})
    user_id = metadata.get("supabase_user_id")
    if not user_id:
        logger.warning(
            "customer.subscription.deleted without metadata.supabase_user_id, "
            "cannot revoke premium (sub=%s)", subscription.get("id"),
        )
        return

    success = await revoke_premium(user_id)
    if success:
        logger.info("Premium revoked for user %s (sub=%s)", user_id, subscription.get("id"))
    else:
        logger.error("Failed to revoke premium for user %s (sub=%s)", user_id, subscription.get("id"))


# -- Checkout session creator ----------------------------------------------


async def create_checkout_endpoint(request: Request) -> JSONResponse:
    """Create a Stripe Checkout session and return the URL.

    Expects JSON body::

        {"user_id": "supabase-uuid", "email": "user@example.com"}

    Returns::

        {"checkout_url": "https://checkout.stripe.com/..."}
    """
    _init_stripe()

    if not settings.stripe_price_id:
        return JSONResponse(
            {"error": "not_configured", "message": "Stripe price not configured yet"},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_body", "message": "Expected JSON with user_id and email"},
            status_code=400,
        )

    user_id = body.get("user_id")
    email = body.get("email")
    if not user_id:
        return JSONResponse(
            {"error": "missing_user_id", "message": "user_id is required"},
            status_code=400,
        )

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
            client_reference_id=user_id,
            customer_email=email,
            success_url=settings.stripe_success_url,
            cancel_url=settings.stripe_cancel_url,
            subscription_data={
                "metadata": {"supabase_user_id": user_id},
            },
        )
    except stripe.StripeError as exc:
        logger.error("Stripe checkout creation failed: %s", exc)
        return JSONResponse(
            {"error": "stripe_error", "message": "Failed to create checkout session"},
            status_code=502,
        )

    logger.info("Checkout session created for user %s: %s", user_id, session.id)
    return JSONResponse({"checkout_url": session.url})
