"""Session-scoped fixtures for FROST integration tests.

Spins up FROST-Server + PostGIS via docker compose, waits for readiness,
and tears down after the test session.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

import pytest
import requests

COMPOSE_FILE = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir, "docker-compose.test.yaml"
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_frost(base_url: str, timeout: int = 180) -> None:
    """Poll FROST root endpoint until it responds or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/v1.1/", timeout=5)
            if resp.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(2)
    raise TimeoutError(f"FROST server at {base_url} did not become ready within {timeout}s")


@pytest.fixture(scope="session")
def frost_url():
    """Start FROST via docker compose and yield the base URL.

    Skips the entire session if Docker is unavailable.
    """
    # Check docker is available
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True, capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("Docker Compose not available")

    port = _find_free_port()
    env = {**os.environ, "FROST_PORT": str(port)}
    base_url = f"http://localhost:{port}/FROST-Server"

    # Start containers
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--wait"],
        check=True, env=env, capture_output=True, timeout=300,
    )

    try:
        _wait_for_frost(base_url)
        yield base_url
    finally:
        subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "down", "-v", "--remove-orphans"],
            env=env, capture_output=True, timeout=60,
        )


@pytest.fixture()
def frost_session(frost_url):
    """Return a requests.Session pre-configured with the FROST base URL."""
    session = requests.Session()
    session.base_url = frost_url  # type: ignore[attr-defined]
    return session
