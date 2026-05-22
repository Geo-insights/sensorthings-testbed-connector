from fastapi import FastAPI

from app.config import settings
from app.routes.connector import router as connector_router
from app.routes.health import router as health_router
from app.services.sensorthings_client import client

app = FastAPI(
    title=settings.connector_name,
    version="0.1.0",
    description="Climate-adaptation proof of concept for connecting TGV and Blijdorp sensors to a central OGC SensorThings API server.",
)

app.include_router(health_router)
app.include_router(connector_router)


@app.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()
