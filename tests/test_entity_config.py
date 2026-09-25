"""Tests for YAML entity config loading and reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.sta.entity_config import (
    EntityConfig,
    load_all_configs,
    load_entity_config,
    load_entity_sets,
    reconcile_config_to_entity_set,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_CONFIG = {
    "version": "1.1",
    "site_key": "tgv",
    "site_name": "The Green Village",
    "thing": {
        "name": "Test Thing",
        "description": "A test thing.",
        "properties": {"site": "tgv"},
    },
    "location": {
        "name": "Test location",
        "description": "A test location.",
        "encodingType": "application/geo+json",
        "location": {"type": "Point", "coordinates": [4.377, 51.996]},
        "properties": {},
    },
    "sensors": [
        {
            "sensor_id": "test-sensor-1",
            "name": "Test sensor",
            "description": "A test sensor.",
            "encodingType": "application/json",
            "metadata": "https://example.com",
            "observed_properties": ["temperature", "humidity"],
            "properties": {},
        }
    ],
    "observed_properties": {
        "temperature": "Indoor air temperature.",
        "humidity": "Indoor relative humidity.",
    },
}


@pytest.fixture()
def valid_yaml_file(tmp_path: Path) -> Path:
    path = tmp_path / "test_thing.yaml"
    path.write_text(yaml.dump(_VALID_CONFIG), encoding="utf-8")
    return path


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a directory with two valid configs and one template."""
    d = tmp_path / "entity_configs"
    d.mkdir()

    config_a = dict(_VALID_CONFIG)
    config_a["thing"] = {**config_a["thing"], "name": "Thing A"}
    (d / "a_thing.yaml").write_text(yaml.dump(config_a), encoding="utf-8")

    config_b = dict(_VALID_CONFIG)
    config_b["thing"] = {**config_b["thing"], "name": "Thing B"}
    (d / "b_thing.yaml").write_text(yaml.dump(config_b), encoding="utf-8")

    # Template files (underscore-prefixed) should be skipped.
    config_t = dict(_VALID_CONFIG)
    config_t["thing"] = {**config_t["thing"], "name": "Template Thing"}
    (d / "_template.yaml").write_text(yaml.dump(config_t), encoding="utf-8")

    return d


# ---------------------------------------------------------------------------
# Test: loading a valid config
# ---------------------------------------------------------------------------


def test_load_valid_config(valid_yaml_file: Path):
    config = load_entity_config(valid_yaml_file)
    assert config.version == "1.1"
    assert config.site_key == "tgv"
    assert config.thing["name"] == "Test Thing"
    assert len(config.sensors) == 1
    assert config.sensors[0].name == "Test sensor"
    assert config.sensors[0].observed_properties == ["temperature", "humidity"]
    assert "temperature" in config.observed_properties


# ---------------------------------------------------------------------------
# Test: validation rejects missing required fields
# ---------------------------------------------------------------------------


def test_missing_thing_raises():
    data = dict(_VALID_CONFIG)
    del data["thing"]
    with pytest.raises(Exception):  # noqa: B017
        EntityConfig(**data)


def test_missing_sensors_raises():
    data = dict(_VALID_CONFIG)
    del data["sensors"]
    with pytest.raises(Exception):  # noqa: B017
        EntityConfig(**data)


def test_missing_site_key_raises():
    data = dict(_VALID_CONFIG)
    del data["site_key"]
    with pytest.raises(Exception):  # noqa: B017
        EntityConfig(**data)


# ---------------------------------------------------------------------------
# Test: validation rejects invalid iot_links / observed_property references
# ---------------------------------------------------------------------------


def test_sensor_referencing_undeclared_observed_property_raises():
    data = dict(_VALID_CONFIG)
    data["sensors"] = [
        {
            "sensor_id": "bad-sensor",
            "name": "Bad sensor",
            "observed_properties": ["nonexistent_property"],
            "properties": {},
        }
    ]
    with pytest.raises(ValueError, match="nonexistent_property"):
        EntityConfig(**data)


# ---------------------------------------------------------------------------
# Test: reconcile_config_to_entity_set produces correct dict format
# ---------------------------------------------------------------------------


def test_reconcile_produces_legacy_format():
    config = EntityConfig(**_VALID_CONFIG)
    entity_set = reconcile_config_to_entity_set(config)

    assert entity_set["site_key"] == "tgv"
    assert entity_set["site_name"] == "The Green Village"
    assert entity_set["thing"]["name"] == "Test Thing"
    assert entity_set["thing"]["description"] == "A test thing."
    assert entity_set["thing"]["properties"] == {"site": "tgv"}
    assert entity_set["location"]["name"] == "Test location"
    assert entity_set["location"]["location"]["type"] == "Point"
    assert len(entity_set["sensors"]) == 1
    assert entity_set["sensors"][0]["sensor_id"] == "test-sensor-1"
    assert entity_set["sensors"][0]["observed_properties"] == ["temperature", "humidity"]
    assert entity_set["sensors"][0]["properties"] == {}
    assert entity_set["observed_properties"]["temperature"] == "Indoor air temperature."


def test_reconcile_preserves_sensor_metadata():
    config = EntityConfig(**_VALID_CONFIG)
    entity_set = reconcile_config_to_entity_set(config)
    sensor = entity_set["sensors"][0]
    assert sensor["encodingType"] == "application/json"
    assert sensor["metadata"] == "https://example.com"
    assert sensor["description"] == "A test sensor."


# ---------------------------------------------------------------------------
# Test: loading multiple configs from a directory
# ---------------------------------------------------------------------------


def test_load_all_configs_from_directory(config_dir: Path):
    configs = load_all_configs(config_dir)
    # Should load a_thing.yaml and b_thing.yaml but skip _template.yaml
    assert len(configs) == 2
    names = {c.thing["name"] for c in configs}
    assert names == {"Thing A", "Thing B"}


def test_load_all_configs_skips_templates(config_dir: Path):
    configs = load_all_configs(config_dir)
    names = {c.thing["name"] for c in configs}
    assert "Template Thing" not in names


def test_load_entity_sets_returns_legacy_dicts(config_dir: Path):
    entity_sets = load_entity_sets(config_dir)
    assert len(entity_sets) == 2
    for es in entity_sets:
        assert "site_key" in es
        assert "thing" in es
        assert "location" in es
        assert "sensors" in es
        assert "observed_properties" in es


def test_load_all_configs_empty_dir(tmp_path: Path):
    d = tmp_path / "empty"
    d.mkdir()
    configs = load_all_configs(d)
    assert configs == []


def test_load_all_configs_nonexistent_dir(tmp_path: Path):
    configs = load_all_configs(tmp_path / "nonexistent")
    assert configs == []


# ---------------------------------------------------------------------------
# Test: real entity configs produce identical output to old hardcoded dicts
# ---------------------------------------------------------------------------


def test_real_entity_configs_load_successfully():
    """The actual entity_configs/ directory should load without errors."""
    repo_root = Path(__file__).resolve().parents[1]
    config_dir = repo_root / "entity_configs"
    if not config_dir.is_dir():
        pytest.skip("entity_configs/ directory not found")
    configs = load_all_configs(config_dir)
    assert len(configs) == 4, f"Expected 4 entity configs, got {len(configs)}"


def test_real_entity_configs_match_expected_things():
    """The YAML configs should define the same Things as the old Python dicts."""
    repo_root = Path(__file__).resolve().parents[1]
    config_dir = repo_root / "entity_configs"
    if not config_dir.is_dir():
        pytest.skip("entity_configs/ directory not found")
    entity_sets = load_entity_sets(config_dir)
    thing_names = {es["thing"]["name"] for es in entity_sets}
    expected = {"TGV Office Lab", "Climate Davis", "Weather Climatics", "Hitteplein"}
    assert thing_names == expected


def test_real_entity_configs_preserve_coordinates():
    """Coordinates must be preserved exactly."""
    repo_root = Path(__file__).resolve().parents[1]
    config_dir = repo_root / "entity_configs"
    if not config_dir.is_dir():
        pytest.skip("entity_configs/ directory not found")
    entity_sets = load_entity_sets(config_dir)
    for es in entity_sets:
        coords = es["location"]["location"]["coordinates"]
        assert coords == [4.377633926937161, 51.99658144237765], (
            f"Coordinates mismatch for {es['thing']['name']}: {coords}"
        )
