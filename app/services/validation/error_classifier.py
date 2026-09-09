"""Bucket push failures into a stable, small taxonomy for the 14-day report.

The Geonovum brief asks for error-rate breakouts by failure type (timeout, auth
failure, malformed payload, target unavailable, etc.). This module maps raw
requests exceptions and HTTP status codes to a fixed set of buckets that the
recorder and report generator both agree on.

Keep the bucket strings stable — downstream aggregation groups by them.
"""

from __future__ import annotations

import socket

import requests

TIMEOUT = "timeout"
CONNECTION = "connection"
DNS = "dns"
TLS = "tls"
AUTH_401 = "auth_401"
AUTH_403 = "auth_403"
MALFORMED_4XX = "malformed_4xx"
RATE_LIMIT = "rate_limit"
SERVER_5XX = "server_5xx"
SERIALIZATION = "serialization"
CIRCUIT_OPEN = "circuit_open"
UNRESOLVED_DATASTREAM = "unresolved_datastream"
UNKNOWN = "unknown"

ALL_CLASSES = (
    TIMEOUT, CONNECTION, DNS, TLS,
    AUTH_401, AUTH_403, MALFORMED_4XX, RATE_LIMIT, SERVER_5XX,
    SERIALIZATION, CIRCUIT_OPEN, UNRESOLVED_DATASTREAM, UNKNOWN,
)


def classify_error(
    exc: BaseException | None = None,
    status_code: int | None = None,
) -> str:
    """Classify a push failure. Pass an exception or a status code (or both).

    Exception-based signals take precedence over status-code-based signals
    because they carry richer type information (e.g. ``ConnectTimeout`` vs
    ``ReadTimeout`` both classify as ``timeout``; DNS failures inside
    ``ConnectionError`` are recovered from the message).

    Returns ``UNKNOWN`` when neither argument is diagnostic.
    """
    if exc is not None:
        if isinstance(exc, requests.exceptions.SSLError):
            return TLS
        if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
            return TIMEOUT
        if isinstance(exc, requests.exceptions.ConnectionError):
            msg = str(exc).lower()
            if any(t in msg for t in ("nameresolutionerror", "name or service not known", "temporary failure in name resolution", "no address associated")):
                return DNS
            return CONNECTION
        if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
            return CONNECTION
        if isinstance(exc, socket.gaierror):
            return DNS
        etype = type(exc).__name__.lower()
        msg = str(exc).lower()
        if "jsondecode" in etype or "jsonencode" in etype or "serializ" in msg:
            return SERIALIZATION
        if isinstance(exc, ConnectionError):
            return CONNECTION

    if status_code is not None:
        if status_code == 401:
            return AUTH_401
        if status_code == 403:
            return AUTH_403
        if status_code == 429:
            return RATE_LIMIT
        if 400 <= status_code < 500:
            return MALFORMED_4XX
        if 500 <= status_code < 600:
            return SERVER_5XX

    return UNKNOWN


def classify_status_string(text: str) -> str:
    """Fallback classifier when we only have an error string (from the DLQ).

    Best-effort keyword match — used by the report generator to bucket old DLQ
    entries that predate the taxonomy tag.
    """
    if not text:
        return UNKNOWN
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return TIMEOUT
    if "ssl" in low or "certificate" in low:
        return TLS
    if "name resolution" in low or "gaierror" in low or "dns" in low:
        return DNS
    if "connection" in low or "refused" in low or "reset" in low:
        return CONNECTION
    if "unauthor" in low or " 401" in low:
        return AUTH_401
    if "forbidden" in low or " 403" in low:
        return AUTH_403
    if "circuit_open" in low:
        return CIRCUIT_OPEN
    if "serializ" in low or "json" in low:
        return SERIALIZATION
    return UNKNOWN
