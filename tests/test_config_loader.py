"""Tests for the YAML site configuration loader and validator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.sta.config_loader import load_all_site_configs, load_site_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_entity_set(**overrides) -> dict:
    """Return a minimal valid entity set dict, with optional overrides."""
    base = {
        "site_key": "test",
        "thing": {"name": "Thing1", "description": "A thing"},
        "location": {
            "name": "Loc1",
            "description": "A location",
            "location": {"type": "Point", "coordinates": [4.37, 51.99]},
        },
        "sensors": [
            {
                "sensor_id": "s1",
                "name": "Sensor1",
                "description": "A sensor",
                "observed_properties": ["temperature"],
            }
        ],
        "observed_properties": {
            "temperature": {
                "name": "Air temperature",
                "definition": "https://example.org/temperature",
                "description": "Temp",
                "unit": "Cel",
            }
        },
    }
    base.update(overrides)
    return base


def _write_yaml(path: Path, data) -> Path:
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Loading a valid YAML site config file
# ---------------------------------------------------------------------------

class TestLoadValidConfig:
    def test_single_entity_set(self, tmp_path):
        cfg = _write_yaml(tmp_path / "site.yaml", [_minimal_entity_set()])
        entity_sets, errors = load_site_config(cfg)
        assert errors == []
        assert len(entity_sets) == 1
        assert entity_sets[0]["site_key"] == "test"

    def test_multiple_entity_sets_in_one_file(self, tmp_path):
        data = [
            _minimal_entity_set(site_key="a"),
            _minimal_entity_set(site_key="b"),
        ]
        cfg = _write_yaml(tmp_path / "multi.yaml", data)
        entity_sets, errors = load_site_config(cfg)
        assert errors == []
        assert len(entity_sets) == 2

    def test_real_tgv_config_loads_without_errors(self):
        tgv_path = Path(__file__).resolve().parent.parent / "config" / "sites" / "tgv.yaml"
        if not tgv_path.exists():
            pytest.skip("tgv.yaml not found in repo")
        entity_sets, errors = load_site_config(tgv_path)
        assert errors == []
        assert len(entity_sets) >= 1

    def test_multiple_sensors_per_entity_set(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"].append(
            {
                "sensor_id": "s2",
                "name": "Sensor2",
                "description": "Another sensor",
                "observed_properties": ["temperature"],
            }
        )
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        entity_sets, errors = load_site_config(cfg)
        assert errors == []
        assert len(entity_sets[0]["sensors"]) == 2


# ---------------------------------------------------------------------------
# Loading from a directory with multiple YAML files
# ---------------------------------------------------------------------------

class TestLoadAllSiteConfigs:
    def test_loads_multiple_yaml_files(self, tmp_path):
        _write_yaml(tmp_path / "alpha.yaml", [_minimal_entity_set(site_key="alpha")])
        _write_yaml(tmp_path / "beta.yaml", [_minimal_entity_set(site_key="beta")])
        entity_sets, errors = load_all_site_configs(tmp_path)
        assert errors == []
        assert len(entity_sets) == 2

    def test_loads_yml_extension(self, tmp_path):
        _write_yaml(tmp_path / "gamma.yml", [_minimal_entity_set(site_key="gamma")])
        entity_sets, errors = load_all_site_configs(tmp_path)
        assert errors == []
        assert len(entity_sets) == 1

    def test_ignores_non_yaml_files(self, tmp_path):
        _write_yaml(tmp_path / "good.yaml", [_minimal_entity_set()])
        (tmp_path / "readme.txt").write_text("not yaml", encoding="utf-8")
        entity_sets, errors = load_all_site_configs(tmp_path)
        assert errors == []
        assert len(entity_sets) == 1

    def test_empty_directory_returns_empty(self, tmp_path):
        entity_sets, errors = load_all_site_configs(tmp_path)
        assert entity_sets == []
        assert errors == []

    def test_aggregates_errors_across_files(self, tmp_path):
        _write_yaml(tmp_path / "good.yaml", [_minimal_entity_set()])
        # Missing required keys in second file
        _write_yaml(tmp_path / "bad.yaml", [{"site_key": "bad"}])
        entity_sets, errors = load_all_site_configs(tmp_path)
        assert len(errors) > 0
        # Good file still contributes its entity set
        assert len(entity_sets) == 2


# ---------------------------------------------------------------------------
# Validation errors for missing required keys
# ---------------------------------------------------------------------------

class TestMissingRequiredKeys:
    def test_missing_top_level_keys(self, tmp_path):
        data = [{"site_key": "x", "thing": {"name": "T", "description": "d"}}]
        cfg = _write_yaml(tmp_path / "site.yaml", data)
        _, errors = load_site_config(cfg)
        assert len(errors) == 1
        assert "missing required keys" in errors[0]

    def test_missing_thing_keys(self, tmp_path):
        es = _minimal_entity_set()
        es["thing"] = {"name": "T"}  # missing description
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("thing" in e and "missing keys" in e for e in errors)

    def test_missing_location_keys(self, tmp_path):
        es = _minimal_entity_set()
        es["location"] = {"name": "L"}  # missing description and location
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("location" in e and "missing keys" in e for e in errors)

    def test_missing_sensor_keys(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"] = [{"sensor_id": "s1"}]  # missing name, description, observed_properties
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("sensors[0]" in e and "missing keys" in e for e in errors)

    def test_missing_observed_property_keys(self, tmp_path):
        es = _minimal_entity_set()
        es["observed_properties"]["temperature"] = {"name": "Temp"}  # missing definition, description, unit
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("observed_properties.temperature" in e and "missing keys" in e for e in errors)


# ---------------------------------------------------------------------------
# Validation errors for invalid field types
# ---------------------------------------------------------------------------

class TestInvalidFieldTypes:
    def test_thing_not_a_dict(self, tmp_path):
        es = _minimal_entity_set()
        es["thing"] = "not a dict"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("thing" in e and "must be a dict" in e for e in errors)

    def test_location_not_a_dict(self, tmp_path):
        es = _minimal_entity_set()
        es["location"] = "not a dict"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("location" in e and "must be a dict" in e for e in errors)

    def test_sensors_not_a_list(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"] = "not a list"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("sensors" in e and "must be a list" in e for e in errors)

    def test_sensor_item_not_a_dict(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"] = ["not a dict"]
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("sensors[0]" in e and "must be a dict" in e for e in errors)

    def test_observed_properties_not_a_dict(self, tmp_path):
        es = _minimal_entity_set()
        es["observed_properties"] = "not a dict"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("observed_properties" in e and "must be a dict" in e for e in errors)

    def test_observed_property_value_not_a_dict(self, tmp_path):
        es = _minimal_entity_set()
        es["observed_properties"]["temperature"] = "not a dict"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("observed_properties.temperature" in e and "must be a dict" in e for e in errors)

    def test_top_level_not_a_list(self, tmp_path):
        cfg = _write_yaml(tmp_path / "site.yaml", {"not": "a list"})
        _, errors = load_site_config(cfg)
        assert any("expected a YAML list" in e for e in errors)

    def test_list_item_not_a_dict(self, tmp_path):
        cfg = _write_yaml(tmp_path / "site.yaml", ["a string item"])
        _, errors = load_site_config(cfg)
        assert any("expected dict" in e for e in errors)


# ---------------------------------------------------------------------------
# IoT link integrity checks
# ---------------------------------------------------------------------------

class TestIoTLinkIntegrity:
    def test_sensor_references_nonexistent_observed_property(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"][0]["observed_properties"] = ["temperature", "humidity"]
        # Only "temperature" exists in observed_properties
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("humidity" in e and "not found in observed_properties" in e for e in errors)

    def test_all_references_valid_no_errors(self, tmp_path):
        es = _minimal_entity_set()
        es["observed_properties"]["humidity"] = {
            "name": "Relative humidity",
            "definition": "https://example.org/humidity",
            "description": "RH",
            "unit": "%",
        }
        es["sensors"][0]["observed_properties"] = ["temperature", "humidity"]
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert errors == []

    def test_multiple_sensors_with_broken_references(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"] = [
            {
                "sensor_id": "s1",
                "name": "Sensor1",
                "description": "d",
                "observed_properties": ["temperature", "nonexistent_a"],
            },
            {
                "sensor_id": "s2",
                "name": "Sensor2",
                "description": "d",
                "observed_properties": ["nonexistent_b"],
            },
        ]
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        _, errors = load_site_config(cfg)
        assert any("nonexistent_a" in e for e in errors)
        assert any("nonexistent_b" in e for e in errors)

    def test_empty_observed_properties_list_is_valid(self, tmp_path):
        es = _minimal_entity_set()
        es["sensors"][0]["observed_properties"] = []
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        # Empty list means no references to check; only missing-key validation may apply
        # but sensor still has the key, so no reference errors
        _, errors = load_site_config(cfg)
        assert not any("not found in observed_properties" in e for e in errors)


# ---------------------------------------------------------------------------
# Handling of non-existent file/directory paths
# ---------------------------------------------------------------------------

class TestNonExistentPaths:
    def test_nonexistent_file(self, tmp_path):
        missing = tmp_path / "does_not_exist.yaml"
        entity_sets, errors = load_site_config(missing)
        assert entity_sets == []
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_nonexistent_directory(self, tmp_path):
        missing_dir = tmp_path / "no_such_dir"
        entity_sets, errors = load_all_site_configs(missing_dir)
        assert entity_sets == []
        assert len(errors) == 1
        assert "not found" in errors[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_yaml_file(self, tmp_path):
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("", encoding="utf-8")
        entity_sets, errors = load_site_config(cfg)
        # yaml.safe_load("") returns None, which is not a list
        assert entity_sets == []
        assert len(errors) == 1
        assert "expected a YAML list" in errors[0]

    def test_yaml_with_only_null(self, tmp_path):
        cfg = tmp_path / "null.yaml"
        cfg.write_text("null\n", encoding="utf-8")
        entity_sets, errors = load_site_config(cfg)
        assert entity_sets == []
        assert any("expected a YAML list" in e for e in errors)

    def test_invalid_yaml_syntax(self, tmp_path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(":\n  - :\n    bad:: yaml::: content\n", encoding="utf-8")
        entity_sets, errors = load_site_config(cfg)
        assert entity_sets == []
        assert len(errors) >= 1

    def test_yaml_list_of_empty_dicts(self, tmp_path):
        cfg = _write_yaml(tmp_path / "site.yaml", [{}])
        entity_sets, errors = load_site_config(cfg)
        assert len(errors) > 0
        assert any("missing required keys" in e for e in errors)

    def test_extra_keys_are_tolerated(self, tmp_path):
        es = _minimal_entity_set()
        es["extra_field"] = "should be ignored"
        cfg = _write_yaml(tmp_path / "site.yaml", [es])
        entity_sets, errors = load_site_config(cfg)
        assert errors == []
        assert entity_sets[0]["extra_field"] == "should be ignored"
