import json
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
        frost_targets=(),
        frost_batch_push_enabled=False,
        frost_v2_batch_push_enabled=False,
        frost_batch_max_observations=500,
        frost_batch_timeout_seconds=60,
        frost_batch_max_concurrency=4,
        frost_cb_failure_threshold=3,
        frost_cb_cooldown_seconds=600,
        failed_dlq_max_bytes=50_000_000,
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

    monkeypatch.setattr(client, "_post_observation", lambda endpoint, payload, datastream_id, base_url=None: FakeResponse())

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

    def fake_fetch(endpoint, query_filter, base_url=None):
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


# -- warm_datastream_cache purge tests -----------------------------------------


def _make_frost_response(datastreams, next_link=None):
    """Build a mock FROST v1.1 Datastreams response."""
    body = {"value": datastreams}
    if next_link:
        body["@iot.nextLink"] = next_link
    return body


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def test_warm_datastream_cache_purges_stale_ids(monkeypatch, tmp_path):
    """Cached DS IDs that don't exist on the server should be purged."""
    # Pre-seed the cache with a stale entry (ID 9999) and a valid one (ID 10).
    cache_data = {
        "things": {},
        "locations": {},
        "sensors": {},
        "observed_properties": {},
        "datastreams": {
            "valid-sensor::temperature": "10",
            "stale-sensor::pm10": "9999",
        },
        "projects": {},
        "actuators": {},
        "tasking_capabilities": {},
    }
    cache_path = tmp_path / "registered_entities.json"
    cache_path.write_text(json.dumps(cache_data))

    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, registered_entities_path=str(cache_path)),
    )
    client = SensorThingsClient()

    # Verify both entries were loaded.
    assert client._datastream_ids.get("stale-sensor::pm10") == "9999"
    assert client._datastream_ids.get("valid-sensor::temperature") == "10"

    # Server returns only DS ID 10 (valid).
    server_response = _make_frost_response([
        {"@iot.id": 10, "name": "GEO_temperature", "properties": {"sensor_id": "valid-sensor", "observed_property": "temperature"}},
    ])
    monkeypatch.setattr(
        "app.services.sensorthings_client.requests.get",
        lambda *a, **kw: _FakeResponse(server_response),
    )
    client.warm_datastream_cache()

    # Stale entry should be purged from memory and cache file.
    assert "stale-sensor::pm10" not in client._datastream_ids
    assert client._datastream_ids.get("valid-sensor::temperature") == "10"
    reloaded = json.loads(cache_path.read_text())
    assert "stale-sensor::pm10" not in reloaded.get("datastreams", {})


def test_warm_datastream_cache_follows_pagination(monkeypatch, tmp_path):
    """Warm-up should follow nextLink to fetch all server datastreams."""
    cache_path = tmp_path / "registered_entities.json"
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, registered_entities_path=str(cache_path)),
    )
    client = SensorThingsClient()

    page1 = _make_frost_response(
        [{"@iot.id": 1, "name": "DS1", "properties": {"sensor_id": "s1", "observed_property": "temp"}}],
        next_link="https://example.test/v1.1/Datastreams?$skip=1",
    )
    page2 = _make_frost_response(
        [{"@iot.id": 2, "name": "DS2", "properties": {"sensor_id": "s2", "observed_property": "humidity"}}],
    )
    call_count = 0

    def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(page1 if call_count == 1 else page2)

    monkeypatch.setattr("app.services.sensorthings_client.requests.get", fake_get)
    client.warm_datastream_cache()

    assert call_count == 2
    assert client._datastream_ids.get("s1::temp") == "1"
    assert client._datastream_ids.get("s2::humidity") == "2"


def test_warm_datastream_cache_no_purge_on_fetch_failure(monkeypatch, tmp_path):
    """If the server fetch fails, no entries should be purged."""
    cache_data = {
        "things": {}, "locations": {}, "sensors": {}, "observed_properties": {},
        "datastreams": {"some-sensor::co2": "42"},
        "projects": {}, "actuators": {}, "tasking_capabilities": {},
    }
    cache_path = tmp_path / "registered_entities.json"
    cache_path.write_text(json.dumps(cache_data))

    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        _settings(tmp_path, registered_entities_path=str(cache_path)),
    )
    client = SensorThingsClient()
    assert client._datastream_ids.get("some-sensor::co2") == "42"

    def fail_get(*args, **kwargs):
        raise ConnectionError("server down")

    monkeypatch.setattr("app.services.sensorthings_client.requests.get", fail_get)
    warmed = client.warm_datastream_cache()

    assert warmed == 0
    assert client._datastream_ids.get("some-sensor::co2") == "42"
