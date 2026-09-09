"""HTTP surface for the 14-day validation harness.

Endpoints:
  * ``GET /validation/status``    — harness flag + file sizes for a quick check.
  * ``GET /validation/summary``   — non-destructive in-memory rollup
                                    (per-(source, target) counters + latency
                                    percentiles from the current window).
  * ``GET /validation/incidents`` — last ``limit`` incidents (default 100).

Used during smoke testing to confirm the harness is capturing failures, and by
the report generator to sanity-check the running state before it reads the
JSONL files from disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Query

from app.config import settings
from app.services.validation import recorder

router = APIRouter(prefix="/validation", tags=["validation"])


@router.get("/status")
def validation_status() -> dict:
    data_dir = Path(settings.validation_data_dir)
    files: dict[str, dict[str, int]] = {}
    for name in ("events.jsonl", "incidents.jsonl", "snapshots.jsonl"):
        p = data_dir / name
        try:
            files[name] = {"bytes": p.stat().st_size}
        except FileNotFoundError:
            files[name] = {"bytes": 0}
    return {
        "enabled": settings.validation_harness_enabled,
        "data_dir": str(data_dir),
        "snapshot_interval_seconds": settings.validation_snapshot_interval_seconds,
        "files": files,
    }


@router.get("/summary")
def validation_summary() -> dict:
    return recorder.summary()


@router.get("/incidents")
def validation_incidents(limit: int = Query(default=100, ge=1, le=2000)) -> dict:
    """Return the last ``limit`` incident lines (newest last), safely parsed."""
    path = Path(settings.validation_data_dir) / "incidents.jsonl"
    lines: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    continue
    except FileNotFoundError:
        return {"count": 0, "incidents": []}
    return {"count": len(lines), "incidents": lines[-limit:]}
