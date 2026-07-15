from types import SimpleNamespace

from app.models import TaskingTaskCreateRequest, TaskingTaskQuery
from app.services.sensorthings_client import SensorThingsClient
from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS


def _settings(tmp_path, **overrides):
    base = dict(
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
        projects_path="/Projects",
        actuators_path="/Actuators",
        tasking_capabilities_path="/TaskingCapabilities",
        tasks_path="/Tasks",
        default_project_name="",
        default_project_id="",
        default_project_description="",
        default_project_public=True,
        site_project_configs={},
        site_tasking_configs={},
        tasking_allowed_commands=set(),
        request_timeout_seconds=15.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_client_prefers_basic_auth_and_prefixes_registration_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path),
    )

    client = SensorThingsClient()

    headers = client._headers()
    preview = client.build_registration_preview()

    assert headers["Authorization"] == "Basic d3JpdGU6c2VjcmV0"
    assert preview.thing["name"] == "GEO_Climate adaptation sensor network"
    assert preview.sensors[0]["name"].startswith("GEO_")
    assert preview.datastreams[0]["name"].startswith("GEO_")
    assert preview.project is None
    assert preview.endpoints["projects"] == "https://example.test/v1.1/Projects"


def test_registration_preview_includes_project_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, default_project_name="UrbanAdapt"),
    )

    client = SensorThingsClient()
    preview = client.build_registration_preview()

    assert preview.project is not None
    assert preview.project["name"] == "UrbanAdapt"
    assert preview.project["public"] is True


def test_register_entity_set_links_entities_to_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, default_project_name="UrbanAdapt"),
    )

    client = SensorThingsClient()

    captured: dict[str, list[dict]] = {}

    def fake_get_or_create(path, name, payload, collection):
        captured.setdefault(collection, []).append(payload)
        return f"id-{collection}", "created:201"

    monkeypatch.setattr(client, "_get_or_create_entity", fake_get_or_create)

    result = client.register_entity_set(CLIMATE_ADAPTATION_ENTITY_SETS[0])

    assert result["project_id"] == "id-projects"
    assert captured["things"][0]["Projects"] == [{"@iot.id": "id-projects"}]
    assert captured["locations"][0]["Projects"] == [{"@iot.id": "id-projects"}]
    assert captured["sensors"][0]["Projects"] == [{"@iot.id": "id-projects"}]
    # ObservedProperties are shared entities and must not be linked to a Project.
    assert all("Projects" not in payload for payload in captured.get("observed_properties", []))


def test_register_entity_set_skips_project_link_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path),
    )

    client = SensorThingsClient()

    captured: dict[str, list[dict]] = {}

    def fake_get_or_create(path, name, payload, collection):
        captured.setdefault(collection, []).append(payload)
        return f"id-{collection}", "created:201"

    monkeypatch.setattr(client, "_get_or_create_entity", fake_get_or_create)

    result = client.register_entity_set(CLIMATE_ADAPTATION_ENTITY_SETS[0])

    assert result["project_id"] is None
    assert "projects" not in captured
    assert "Projects" not in captured["things"][0]


def test_register_entity_set_uses_site_specific_project_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            default_project_name="GlobalFallback",
            site_project_configs={
                "tgv": {"name": "GI-TGV-Fieldlab", "description": "TGV project", "public": True},
                "blijdorp": {"name": "GI-Blijdorp-Fieldlab", "description": "Blijdorp project", "public": False},
            },
        ),
    )

    client = SensorThingsClient()

    captured: dict[str, list[dict]] = {}

    def fake_get_or_create(path, name, payload, collection):
        captured.setdefault(collection, []).append(payload)
        return f"id-{name}", "created:201"

    monkeypatch.setattr(client, "_get_or_create_entity", fake_get_or_create)

    tgv_entity_set = next(item for item in CLIMATE_ADAPTATION_ENTITY_SETS if item["site_key"] == "tgv")
    result = client.register_entity_set(tgv_entity_set)

    assert result["project_name"] == "GI-TGV-Fieldlab"
    assert result["project_id"] == "id-GI-TGV-Fieldlab"
    assert captured["projects"][0]["name"] == "GI-TGV-Fieldlab"
    assert captured["projects"][0]["description"] == "TGV project"


def test_site_registration_preview_uses_site_specific_project_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            site_project_configs={
                "tgv": {"name": "GI-TGV-Fieldlab", "public": True},
            },
        ),
    )

    client = SensorThingsClient()
    preview = client.build_site_registration_preview("tgv")

    assert preview is not None
    assert preview.project is not None
    assert preview.project["name"] == "GI-TGV-Fieldlab"


def test_register_site_tasking_creates_actuator_and_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            site_tasking_configs={
                "tgv": {
                    "actuators": [
                        {
                            "key": "weather-controller",
                            "name": "Weather Controller",
                            "description": "Controller",
                        }
                    ],
                    "capabilities": [
                        {
                            "key": "set_reporting_interval",
                            "name": "Set reporting interval",
                            "description": "Set interval",
                            "actuator_key": "weather-controller",
                            "thing_name": "Weather Climatics",
                        }
                    ],
                }
            },
        ),
    )

    client = SensorThingsClient()

    captured: dict[str, list[dict]] = {}

    def fake_get_or_create(path, name, payload, collection):
        captured.setdefault(collection, []).append(payload)
        return f"id-{collection}", "created:201"

    monkeypatch.setattr(client, "_get_or_create_entity", fake_get_or_create)
    monkeypatch.setattr(client, "_resolve_thing_id", lambda thing_name: "id-thing")

    result = client.register_site_tasking("tgv")

    assert result is not None
    assert result["actuator_ids"]["weather-controller"] == "id-actuators"
    assert result["capability_ids"]["set_reporting_interval"] == "id-tasking_capabilities"
    assert captured["tasking_capabilities"][0]["Actuator"] == {"@iot.id": "id-actuators"}


def test_create_task_rejects_not_allowed_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            tasking_allowed_commands={"set_reporting_interval"},
        ),
    )

    client = SensorThingsClient()
    request = TaskingTaskCreateRequest(site_key="tgv", capability_key="restart_sensor", input={"value": 1})
    result = client.create_task(request)

    assert result["ok"] is False
    assert "not allowed" in result["message"].lower()


def test_list_tasks_preview_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            sensorthings_base_url="",
        ),
    )

    client = SensorThingsClient()
    result = client.list_tasks(TaskingTaskQuery(site_key="tgv", top=5))

    assert result is not None
    assert result["mode"] == "preview"


def test_get_or_create_entity_drops_stale_cached_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path),
    )

    client = SensorThingsClient()
    client._registered_entities["things"] = {"GEO_Hitteplein": "3"}

    monkeypatch.setattr(
        client._entity_manager,
        "fetch_by_id",
        lambda path, entity_id: {"name": "Wrong Thing"},
    )
    monkeypatch.setattr(client._entity_manager, "find_by_name", lambda path, name: "42")

    entity_id, status = client._get_or_create_entity(
        "/Things",
        "GEO_Hitteplein",
        {"name": "GEO_Hitteplein", "description": "desc"},
        "things",
    )

    assert entity_id == "42"
    assert status == "existing"
    assert client._registered_entities["things"]["GEO_Hitteplein"] == "42"


def test_check_frost_status_aggregates_multiple_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(
            tmp_path,
            sensorthings_base_url="https://primary.example/v1.1",
            sensorthings_base_urls=("https://primary.example/v1.1", "https://secondary.example/v1.1"),
        ),
    )

    client = SensorThingsClient()

    monkeypatch.setattr(
        client,
        "_check_frost_status_for_base_url",
        lambda url: {
            "mode": "live",
            "root_url": url,
            "reachable": True,
            "status_code": 200,
            "things_count": 1,
            "things_status_code": 200,
        },
    )

    result = client.check_frost_status()

    assert result["mode"] == "live"
    assert result["multi_target"] is True
    assert result["reachable"] is True
    assert len(result["targets"]) == 2


def test_push_observations_uses_dynamic_datastream_lookup_when_mapping_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, datastream_ids={}),
    )

    client = SensorThingsClient()
    from app.models import SensorReading

    reading = SensorReading(
        sensor_id="sensor-1",
        sensor_name="Sensor 1",
        observed_property="air_temperature",
        observed_property_name="Air temperature",
        unit="Cel",
        value=21.3,
        timestamp="2026-01-01T12:00:00Z",
        thing_name="Hitteplein",
    )

    monkeypatch.setattr(client, "_resolve_datastream_id_live", lambda base_url, reading: "123")

    class FakeResponse:
        ok = True
        status_code = 201
        text = "created"

    monkeypatch.setattr(client, "_post_observation", lambda endpoint, payload, datastream_id: FakeResponse())

    result = client.push_observations([reading])

    assert result["mode"] == "live"
    assert result["sent"] == 1
    assert result["results"][0]["ok"] is True


def test_resolve_datastream_id_live_queries_by_device_eui_and_stream_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path),
    )

    client = SensorThingsClient()

    captured_filters: list[str] = []

    def fake_fetch(endpoint, query_filter):
        captured_filters.append(query_filter)
        return "777"

    monkeypatch.setattr(client, "_fetch_datastream_id_by_filter", fake_fetch)

    from app.models import SensorReading

    reading = SensorReading(
        sensor_id="sensor-1",
        sensor_name="Sensor 1",
        observed_property="air_temperature",
        unit="Cel",
        value=21.3,
        timestamp="2026-01-01T12:00:00Z",
        thing_name="Hitteplein",
        device_eui="ABC123",
        stream_key="temp",
    )

    datastream_id = client._resolve_datastream_id_live("https://example.test/v1.1", reading)

    assert datastream_id == "777"
    assert any("Thing/properties/deviceEui eq 'ABC123'" in item for item in captured_filters)
    assert any("properties/streamKey eq 'temp'" in item for item in captured_filters)