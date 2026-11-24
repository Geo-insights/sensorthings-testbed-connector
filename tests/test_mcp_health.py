"""GET /health: the liveness route the FROST host's compose healthcheck calls.

Until 2026-11-24 the server had no /health, so the healthcheck got 404 and the container
stayed "unhealthy" while serving /mcp fine; Caddy's depends_on (service_healthy) would then
keep Caddy from starting on the next `docker compose up`.
"""

from __future__ import annotations

from starlette.testclient import TestClient


def test_health_route_answers_ok_on_the_real_app() -> None:
    from mcp_server import server

    client = TestClient(server.app, base_url="http://localhost:8030")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_touch_upstreams(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A slow or failing FROST/Supabase must not make the container unhealthy."""
    from mcp_server import server

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("/health must not create upstream clients")

    monkeypatch.setattr(server, "_get_client", boom)
    monkeypatch.setattr(server, "_get_supabase", boom)
    resp = TestClient(server.app, base_url="http://localhost:8030").get("/health")
    assert resp.status_code == 200
