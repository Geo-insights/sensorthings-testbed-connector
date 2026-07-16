"""Levellog groundwater level polling source via CARS Online API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient, OAuth2ClientCredentials
from app.services.polling_source import PollingSource
from app.sources.levellog import build_entity_set, parse_logdata_readings

logger = logging.getLogger("connector.levellog")


class LevellogPollingSource(PollingSource):
    """Polls the CARS Online API for groundwater level readings."""

    source_name = "levellog"

    def __init__(self) -> None:
        self._oauth = OAuth2ClientCredentials(
            token_url=settings.levellog_token_url,
            client_id=settings.levellog_client_id,
            client_secret=settings.levellog_client_secret,
            scope="service.external",
        )
        self._base_url = settings.levellog_api_url
        self._installation_ids = _parse_installation_ids(settings.levellog_installation_ids)
        self._last_fetch: dict[str, datetime] = {}

    def is_enabled(self) -> bool:
        return settings.levellog_enabled

    def poll_interval(self) -> int:
        return max(10, settings.levellog_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for configured installations."""
        sets: list[dict[str, Any]] = []
        for inst_id in self._installation_ids:
            name = inst_id[:8]  # short name from UUID
            sets.append(build_entity_set(name, inst_id))
        return sets

    async def fetch_readings(self) -> list[SensorReading]:
        """Fetch latest groundwater readings from CARS API."""
        if not self._installation_ids:
            logger.warning("Levellog: no installation IDs configured")
            return []

        token = await self._oauth.get_token()
        client = AsyncAPIClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

        all_readings: list[SensorReading] = []
        for inst_id in self._installation_ids:
            readings = await self._fetch_installation(client, inst_id)
            all_readings.extend(readings)

        if all_readings:
            logger.info("Levellog: fetched %d readings from %d installation(s)", len(all_readings), len(self._installation_ids))
        return all_readings

    async def _fetch_installation(
        self, client: AsyncAPIClient, installation_id: str
    ) -> list[SensorReading]:
        """Fetch readings for a single installation."""
        # Query data since last fetch (or last 24h on first run)
        since = self._last_fetch.get(installation_id, datetime.now(UTC) - timedelta(hours=24))
        now = datetime.now(UTC)

        try:
            # POST /api/normalizedlogdata/{installationId}
            # Body typically contains date range filters
            body = {
                "from": since.isoformat(),
                "till": now.isoformat(),
            }
            data = await client.post(
                f"/api/normalizedlogdata/{installation_id}",
                json_body=body,
            )
        except Exception:
            logger.exception("Levellog: failed to fetch data for installation %s", installation_id)
            return []

        name = installation_id[:8]
        readings = parse_logdata_readings(data, installation_id, name)

        if readings:
            self._last_fetch[installation_id] = now

        return readings


def _parse_installation_ids(raw: str) -> list[str]:
    """Parse comma-separated installation IDs."""
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]
