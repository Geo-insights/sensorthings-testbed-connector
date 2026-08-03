"""Best-effort outbound alerting for the connector.

Sends a signed JSON envelope to ``settings.alert_webhook_url`` when operational
events occur (Kafka stall, auth failure, auto-reconnect, freshness breach).

Design mirrors the monitoring module's webhook delivery
(``services/alert_engine._dispatch_webhooks``):

* HMAC-SHA256 signature over the raw body in ``X-Connector-Signature`` when a
  secret is configured.
* Short timeout, failures are swallowed (never block the ingest loop).
* Slack-compatible: the body includes a top-level ``text`` field so a Slack
  Incoming Webhook renders it directly, alongside structured fields.

A per-event-key cooldown (``settings.alert_min_interval_seconds``) prevents a
persistent condition from spamming the webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import requests

from app.config import settings

logger = logging.getLogger("connector.alerting")

_LEVEL_EMOJI = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "critical": "\U0001f6a8"}

# Per-event-key timestamp of the last send, for cooldown/dedup.
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def _should_send(event_key: str, min_interval: float) -> bool:
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(event_key)
        if last is not None and (now - last) < min_interval:
            return False
        _last_sent[event_key] = now
    return True


def send_alert(
    event: str,
    message: str,
    *,
    level: str = "warning",
    context: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    force: bool = False,
) -> bool:
    """POST an alert to the configured webhook (best-effort).

    Args:
        event: Machine-readable event name, e.g. ``"kafka.stall"``.
        message: Human-readable summary.
        level: ``info`` | ``warning`` | ``critical`` (affects the Slack prefix).
        context: Optional structured fields included in the payload.
        dedup_key: Cooldown key; defaults to ``event``. Repeated alerts with the
            same key inside ``alert_min_interval_seconds`` are suppressed.
        force: Bypass the cooldown (use for resolved/recovery notices).

    Returns:
        True if a request was sent (regardless of HTTP status), False if the
        webhook is unconfigured or the alert was suppressed by cooldown.
    """
    url = settings.alert_webhook_url
    if not url:
        return False

    key = dedup_key or event
    if not force and not _should_send(key, max(0, settings.alert_min_interval_seconds)):
        logger.debug("Alert '%s' suppressed by cooldown", key)
        return False

    prefix = _LEVEL_EMOJI.get(level, "")
    text = f"{prefix} [{level.upper()}] {message}".strip()
    envelope: dict[str, Any] = {
        "text": text,
        "event": event,
        "level": level,
        "message": message,
        "source": "sensorthings-testbed-connector",
        "timestamp": datetime.now(UTC).isoformat(),
        "context": context or {},
    }
    body = json.dumps(envelope, default=str).encode()

    headers = {"Content-Type": "application/json"}
    if settings.alert_webhook_secret:
        signature = hmac.new(settings.alert_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Connector-Signature"] = f"sha256={signature}"

    try:
        resp = requests.post(url, data=body, headers=headers, timeout=5.0)
        logger.info("Alert '%s' delivered (status=%s)", event, resp.status_code)
    except requests.RequestException as exc:
        logger.warning("Alert '%s' delivery failed: %s", event, exc)
    return True
