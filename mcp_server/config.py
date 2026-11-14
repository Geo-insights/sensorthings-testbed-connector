"""MCP server configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class McpSettings:
    frost_base_url: str = os.getenv(
        "FROST_BASE_URL", "http://localhost:8080/FROST-Server/v1.1"
    )
    frost_auth_username: str = os.getenv("FROST_AUTH_USERNAME", "")
    frost_auth_password: str = os.getenv("FROST_AUTH_PASSWORD", "")
    frost_timeout_seconds: float = float(
        os.getenv("MCP_REQUEST_TIMEOUT_SECONDS", "30")
    )

    rate_limit_rpm: int = int(os.getenv("MCP_RATE_LIMIT_RPM", "100"))
    rate_limit_authenticated_rpm: int = int(
        os.getenv("MCP_RATE_LIMIT_AUTHENTICATED_RPM", "500")
    )
    circuit_breaker_threshold: int = int(
        os.getenv("MCP_CIRCUIT_BREAKER_THRESHOLD", "5")
    )
    circuit_breaker_cooldown_seconds: float = float(
        os.getenv("MCP_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "300")
    )

    # Supabase JWT auth (optional — empty secret disables auth)
    supabase_jwt_secret: str = os.getenv("SUPABASE_JWT_SECRET", "")
    supabase_project_url: str = os.getenv(
        "SUPABASE_URL", "https://upjxsqjnrnamsexrlkqx.supabase.co"
    )

    port: int = int(os.getenv("MCP_PORT", "8030"))


settings = McpSettings()
