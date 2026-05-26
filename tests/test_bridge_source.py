from types import SimpleNamespace

from app.sources.bridge_source import load_bridge_readings


def test_bridge_source_prefers_payload_file_over_demo(monkeypatch, tmp_path):
    payload_path = tmp_path / "bridge.json"
    payload_path.write_text(
        """
        {
          "readings": [
            {
              "sensor_id": "sensor-1",
              "sensor_name": "Bridge Sensor",
              "observed_property": "air_temperature",
              "unit": "Cel",
              "value": 21.5,
              "timestamp": "2026-05-26T10:00:00Z",
              "quality": "good",
              "location": "tgv",
              "thing_name": "Bridge Thing"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.sources.bridge_source.settings",
        SimpleNamespace(bridge_payload_path=str(payload_path)),
    )
    monkeypatch.setattr(
        "app.sources.bridge_source.generate_demo_readings",
        lambda: [],
    )

    readings = load_bridge_readings()

    assert len(readings) == 1
    assert readings[0].sensor_id == "sensor-1"


def test_bridge_source_falls_back_to_demo_when_payload_missing(monkeypatch):
    monkeypatch.setattr(
        "app.sources.bridge_source.settings",
        SimpleNamespace(bridge_payload_path="missing.json"),
    )
    monkeypatch.setattr(
        "app.sources.bridge_source.generate_demo_readings",
        lambda: ["demo"],
    )

    readings = load_bridge_readings()

    assert readings == ["demo"]