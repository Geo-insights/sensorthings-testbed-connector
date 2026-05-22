from fastapi import APIRouter

from app.models import IngestRequest
from app.sources.climate_adaptation import generate_demo_readings
from app.services.sensorthings_client import client

router = APIRouter(prefix="/connector", tags=["connector"])


@router.get("/preview")
def preview_payloads() -> dict:
    readings = generate_demo_readings()
    return client.build_preview(readings).model_dump(mode="json")


@router.get("/registration-preview")
def preview_registration() -> dict:
    return client.build_registration_preview().model_dump(mode="json")


@router.post("/register-demo")
def register_demo_entities() -> dict:
    return client.register_demo_entities()


@router.post("/ingest-preview")
def ingest_preview(request: IngestRequest) -> dict:
    return client.build_preview(request.readings).model_dump(mode="json")


@router.post("/ingest")
def ingest_readings(request: IngestRequest) -> dict:
    return client.push_observations(request.readings)


@router.post("/push")
def push_demo_observations() -> dict:
    readings = generate_demo_readings()
    return client.push_observations(readings)


@router.post("/replay-failed")
def replay_failed_observations() -> dict:
    return client.replay_failed_observations()


@router.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()
