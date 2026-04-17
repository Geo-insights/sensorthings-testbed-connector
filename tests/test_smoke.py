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
                    "thing_name": "Brabantse Delta Wastewater Demo"
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["readings"][0]["sensor_id"] == "pump-rpm-01"
    assert body["payloads"][0]["result"] == 1450
