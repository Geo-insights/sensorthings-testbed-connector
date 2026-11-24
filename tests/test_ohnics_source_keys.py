"""Ohnics registers PM2.5 under the canonical key pm2_5, not the legacy alias pm25.

Until 2026-11-24 OhnicsPollingSource passed ["pm25", "air_temperature"] to build_entity_set, so its
44 PM2.5 Datastreams on FROST carry observed_property/streamKey "pm25" while every reading says
"pm2_5". Routing tolerated it (both resolve to PM2_5); the labels did not match canonical.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.services.ohnics_source import OhnicsPollingSource


def _sensor(name: str) -> dict[str, Any]:
    return {"Name": name, "Lat": 52.01, "Long": 4.36, "P2": 7.5, "T": 11.2, "Time": "2026-11-24T12:00:00Z"}


@pytest.mark.asyncio
async def test_discovered_sensors_register_canonical_keys() -> None:
    source = OhnicsPollingSource()
    name = f"{settings.ohnics_sensor_prefix}4034"
    source._client = AsyncMock()  # type: ignore[assignment]
    source._client.get.return_value = [_sensor(name)]

    await source._do_fetch_readings()
    (entity_set,) = source.entity_sets()

    (sensor,) = entity_set["sensors"]
    assert sensor["observed_properties"] == ["pm2_5", "air_temperature"]
    assert "pm25" not in entity_set["observed_properties"]
    assert set(entity_set["observed_properties"]) == {"pm2_5", "air_temperature"}


def test_sensor_without_cached_properties_defaults_to_canonical_keys() -> None:
    source = OhnicsPollingSource()
    source._discovered_sensors["de-1"] = {"lat": 52.0, "lon": 4.3}
    (entity_set,) = source.entity_sets()
    assert entity_set["sensors"][0]["observed_properties"] == ["pm2_5", "air_temperature"]


def test_datastream_registered_as_pm25_is_reused_by_name_not_duplicated(tmp_path: Any) -> None:
    """The new cache key (ohnics-de-1::pm2_5) is unknown, but the Datastream's name is unchanged:
    get_or_create finds the pm25-era Datastream by name and creates nothing."""
    http = AsyncMock()  # only .endpoint/.post are touched; find_by_name is replaced below
    http.post.side_effect = AssertionError("must not create a second Datastream")
    em = EntityManager(http, EntityCache(tmp_path / "cache.json"))
    names = {"GEO_Ohnics de-1 sensor - PM2.5 concentration": "1"}
    em.find_by_name = lambda path, name: names.get(name)  # type: ignore[method-assign]

    ds_id, status = em.get_or_create(
        "Datastreams", "ohnics-de-1::pm2_5",
        {"name": "GEO_Ohnics de-1 sensor - PM2.5 concentration"}, "datastreams",
    )
    assert (ds_id, status) == ("1", "existing")
