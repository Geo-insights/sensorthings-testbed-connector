from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query

from app.config import settings
from app.models import IngestRequest, TaskingTaskCreateRequest, TaskingTaskQuery
from app.services.kafka_tgv_consumer import KafkaTGVConsumer
from app.sources.bridge_source import load_bridge_readings
from app.sources.tgv_kafka_mapping import avro_batch_to_sensor_readings
from app.services.monitoring_mqtt_bridge import (
    build_monitoring_mqtt_preview,
    publish_monitoring_mqtt,
)
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


@router.get("/monitoring-mqtt-preview")
def monitoring_mqtt_preview() -> dict:
    readings = load_bridge_readings()
    return build_monitoring_mqtt_preview(readings).model_dump(mode="json")


@router.post("/monitoring-mqtt-preview")
def monitoring_mqtt_preview_custom(request: IngestRequest) -> dict:
    return build_monitoring_mqtt_preview(request.readings).model_dump(mode="json")


@router.post("/monitoring-mqtt-push")
def monitoring_mqtt_push(request: IngestRequest | None = None) -> dict:
    readings = request.readings if request else load_bridge_readings()
    return publish_monitoring_mqtt(readings)


@router.post("/replay-failed")
def replay_failed_observations() -> dict:
    return client.replay_failed_observations()


@router.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()


@router.get("/frost/capabilities")
def frost_capabilities() -> dict:
    return client.check_capabilities()


@router.post("/tasking/register-site/{site_key}")
def register_site_tasking(site_key: str) -> dict:
    result = client.register_site_tasking(site_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown site key: {site_key}")
    return result


@router.post("/tasking/tasks")
def create_task(request: TaskingTaskCreateRequest) -> dict:
    return client.create_task(request)


@router.get("/tasking/tasks")
def list_tasks(
    site_key: str = Query(..., pattern="^(tgv|blijdorp)$"),
    capability_key: str | None = None,
    top: int = Query(20, ge=1, le=200),
) -> dict:
    query = TaskingTaskQuery(site_key=site_key, capability_key=capability_key, top=top)
    result = client.list_tasks(query)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown site key: {site_key}")
    return result


@router.post("/kafka-push")
def kafka_push(
    max_messages: int = Query(default=1000, ge=1, le=10000),
    timeout: float = Query(default=5.0, ge=0.5, le=60.0),
) -> dict:
    """
    Pull a batch from Confluent Cloud Kafka (TGV officelab-climate topic),
    map Avro records to SensorReadings, and push observations to FROST.

    Returns a summary with pulled/mapped/pushed/failed counts.
    Requires KAFKA_TGV_ENABLED=true and all KAFKA_TGV_* credentials in .env.
    """
    if not settings.kafka_tgv_enabled:
        raise HTTPException(
            status_code=503,
            detail="Kafka TGV source is disabled. Set KAFKA_TGV_ENABLED=true.",
        )

    consumer = KafkaTGVConsumer()
    try:
        consumer.connect()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Kafka configuration error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Kafka connection failed: {exc}") from exc

    try:
        raw_records = consumer.consume_batch(max_messages=max_messages, timeout=timeout)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kafka consume error: {exc}") from exc
    finally:
        consumer.close()

    pulled = len(raw_records)
    readings = avro_batch_to_sensor_readings(raw_records)
    mapped = len(readings)

    if not readings:
        return {
            "pulled": pulled,
            "mapped": 0,
            "pushed": 0,
            "failed": 0,
            "message": "No mappable readings in batch.",
        }

    push_result = client.push_observations(readings)

    results_list: list[dict] = push_result.get("results", [])
    if not results_list and "targets" in push_result:
        for target in push_result["targets"]:
            results_list.extend(target.get("results", []))

    pushed = sum(1 for r in results_list if r.get("ok"))
    failed = sum(1 for r in results_list if not r.get("ok"))

    return {
        "pulled": pulled,
        "mapped": mapped,
        "pushed": pushed,
        "failed": failed,
        "detail": push_result,
    }
