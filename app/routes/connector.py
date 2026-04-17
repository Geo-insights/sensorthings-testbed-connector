from fastapi import APIRouter

from app.models import IngestRequest
from app.services.mock_source import generate_demo_readings
from app.services.sensorthings_client import client

router = APIRouter(prefix="/connector", tags=["connector"])


@router.get("/preview")
def preview_payloads() -> dict:
    readings = generate_demo_readings()
    return client.build_preview(readings).model_dump(mode="json")


@router.get("/registration-preview")
def preview_registration() -> dict:
    readings = generate_demo_readings()
    return client.build_registration_preview(readings).model_dump(mode="json")


@router.post("/register-demo")
def register_demo_entities() -> dict:
    readings = generate_demo_readings()
    return client.register_demo_entities(readings)


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
