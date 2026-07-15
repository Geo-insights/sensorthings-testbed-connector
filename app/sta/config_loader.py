"""YAML-driven sensor configuration loader with validation.

Loads site configuration from YAML files and validates them against
the STA domain models and canonical datastream names.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = {"site_key", "thing", "location", "sensors", "observed_properties"}
_REQUIRED_THING_KEYS = {"name", "description"}
_REQUIRED_LOCATION_KEYS = {"name", "description", "location"}
_REQUIRED_SENSOR_KEYS = {"sensor_id", "name", "description", "observed_properties"}
_REQUIRED_OP_KEYS = {"name", "definition", "description", "unit"}


def _validate_entity_set(entity_set: dict[str, Any], index: int) -> list[str]:
    """Validate a single entity set dict, returning a list of error messages."""
    errors: list[str] = []
    prefix = f"entity_set[{index}]"

    # Top-level keys
    missing = _REQUIRED_KEYS - set(entity_set.keys())
    if missing:
        errors.append(f"{prefix}: missing required keys: {missing}")
        return errors

    # Thing
    thing = entity_set["thing"]
    if not isinstance(thing, dict):
        errors.append(f"{prefix}.thing: must be a dict")
    else:
        missing_thing = _REQUIRED_THING_KEYS - set(thing.keys())
        if missing_thing:
            errors.append(f"{prefix}.thing: missing keys: {missing_thing}")

    # Location
    location = entity_set["location"]
    if not isinstance(location, dict):
        errors.append(f"{prefix}.location: must be a dict")
    else:
        missing_loc = _REQUIRED_LOCATION_KEYS - set(location.keys())
        if missing_loc:
            errors.append(f"{prefix}.location: missing keys: {missing_loc}")

    # Sensors
    sensors = entity_set["sensors"]
    if not isinstance(sensors, list):
        errors.append(f"{prefix}.sensors: must be a list")
    else:
        for i, sensor in enumerate(sensors):
            if not isinstance(sensor, dict):
                errors.append(f"{prefix}.sensors[{i}]: must be a dict")
                continue
            missing_sensor = _REQUIRED_SENSOR_KEYS - set(sensor.keys())
            if missing_sensor:
                errors.append(f"{prefix}.sensors[{i}]: missing keys: {missing_sensor}")

            # IoT link integrity: each observed_property reference must exist
            op_refs = sensor.get("observed_properties", [])
            if isinstance(op_refs, list):
                obs_props = entity_set.get("observed_properties", {})
                for ref in op_refs:
                    if ref not in obs_props:
                        errors.append(
                            f"{prefix}.sensors[{i}]: observed_property '{ref}' "
                            f"not found in observed_properties dict"
                        )

    # ObservedProperties
    obs_props = entity_set["observed_properties"]
    if not isinstance(obs_props, dict):
        errors.append(f"{prefix}.observed_properties: must be a dict")
    else:
        for key, op in obs_props.items():
            if not isinstance(op, dict):
                errors.append(f"{prefix}.observed_properties.{key}: must be a dict")
                continue
            missing_op = _REQUIRED_OP_KEYS - set(op.keys())
            if missing_op:
                errors.append(f"{prefix}.observed_properties.{key}: missing keys: {missing_op}")

    return errors


def load_site_config(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse a YAML site config file and validate.

    Returns (entity_sets, errors).  If errors is non-empty, entity_sets
    may be partial or empty.
    """
    try:
        import yaml
    except ImportError:
        return [], ["PyYAML is not installed. Run: pip install pyyaml"]

    if not path.exists():
        return [], [f"Config file not found: {path}"]

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [f"Failed to parse {path}: {exc}"]

    if not isinstance(raw, list):
        return [], [f"{path}: expected a YAML list of entity sets, got {type(raw).__name__}"]

    errors: list[str] = []
    entity_sets: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"{path}[{i}]: expected dict, got {type(item).__name__}")
            continue
        item_errors = _validate_entity_set(item, i)
        if item_errors:
            errors.extend(item_errors)
        entity_sets.append(item)

    return entity_sets, errors


def load_all_site_configs(config_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load all YAML files from a directory.

    Returns (all_entity_sets, all_errors).
    """
    if not config_dir.is_dir():
        return [], [f"Config directory not found: {config_dir}"]

    all_sets: list[dict[str, Any]] = []
    all_errors: list[str] = []

    for yaml_path in sorted(config_dir.glob("*.yaml")) + sorted(config_dir.glob("*.yml")):
        sets, errors = load_site_config(yaml_path)
        all_sets.extend(sets)
        all_errors.extend(errors)

    if not all_sets and not all_errors:
        logger.info("No YAML config files found in %s", config_dir)

    return all_sets, all_errors
