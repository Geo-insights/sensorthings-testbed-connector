"""Tests for canonical datastream names."""

from app.sta.canonical import CanonicalDatastream
from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS


def test_all_members_have_meta():
    for member in CanonicalDatastream:
        meta = member.meta
        assert meta.display_name
        assert meta.unit
        assert meta.definition.startswith("http")


def test_enum_values_are_strings():
    assert CanonicalDatastream.TEMPERATURE == "temperature"
    assert CanonicalDatastream.CO2 == "co2"


def test_entity_sets_use_canonical_names():
    """Every observed_property key in entity sets must be a valid canonical name."""
    canonical_values = {member.value for member in CanonicalDatastream}
    for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
        for key in entity_set["observed_properties"]:
            assert key in canonical_values, (
                f"observed_property key '{key}' in entity set "
                f"'{entity_set['thing']['name']}' is not a CanonicalDatastream member"
            )


def test_meta_lookup():
    meta = CanonicalDatastream.TEMPERATURE.meta
    assert meta.display_name == "Air temperature"
    assert meta.unit == "Cel"
