from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_site_registration_preview_returns_preview(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.client.build_site_registration_preview",
        lambda site_key: type("Preview", (), {"model_dump": lambda self, mode: {"site_key": site_key, "mode": "preview"}})(),
    )

    response = client.get("/connector/registration-preview/tgv")

    assert response.status_code == 200
    assert response.json() == {"site_key": "tgv", "mode": "preview"}


def test_register_site_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.client.register_site_entities",
        lambda site_key: {"site_key": site_key, "mode": "live", "site_results": []},
    )

    response = client.post("/connector/register-site/tgv")

    assert response.status_code == 200
    assert response.json() == {"site_key": "tgv", "mode": "live", "site_results": []}


def test_register_site_tasking_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.client.register_site_tasking",
        lambda site_key: {"site_key": site_key, "mode": "live", "capability_ids": {}},
    )

    response = client.post("/connector/tasking/register-site/tgv")

    assert response.status_code == 200
    assert response.json() == {"site_key": "tgv", "mode": "live", "capability_ids": {}}


def test_create_task_route_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.client.create_task",
        lambda request: {"ok": True, "site_key": request.site_key, "capability_key": request.capability_key},
    )

    response = client.post(
        "/connector/tasking/tasks",
        json={
            "site_key": "tgv",
            "capability_key": "set_reporting_interval",
            "input": {"interval_seconds": 60},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "site_key": "tgv", "capability_key": "set_reporting_interval"}


def test_list_tasks_route_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.client.list_tasks",
        lambda query: {"site_key": query.site_key, "tasks": []},
    )

    response = client.get("/connector/tasking/tasks", params={"site_key": "tgv", "top": 5})

    assert response.status_code == 200
    assert response.json() == {"site_key": "tgv", "tasks": []}


def test_monitoring_mqtt_preview_route_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.build_monitoring_mqtt_preview",
        lambda readings: type(
            "Preview",
            (),
            {"model_dump": lambda self, mode: {"mode": "preview", "payloads": [{"datastream_id": "1"}]}}
        )(),
    )
    monkeypatch.setattr(
        "app.routes.connector.load_bridge_readings",
        lambda: [],
    )

    response = client.get("/connector/monitoring-mqtt-preview")

    assert response.status_code == 200
    assert response.json()["payloads"][0]["datastream_id"] == "1"


def test_monitoring_mqtt_push_route_returns_result(monkeypatch):
    monkeypatch.setattr(
        "app.routes.connector.publish_monitoring_mqtt",
        lambda readings: {"mode": "live", "published": len(readings)},
    )

    response = client.post(
        "/connector/monitoring-mqtt-push",
        json={
            "readings": [
                {
                    "sensor_id": "pump-rpm-01",
                    "sensor_name": "Pump RPM Sensor",
                    "observed_property": "pump_speed",
                    "unit": "rpm",
                    "value": 1450,
                    "timestamp": "2026-04-17T10:00:00Z",
                    "quality": "good",
                    "location": "tgv",
                    "thing_name": "Climate adaptation setup"
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {"mode": "live", "published": 1}