from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services.health_monitor import health_monitor

router = APIRouter(tags=["health"])


@router.get("/")
def root() -> dict[str, str]:
    return {"message": "SensorThings testbed connector is running"}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/report")
def health_report() -> dict:
    return health_monitor.get_summary()


@router.get("/health/report/html", response_class=HTMLResponse)
def health_report_html() -> str:
    return health_monitor.render_html()
