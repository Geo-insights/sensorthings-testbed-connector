"""Tests for the STA domain models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.sta.models import (
    STAActuator,
    STADatastream,
    STAEntityBase,
    STALocation,
    STAObservation,
    STAObservedProperty,
    STAProject,
    STASensor,
    STATaskingCapability,
    STAThing,
    UnitOfMeasurement,
)


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
