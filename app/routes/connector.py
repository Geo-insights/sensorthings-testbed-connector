from fastapi import APIRouter
from fastapi import HTTPException

from app.models import IngestRequest
from app.sources.bridge_source import load_bridge_readings
from app.services.sensorthings_client import client

router = APIRouter(prefix="/connector", tags=["connector"])


@router.get("/preview")
def preview_payloads() -> dict:
    readings = load_bridge_readings()
    return client.build_preview(readings).model_dump(mode="json")


@router.get("/registration-preview")
def preview_registration() -> dict:
    return client.build_registration_preview().model_dump(mode="json")


@router.get("/registration-preview/{site_key}")
def preview_site_registration(site_key: str) -> dict:
    preview = client.build_site_registration_preview(site_key)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"Unknown site key: {site_key}")
    return preview.model_dump(mode="json")


@router.post("/register-demo")
def register_demo_entities() -> dict:
    return client.register_demo_entities()


@router.post("/register-site/{site_key}")
def register_site_entities(site_key: str) -> dict:
    result = client.register_site_entities(site_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown site key: {site_key}")
    return result


@router.post("/ingest-preview")
def ingest_preview(request: IngestRequest) -> dict:
    return client.build_preview(request.readings).model_dump(mode="json")


@router.post("/ingest")
def ingest_readings(request: IngestRequest) -> dict:
    return client.push_observations(request.readings)


@router.post("/push")
def push_demo_observations() -> dict:
    readings = load_bridge_readings()
    return client.push_observations(readings)


@router.post("/replay-failed")
def replay_failed_observations() -> dict:
    return client.replay_failed_observations()


@router.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()
