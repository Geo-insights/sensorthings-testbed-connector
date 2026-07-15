"""Tests for the pipeline ABCs and component registry."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.models import SensorReading
from app.pipeline.base import Decoder, Decapsulator, Deserializer, Normalizer, Parser
from app.pipeline.registry import ComponentRegistry, PipelineComponents, registry


# ---------------------------------------------------------------------------
# Concrete stubs for each ABC
# ---------------------------------------------------------------------------

class StubDecoder(Decoder):
    def decode(self, raw: bytes) -> dict[str, Any]:
        return {"decoded": raw.decode()}


class StubDeserializer(Deserializer):
    def deserialize(self, record: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in record.items()}


class StubDecapsulator(Decapsulator):
    def decapsulate(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return [record]


class StubParser(Parser):
    def parse(self, raw: dict[str, Any]) -> SensorReading:
        return SensorReading(
            sensor_id="s1",
            sensor_name="Sensor 1",
            observed_property="temperature",
            unit="°C",
            value=raw.get("value", 0.0),
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


class StubNormalizer(Normalizer):
    def normalize(self, reading: SensorReading) -> SensorReading:
        return reading.model_copy(update={"unit": "K", "value": reading.value + 273.15})


# ---------------------------------------------------------------------------
# ABC instantiation tests
# ---------------------------------------------------------------------------

class TestABCsCannotBeInstantiated:
    @pytest.mark.parametrize("abc_cls", [Decoder, Deserializer, Decapsulator, Parser, Normalizer])
    def test_cannot_instantiate(self, abc_cls):
        with pytest.raises(TypeError):
            abc_cls()


# ---------------------------------------------------------------------------
# Concrete implementation tests
# ---------------------------------------------------------------------------

class TestConcreteDecoder:
    def test_decode(self):
        decoder = StubDecoder()
        result = decoder.decode(b"hello")
        assert result == {"decoded": "hello"}


class TestConcreteDeserializer:
    def test_deserialize(self):
        ds = StubDeserializer()
        result = ds.deserialize({"a": 1, "b": 2})
        assert result == {"a": 1, "b": 2}


class TestConcreteDecapsulator:
    def test_decapsulate(self):
        dc = StubDecapsulator()
        result = dc.decapsulate({"payload": "data"})
        assert result == [{"payload": "data"}]


class TestConcreteParser:
    def test_parse(self):
        parser = StubParser()
        reading = parser.parse({"value": 21.5})
        assert isinstance(reading, SensorReading)
        assert reading.value == 21.5
        assert reading.sensor_id == "s1"


class TestConcreteNormalizer:
    def test_normalize(self):
        normalizer = StubNormalizer()
        reading = SensorReading(
            sensor_id="s1",
            sensor_name="Sensor 1",
            observed_property="temperature",
            unit="°C",
            value=0.0,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        result = normalizer.normalize(reading)
        assert result.unit == "K"
        assert result.value == pytest.approx(273.15)


# ---------------------------------------------------------------------------
# ComponentRegistry tests
# ---------------------------------------------------------------------------

class TestComponentRegistry:
    def test_register_and_get(self):
        reg = ComponentRegistry()
        components = PipelineComponents(decoder=StubDecoder())
        reg.register("test-source", components)
        assert reg.get("test-source") is components

    def test_get_unknown_returns_none(self):
        reg = ComponentRegistry()
        assert reg.get("nonexistent") is None

    def test_available_sources(self):
        reg = ComponentRegistry()
        reg.register("alpha", PipelineComponents())
        reg.register("beta", PipelineComponents())
        sources = reg.available_sources()
        assert sorted(sources) == ["alpha", "beta"]

    def test_available_sources_empty(self):
        reg = ComponentRegistry()
        assert reg.available_sources() == []

    def test_register_overwrites(self):
        reg = ComponentRegistry()
        first = PipelineComponents(decoder=StubDecoder())
        second = PipelineComponents(parser=StubParser())
        reg.register("src", first)
        reg.register("src", second)
        assert reg.get("src") is second


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestRegistrySingleton:
    def test_registry_is_component_registry(self):
        assert isinstance(registry, ComponentRegistry)

    def test_registry_is_module_level_singleton(self):
        from app.pipeline.registry import registry as registry2

        assert registry is registry2
