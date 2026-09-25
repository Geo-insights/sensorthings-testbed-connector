"""YAML-based entity configuration loader and reconciler.

Loads entity definitions from YAML files in ``entity_configs/`` and converts
them into the legacy entity-set dict format consumed by
:meth:`SensorThingsClient.register_entity_set`.  The YAML files are the
source of truth for *what should exist*; the JSON entity cache tracks
*what IDs were assigned* at runtime.

Usage::

    from app.sta.entity_config import load_all_configs, configs_to_entity_sets

    configs = load_all_configs(Path("entity_configs"))
    entity_sets = configs_to_entity_sets(configs)
    for es in entity_sets:
        client.register_entity_set(es)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic validation schemas
# ---------------------------------------------------------------------------


class SensorEntry(BaseModel):
    """A sensor definition inside an entity config."""

    sensor_id: str
    name: str
    description: str = ""
    encodingType: str = "application/json"
    metadata: str = ""
    observed_properties: list[str]
    properties: dict[str, Any] = Field(default_factory=dict)


class EntityConfig(BaseModel):
    """Validated representation of a single YAML entity config file.

    Mirrors the structure of the legacy ``CLIMATE_ADAPTATION_ENTITY_SETS``
    dicts so that conversion is a straightforward field rename.
    """

    version: str = "1.1"

    # Top-level entity definitions
    thing: dict[str, Any]
    location: dict[str, Any]
    sensors: list[SensorEntry]
    observed_properties: dict[str, str]

    # Site metadata
    site_key: str
    site_name: str

    @model_validator(mode="after")
    def _validate_sensor_observed_properties(self) -> EntityConfig:
        """Every observed_property referenced by a sensor must be declared."""
        declared = set(self.observed_properties.keys())
        for sensor in self.sensors:
            for op in sensor.observed_properties:
                if op not in declared:
                    raise ValueError(
                        f"Sensor {sensor.name!r} references observed property "
                        f"{op!r} which is not declared in observed_properties "
                        f"(available: {sorted(declared)})"
                    )
        return self


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_entity_config(path: Path) -> EntityConfig:
    """Load and validate a single YAML entity config file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}, got {type(data).__name__}")
    return EntityConfig(**data)


def load_all_configs(config_dir: Path) -> list[EntityConfig]:
    """Load all non-template YAML configs from a directory.

    Files whose name starts with ``_`` (e.g. ``_template_ohnics.yaml``) are
    skipped -- they are documentation templates, not active configs.
    """
    configs: list[EntityConfig] = []
    if not config_dir.is_dir():
        logger.warning("Entity config directory %s does not exist", config_dir)
        return configs
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        try:
            configs.append(load_entity_config(yaml_file))
        except Exception:
            logger.exception("Failed to load entity config %s", yaml_file)
            raise
    return configs


# ---------------------------------------------------------------------------
# Reconciliation: EntityConfig -> legacy entity-set dict
# ---------------------------------------------------------------------------


def reconcile_config_to_entity_set(config: EntityConfig) -> dict[str, Any]:
    """Convert an :class:`EntityConfig` to the legacy entity-set dict format.

    The returned dict has the same shape as entries in the old
    ``CLIMATE_ADAPTATION_ENTITY_SETS`` list, so the existing
    ``register_entity_set()`` function can consume it without changes.
    """
    # Build sensor dicts in the legacy format
    sensors: list[dict[str, Any]] = []
    for sensor in config.sensors:
        sensor_dict: dict[str, Any] = {
            "sensor_id": sensor.sensor_id,
            "name": sensor.name,
            "description": sensor.description,
            "encodingType": sensor.encodingType,
            "metadata": sensor.metadata,
            "observed_properties": list(sensor.observed_properties),
            "properties": dict(sensor.properties),
        }
        sensors.append(sensor_dict)

    return {
        "site_key": config.site_key,
        "site_name": config.site_name,
        "thing": dict(config.thing),
        "location": dict(config.location),
        "sensors": sensors,
        "observed_properties": dict(config.observed_properties),
    }


def configs_to_entity_sets(configs: list[EntityConfig]) -> list[dict[str, Any]]:
    """Convert a list of :class:`EntityConfig` objects to legacy entity-set dicts."""
    return [reconcile_config_to_entity_set(c) for c in configs]


def load_entity_sets(config_dir: Path) -> list[dict[str, Any]]:
    """One-shot: load YAML configs and return legacy entity-set dicts.

    Convenience wrapper combining :func:`load_all_configs` and
    :func:`configs_to_entity_sets`.
    """
    return configs_to_entity_sets(load_all_configs(config_dir))
