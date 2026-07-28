from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings
from app.services.health_monitor import health_monitor

router = APIRouter(tags=["health"])


@router.get("/")
def root() -> dict[str, str]:
    return {"message": "SensorThings testbed connector is running"}


@router.get("/health")
def health() -> dict:
    dlq_path = Path(settings.failed_observations_path)
    try:
        dlq_bytes = dlq_path.stat().st_size
    except FileNotFoundError:
        dlq_bytes = 0
    status = "degraded" if dlq_bytes > settings.failed_dlq_max_bytes * 0.9 else "ok"
    return {"status": status, "dlq_bytes": dlq_bytes, "dlq_max_bytes": settings.failed_dlq_max_bytes}


@router.get("/health/report")
def health_report() -> dict:
    return health_monitor.get_summary()


@router.get("/health/report/html", response_class=HTMLResponse)
def health_report_html() -> str:
    return health_monitor.render_html()
