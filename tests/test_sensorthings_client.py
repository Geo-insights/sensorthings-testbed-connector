from types import SimpleNamespace

from app.services.sensorthings_client import SensorThingsClient


def test_client_prefers_basic_auth_and_prefixes_registration_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        SimpleNamespace(
            registered_entities_path=str(tmp_path / "registered_entities.json"),
            failed_observations_path=str(tmp_path / "failed_observations.jsonl"),
            datastream_ids={},
            sensorthings_base_url="https://example.test/v1.1",
            auth_username="write",
            auth_password="secret",
            auth_token="token-that-should-not-be-used",
            entity_name_prefix="GEO_",
            connector_name="UrbanAdapt SensorThings Connector",
            things_path="/Things",
            locations_path="/Locations",
            sensors_path="/Sensors",
            observed_properties_path="/ObservedProperties",
            datastreams_path="/Datastreams",
            observations_path="/Observations",
            request_timeout_seconds=15.0,
        ),
    )

    client = SensorThingsClient()

    headers = client._headers()
    preview = client.build_registration_preview()

    assert headers["Authorization"] == "Basic d3JpdGU6c2VjcmV0"
    assert preview.thing["name"] == "GEO_Climate adaptation sensor network"
    assert preview.sensors[0]["name"].startswith("GEO_")
    assert preview.datastreams[0]["name"].startswith("GEO_")