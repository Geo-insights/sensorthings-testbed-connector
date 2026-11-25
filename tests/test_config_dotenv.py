"""The developer's .env is loaded by the app, but never inside the test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import SKIP_DOTENV_VAR, load_dotenv_unless_skipped

VAR = "CONNECTOR_TEST_DOTENV_PROBE"


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(VAR, raising=False)
    path = tmp_path / ".env"
    path.write_text(f"{VAR}=from-dotenv\n", encoding="utf-8")
    return path


def test_the_suite_runs_with_dotenv_skipped() -> None:
    assert os.environ.get(SKIP_DOTENV_VAR) == "1"


def test_skipped_dotenv_is_not_loaded(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DOTENV_VAR, "1")
    assert load_dotenv_unless_skipped(env_file) is False
    assert VAR not in os.environ


def test_dotenv_is_loaded_when_not_skipped(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SKIP_DOTENV_VAR, raising=False)
    try:
        assert load_dotenv_unless_skipped(env_file) is True
        assert os.environ[VAR] == "from-dotenv"
    finally:
        os.environ.pop(VAR, None)
