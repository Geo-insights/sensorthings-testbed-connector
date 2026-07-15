"""Structured multi-channel logging configuration.

Three channels:
  - connector:        WARNING to console, INFO to rotating file
  - connector.events: INFO to stdout (lifecycle events)
  - connector.debug:  DEBUG to file (only when DEBUG=true)
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(name)s:%(module)s:%(lineno)d]: %(levelname)s - %(message)s"
_LOG_DIR = Path("logs")
_configured = False


def setup_logging(debug: bool | None = None) -> None:
    """Configure the three-channel logging system.  Safe to call multiple times."""
    global _configured
    if _configured:
        return
    _configured = True

    if debug is None:
        debug = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_LOG_FORMAT)

    # -- Main channel: connector -------------------------------------------
    main_logger = logging.getLogger("connector")
    main_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    main_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        _LOG_DIR / "connector.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    main_logger.addHandler(file_handler)

    # -- Events channel: connector.events ----------------------------------
    events_logger = logging.getLogger("connector.events")
    events_handler = logging.StreamHandler()
    events_handler.setLevel(logging.INFO)
    events_handler.setFormatter(formatter)
    events_logger.addHandler(events_handler)

    # -- Debug channel: connector.debug ------------------------------------
    if debug:
        debug_logger = logging.getLogger("connector.debug")
        debug_handler = RotatingFileHandler(
            _LOG_DIR / "debug.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(formatter)
        debug_logger.addHandler(debug_handler)
