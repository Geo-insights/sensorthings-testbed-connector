"""Tests for the STA domain models (v1/v2 hierarchy)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.sta.models import (
    DatastreamV1,
    DatastreamV2,
    LocationV1,
    LocationV2,
    ObservationV1,
    ObservationV2,
    ObservedPropertyV1,
    ObservedPropertyV2,
    SensorV1,
    SensorV2,
    STAActuator,
    STADatastream,
    STALocation,
    STAObservation,
    STAObservedProperty,
    STAProject,
    STASensor,
    STATaskingCapability,
    STAThing,
    ThingV1,
    ThingV2,
    UnitOfMeasurement,
    class_map_for,
)

# ---------------------------------------------------------------------------
# entity_type derivation
# ---------------------------------------------------------------------------


class TestEntityType:
    def test_thing_entity_type(self):
        assert STAThing(name="T1").entity_type == "Thing"

    def test_sensor_entity_type(self):
        assert STASensor(name="S1").entity_type == "Sensor"

    def test_datastream_entity_type(self):
        assert STADatastream(name="D1").entity_type == "Datastream"

    def test_observation_entity_type(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0)
        assert obs.entity_type == "Observation"

    def test_project_entity_type(self):
        assert STAProject(name="P1").entity_type == "Project"

    def test_actuator_entity_type(self):
        assert STAActuator(name="A1").entity_type == "Actuator"

    def test_tasking_capability_entity_type(self):
        assert STATaskingCapability(name="TC1").entity_type == "TaskingCapability"

    def test_v1_thing_entity_type(self):
        assert ThingV1(name="T1").entity_type == "Thing"

    def test_v2_thing_entity_type(self):
        assert ThingV2(name="T1").entity_type == "Thing"

    def test_v1_datastream_entity_type(self):
        assert DatastreamV1(name="D1").entity_type == "Datastream"

    def test_v2_datastream_entity_type(self):
        assert DatastreamV2(name="D1").entity_type == "Datastream"

    def test_v1_observation_entity_type(self):
        obs = ObservationV1(phenomenonTime=datetime.now(UTC), result=1.0)
        assert obs.entity_type == "Observation"

    def test_v2_observation_entity_type(self):
        obs = ObservationV2(phenomenonTime=datetime.now(UTC), result=1.0)
        assert obs.entity_type == "Observation"


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            STAThing(name="")

    def test_whitespace_only_name_rejected(self):
        with pytest.raises(ValidationError):
            STAThing(name="   ")

    def test_long_name_rejected(self):
        with pytest.raises(ValidationError):
            STAThing(name="x" * 257)

    def test_max_length_name_accepted(self):
        thing = STAThing(name="x" * 256)
        assert len(thing.name) == 256

    def test_name_whitespace_stripped(self):
        thing = STAThing(name="  My Thing  ")
        assert thing.name == "My Thing"


# ---------------------------------------------------------------------------
# partial_eq
# ---------------------------------------------------------------------------


class TestPartialEq:
    def test_equal_things(self):
        a = STAThing(name="T1", description="desc", properties={"k": "v"})
        b = STAThing(name="T1", description="desc", properties={"k": "v"})
        assert a.partial_eq(b)

    def test_different_ids_still_equal(self):
        a = STAThing(name="T1", iot_id="1")
        b = STAThing(name="T1", iot_id="99")
        assert a.partial_eq(b)

    def test_different_names_not_equal(self):
        a = STAThing(name="T1")
        b = STAThing(name="T2")
        assert not a.partial_eq(b)

    def test_different_types_not_equal(self):
        a = STAThing(name="X")
        b = STASensor(name="X")
        assert not a.partial_eq(b)

    def test_datastream_ignores_linked_ids(self):
        a = STADatastream(name="D1", thing_id="1", sensor_id="2", observed_property_id="3")
        b = STADatastream(name="D1", thing_id="99", sensor_id="88", observed_property_id="77")
        assert a.partial_eq(b)

    def test_location_ignores_thing_id(self):
        a = STALocation(name="L1", thing_id="1")
        b = STALocation(name="L1", thing_id="99")
        assert a.partial_eq(b)

    def test_observation_ignores_datastream_id(self):
        now = datetime.now(UTC)
        a = STAObservation(phenomenonTime=now, result=1.0, datastream_id="1")
        b = STAObservation(phenomenonTime=now, result=1.0, datastream_id="99")
        assert a.partial_eq(b)

    def test_v1_partial_eq(self):
        a = ThingV1(name="T1", description="d")
        b = ThingV1(name="T1", description="d")
        assert a.partial_eq(b)

    def test_v2_partial_eq(self):
        a = ThingV2(name="T1", description="d")
        b = ThingV2(name="T1", description="d")
        assert a.partial_eq(b)

    def test_v1_v2_not_equal(self):
        """V1 and V2 are different types, so partial_eq returns False."""
        a = ThingV1(name="T1")
        b = ThingV2(name="T1")
        assert not a.partial_eq(b)


# ---------------------------------------------------------------------------
# FROST payload - v1.1 (base classes use v1.1 semantics)
# ---------------------------------------------------------------------------


class TestFrostPayload:
    def test_thing_payload(self):
        thing = STAThing(name="T1", description="desc", properties={"k": "v"}, iot_id="42")
        payload = thing.as_frost_payload()
        assert payload["name"] == "T1"
        assert payload["description"] == "desc"
        assert payload["properties"] == {"k": "v"}
        assert "iot_id" not in payload
        assert "entity_type" not in payload

    def test_location_with_thing_link(self):
        loc = STALocation(name="L1", location={"type": "Point", "coordinates": [4.37, 51.99]}, thing_id="5")
        payload = loc.as_frost_payload()
        assert payload["Things"] == [{"@iot.id": "5"}]
        assert "thing_id" not in payload

    def test_datastream_with_links(self):
        ds = STADatastream(
            name="D1",
            unitOfMeasurement=UnitOfMeasurement(name="Celsius", symbol="Cel", definition="http://x"),
            thing_id="1",
            sensor_id="2",
            observed_property_id="3",
        )
        payload = ds.as_frost_payload()
        assert payload["Thing"] == {"@iot.id": "1"}
        assert payload["Sensor"] == {"@iot.id": "2"}
        assert payload["ObservedProperty"] == {"@iot.id": "3"}
        for key in ("thing_id", "sensor_id", "observed_property_id"):
            assert key not in payload

    def test_observation_with_datastream_link(self):
        obs = STAObservation(
            phenomenonTime=datetime(2026, 7, 1, tzinfo=UTC),
            result=21.5,
            datastream_id="10",
        )
        payload = obs.as_frost_payload()
        assert payload["Datastream"] == {"@iot.id": "10"}
        assert "datastream_id" not in payload

    def test_tasking_capability_links(self):
        tc = STATaskingCapability(name="TC1", actuator_id="5", thing_id="10")
        payload = tc.as_frost_payload()
        assert payload["Actuator"] == {"@iot.id": "5"}
        assert payload["Thing"] == {"@iot.id": "10"}

    def test_observation_no_link_when_none(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0)
        payload = obs.as_frost_payload()
        assert "Datastream" not in payload


# ---------------------------------------------------------------------------
# V1 subclass payloads
# ---------------------------------------------------------------------------


class TestV1Payload:
    def test_thing_v1(self):
        t = ThingV1(name="T1", description="d", properties={"k": "v"})
        payload = t.as_frost_payload()
        assert payload["name"] == "T1"
        assert "iot_id" not in payload

    def test_datastream_v1_has_uom(self):
        ds = DatastreamV1(
            name="D1",
            unitOfMeasurement=UnitOfMeasurement(name="Celsius", symbol="Cel", definition="http://x"),
            thing_id="1",
            sensor_id="2",
            observed_property_id="3",
        )
        payload = ds.as_frost_payload()
        assert "unitOfMeasurement" in payload
        assert "observationType" in payload
        assert payload["Thing"] == {"@iot.id": "1"}
        assert payload["ObservedProperty"] == {"@iot.id": "3"}

    def test_observation_v1_has_parameters_and_result_quality(self):
        obs = ObservationV1(
            phenomenonTime=datetime(2026, 7, 1, tzinfo=UTC),
            result=21.5,
            parameters={"unit": "Cel"},
            datastream_id="10",
        )
        payload = obs.as_frost_payload()
        assert "parameters" in payload
        assert "resultQuality" in payload
        assert payload["Datastream"] == {"@iot.id": "10"}

    def test_location_v1_refs(self):
        loc = LocationV1(name="L1", thing_id="5")
        payload = loc.as_frost_payload()
        assert payload["Things"] == [{"@iot.id": "5"}]


# ---------------------------------------------------------------------------
# V2 subclass payloads
# ---------------------------------------------------------------------------


class TestV2Payload:
    def test_thing_v2(self):
        t = ThingV2(name="T1", description="d", definition="http://example.com")
        payload = t.as_frost_payload()
        assert payload["name"] == "T1"
        assert payload["definition"] == "http://example.com"

    def test_thing_v2_no_definition(self):
        t = ThingV2(name="T1")
        payload = t.as_frost_payload()
        assert "definition" not in payload  # None excluded

    def test_datastream_v2_has_result_type(self):
        ds = DatastreamV2(
            name="D1",
            unitOfMeasurement=UnitOfMeasurement(name="Celsius", symbol="Cel", definition="http://x"),
            thing_id="1",
            sensor_id="2",
            observed_property_id="3",
        )
        payload = ds.as_frost_payload()
        assert "resultType" in payload
        assert payload["resultType"]["type"] == "Quantity"
        assert payload["resultType"]["uom"]["code"] == "Cel"
        assert "unitOfMeasurement" not in payload
        assert "observationType" not in payload

    def test_datastream_v2_entity_refs(self):
        ds = DatastreamV2(
            name="D1",
            unitOfMeasurement=UnitOfMeasurement(name="Celsius", symbol="Cel", definition="http://x"),
            thing_id="1",
            sensor_id="2",
            observed_property_id="3",
        )
        payload = ds.as_frost_payload()
        assert payload["Thing"] == {"id": "1"}
        assert payload["Sensor"] == {"id": "2"}
        # v2: plural ObservedProperties
        assert "ObservedProperty" not in payload
        assert payload["ObservedProperties"] == [{"id": "3"}]

    def test_datastream_v2_explicit_result_type(self):
        ds = DatastreamV2(
            name="D1",
            resultType={"type": "Category", "constraint": {"enum": ["A", "B"]}},
        )
        payload = ds.as_frost_payload()
        assert payload["resultType"]["type"] == "Category"
        assert "unitOfMeasurement" not in payload

    def test_observation_v2_properties_not_parameters(self):
        obs = ObservationV2(
            phenomenonTime=datetime(2026, 7, 1, tzinfo=UTC),
            result=21.5,
            parameters={"unit": "Cel", "sensor_id": "s1"},
            datastream_id="10",
        )
        payload = obs.as_frost_payload()
        assert "parameters" not in payload
        assert payload["properties"] == {"unit": "Cel", "sensor_id": "s1"}
        assert "resultQuality" not in payload
        assert payload["Datastream"] == {"id": "10"}

    def test_observation_v2_no_result_quality(self):
        obs = ObservationV2(
            phenomenonTime=datetime(2026, 7, 1, tzinfo=UTC),
            result=1.0,
        )
        payload = obs.as_frost_payload()
        assert "resultQuality" not in payload

    def test_location_v2_refs(self):
        loc = LocationV2(name="L1", thing_id="5")
        payload = loc.as_frost_payload()
        assert payload["Things"] == [{"id": "5"}]

    def test_sensor_v2_definition(self):
        s = SensorV2(name="S1", definition="http://example.com/sensor")
        payload = s.as_frost_payload()
        assert payload["definition"] == "http://example.com/sensor"


# ---------------------------------------------------------------------------
# FROST response parsing
# ---------------------------------------------------------------------------


class TestFrostResponseParsing:
    def test_thing_from_response(self):
        data = {"@iot.id": 42, "name": "T1", "description": "desc", "properties": {"k": "v"}}
        thing = STAThing.from_frost_response(data)
        assert thing.iot_id == "42"
        assert thing.name == "T1"
        assert thing.properties == {"k": "v"}

    def test_sensor_from_response(self):
        data = {"id": 5, "name": "S1", "description": "d", "encodingType": "application/json", "metadata": "http://x"}
        sensor = STASensor.from_frost_response(data)
        assert sensor.iot_id == "5"
        assert sensor.name == "S1"

    def test_datastream_from_response_with_expanded_links(self):
        data = {
            "@iot.id": 10,
            "name": "D1",
            "description": "d",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": "Cel", "symbol": "Cel", "definition": "http://x"},
            "Thing": {"@iot.id": 1},
            "Sensor": {"@iot.id": 2},
            "ObservedProperty": {"@iot.id": 3},
        }
        ds = STADatastream.from_frost_response(data)
        assert ds.iot_id == "10"
        assert ds.thing_id == "1"
        assert ds.sensor_id == "2"
        assert ds.observed_property_id == "3"
        assert ds.unitOfMeasurement is not None
        assert ds.unitOfMeasurement.symbol == "Cel"

    def test_round_trip(self):
        original = STAThing(name="T1", description="desc", properties={"k": "v"})
        payload = original.as_frost_payload()
        payload["@iot.id"] = 42
        restored = STAThing.from_frost_response(payload)
        assert original.partial_eq(restored)
        assert restored.iot_id == "42"

    def test_v1_thing_from_response(self):
        data = {"@iot.id": 1, "name": "T1", "description": "d"}
        t = ThingV1.from_frost_response(data)
        assert t.iot_id == "1"
        assert t.name == "T1"

    def test_v2_thing_from_response(self):
        data = {"id": "abc-123", "name": "T1", "description": "d", "definition": "http://x"}
        t = ThingV2.from_frost_response(data)
        assert t.iot_id == "abc-123"
        assert t.definition == "http://x"

    def test_v2_datastream_from_response(self):
        data = {
            "id": "ds-1",
            "name": "D1",
            "description": "d",
            "resultType": {"type": "Quantity", "uom": {"code": "Cel"}},
            "Thing": {"id": "t1"},
            "Sensor": {"id": "s1"},
            "ObservedProperties": [{"id": "op1"}],
        }
        ds = DatastreamV2.from_frost_response(data)
        assert ds.iot_id == "ds-1"
        assert ds.resultType == {"type": "Quantity", "uom": {"code": "Cel"}}
        assert ds.thing_id == "t1"
        assert ds.sensor_id == "s1"
        assert ds.observed_property_id == "op1"

    def test_v2_observation_from_response(self):
        data = {
            "id": "obs-1",
            "phenomenonTime": "2026-07-01T00:00:00+00:00",
            "result": 21.5,
            "properties": {"unit": "Cel"},
            "Datastream": {"id": "ds-1"},
        }
        obs = ObservationV2.from_frost_response(data)
        assert obs.iot_id == "obs-1"
        assert obs.parameters == {"unit": "Cel"}
        assert obs.datastream_id == "ds-1"


# ---------------------------------------------------------------------------
# ResultQuality coercion
# ---------------------------------------------------------------------------


class TestObservationResultQualityCoercion:
    def test_string_coerced_to_list(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0, resultQuality="good")
        assert obs.resultQuality == ["good"]

    def test_list_kept_as_list(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0, resultQuality=["excellent"])
        assert obs.resultQuality == ["excellent"]

    def test_default_is_good_list(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0)
        assert obs.resultQuality == ["good"]


# ---------------------------------------------------------------------------
# class_map_for dispatch
# ---------------------------------------------------------------------------


class TestClassMapFor:
    def test_v11_returns_v1_classes(self):
        m = class_map_for("1.1")
        assert m["Thing"] is ThingV1
        assert m["Location"] is LocationV1
        assert m["Sensor"] is SensorV1
        assert m["ObservedProperty"] is ObservedPropertyV1
        assert m["Datastream"] is DatastreamV1
        assert m["Observation"] is ObservationV1

    def test_v20_returns_v2_classes(self):
        m = class_map_for("2.0")
        assert m["Thing"] is ThingV2
        assert m["Location"] is LocationV2
        assert m["Sensor"] is SensorV2
        assert m["ObservedProperty"] is ObservedPropertyV2
        assert m["Datastream"] is DatastreamV2
        assert m["Observation"] is ObservationV2

    def test_v_prefix_stripped(self):
        m = class_map_for("v2.0")
        assert m["Thing"] is ThingV2

    def test_bare_1_returns_v1(self):
        m = class_map_for("1")
        assert m["Thing"] is ThingV1

    def test_bare_2_returns_v2(self):
        m = class_map_for("2")
        assert m["Observation"] is ObservationV2

    def test_all_entity_types_present(self):
        for version in ("1.1", "2.0"):
            m = class_map_for(version)
            assert set(m.keys()) == {"Thing", "Location", "Sensor", "ObservedProperty", "Datastream", "Observation"}


# ---------------------------------------------------------------------------
# Backward compatibility: old STA* names still work
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify that constructing via STAThing, STADatastream, etc. still works."""

    def test_sta_thing_construction(self):
        t = STAThing(name="T1")
        assert t.name == "T1"

    def test_sta_datastream_construction(self):
        ds = STADatastream(name="D1")
        assert ds.name == "D1"

    def test_sta_observation_construction(self):
        obs = STAObservation(phenomenonTime=datetime.now(UTC), result=1.0)
        assert obs.result == 1.0

    def test_sta_location_construction(self):
        loc = STALocation(name="L1")
        assert loc.name == "L1"

    def test_sta_sensor_construction(self):
        s = STASensor(name="S1")
        assert s.name == "S1"

    def test_sta_observed_property_construction(self):
        op = STAObservedProperty(name="OP1")
        assert op.name == "OP1"
