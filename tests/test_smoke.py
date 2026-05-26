from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_endpoint_is_available():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_preview_endpoint_returns_demo_payloads():
    response = client.get("/connector/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] in {"preview", "live"}
    assert len(body["readings"]) >= 1
    assert len(body["payloads"]) >= 1


def test_registration_preview_exposes_frost_entities():
    response = client.get("/connector/registration-preview")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] in {"preview", "live"}
    assert "thing" in body
    assert "datastreams" in body
    assert len(body["datastreams"]) >= 1


def test_frost_status_reports_preview_mode_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "app.services.sensorthings_client.settings",
        SimpleNamespace(sensorthings_base_url=""),
    )

    response = client.get("/frost/status")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "preview"
    assert body["reachable"] is False
    assert "preview mode" in body["message"].lower()


def test_ingest_preview_accepts_custom_bridge_payload():
    response = client.post(
        "/connector/ingest-preview",
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
    body = response.json()
    assert body["readings"][0]["sensor_id"] == "pump-rpm-01"
    assert body["payloads"][0]["result"] == 1450


def test_site_registration_preview_unknown_site_returns_404():
    response = client.get("/connector/registration-preview/unknown")

    assert response.status_code == 404


def test_register_site_unknown_site_returns_404():
    response = client.post("/connector/register-site/unknown")

    assert response.status_code == 404
