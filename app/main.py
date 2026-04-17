from fastapi import FastAPI

from app.config import settings
from app.routes.connector import router as connector_router
from app.routes.health import router as health_router

app = FastAPI(
    title=settings.connector_name,
    version="0.1.0",
    description="Tender-focused proof of concept for connecting sensors to a central OGC SensorThings API server.",
)

app.include_router(health_router)
app.include_router(connector_router)
