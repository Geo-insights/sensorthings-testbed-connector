"""setup_logging: the module loggers (app.*) get the main channel's handlers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import logging_config

_LOGGERS = ("connector", "connector.events", "connector.debug", "app")


@pytest.fixture()
def fresh_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in _LOGGERS}
    monkeypatch.setattr(logging_config, "_configured", False)
    monkeypatch.setattr(logging_config, "_LOG_DIR", tmp_path)
    for name in _LOGGERS:
        logging.getLogger(name).handlers = []
    yield tmp_path
    for name, (handlers, level) in saved.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)


def test_app_module_info_reaches_the_log_file(fresh_logging: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logging_config.setup_logging(debug=False)
    log = logging.getLogger("app.services.sensorthings_client")

    log.info("Datastream warm-up: listed 7 datastreams (complete)")
    log.warning("Not routing x via y")
    for handler in logging.getLogger("app").handlers:
        handler.flush()

    text = (fresh_logging / "connector.log").read_text(encoding="utf-8")
    assert "listed 7 datastreams (complete)" in text and "Not routing x via y" in text
    err = capsys.readouterr().err
    assert "[app.services.sensorthings_client:" in err and "WARNING - Not routing x via y" in err  # formatted
    assert "listed 7 datastreams" not in err  # INFO stays out of the console


def test_setup_logging_is_idempotent(fresh_logging: Path) -> None:
    logging_config.setup_logging(debug=False)
    logging_config.setup_logging(debug=False)
    assert len(logging.getLogger("app").handlers) == 2
