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