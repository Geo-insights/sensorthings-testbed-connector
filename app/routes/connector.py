from pathlib import Path

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile

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


# --- Polling source manual triggers ---


@router.post("/ohnics-push")
async def ohnics_push() -> dict:
    """Manually trigger one Ohnics fetch-and-push cycle.

    Registers discovered sensor entities in FROST before pushing observations.
    Deduplicates against the latest observation already in FROST.
    Requires OHNICS_ENABLED=true in .env.
    """
    if not settings.ohnics_enabled:
        raise HTTPException(status_code=503, detail="Ohnics source is disabled. Set OHNICS_ENABLED=true.")

    from app.main import _dedup_readings, _seed_timestamps_from_frost
    from app.services.ohnics_source import OhnicsPollingSource

    source = OhnicsPollingSource()
    readings = await source.fetch_readings()
    if not readings:
        return {"source": "ohnics", "readings": 0, "pushed": 0, "message": "No readings fetched."}

    # Register entity sets for discovered sensors before pushing
    entity_sets = source.entity_sets()
    reg_results = []
    for entity_set in entity_sets:
        reg_results.append(client.register_entity_set(entity_set))

    # Dedup against what's already in FROST
    last_timestamps = _seed_timestamps_from_frost("Ohnics")
    new_readings = _dedup_readings(readings, last_timestamps)

    if not new_readings:
        return {"source": "ohnics", "readings": len(readings), "new": 0, "pushed": 0, "message": "All readings already in FROST."}

    result = client.push_observations(new_readings)
    return {
        "source": "ohnics",
        "readings": len(readings),
        "new": len(new_readings),
        "sensors_registered": len(reg_results),
        "pushed": result.get("total_sent", 0),
        "detail": result,
    }


@router.post("/levellog-push")
async def levellog_push() -> dict:
    """Manually trigger one Levellog fetch-and-push cycle.

    Registers entity sets in FROST before pushing observations.
    Requires LEVELLOG_ENABLED=true and LEVELLOG_INSTALLATION_IDS in .env.
    """
    if not settings.levellog_enabled:
        raise HTTPException(status_code=503, detail="Levellog source is disabled. Set LEVELLOG_ENABLED=true.")

    from app.services.levellog_source import LevellogPollingSource

    source = LevellogPollingSource()
    readings = await source.fetch_readings()
    if not readings:
        return {"source": "levellog", "readings": 0, "pushed": 0, "message": "No readings fetched."}

    entity_sets = source.entity_sets()
    for entity_set in entity_sets:
        client.register_entity_set(entity_set)

    result = client.push_observations(readings)
    return {"source": "levellog", "readings": len(readings), "pushed": result.get("total_sent", 0), "detail": result}


@router.post("/ohnics-cleanup")
def ohnics_cleanup_duplicates() -> dict:
    """Remove duplicate Ohnics entities and observations from FROST.

    For each entity type (Things, Sensors, Locations, Datastreams), finds
    entities with the same name and keeps only the one with the lowest @iot.id.
    Also deduplicates observations on the remaining datastreams.
    Temporary maintenance endpoint.
    """
    import requests as req

    prefix = settings.entity_name_prefix
    base = client._http.primary_base_url
    headers = client._headers()
    result: dict = {}

    # Deduplicate entities: keep lowest @iot.id per name
    for entity_type in ["Datastreams", "Sensors", "Things", "Locations"]:
        resp = req.get(
            f"{base}/{entity_type}",
            params={
                "$filter": f"startswith(name,'{prefix}Ohnics')",
                "$select": "@iot.id,name",
                "$orderby": "@iot.id asc",
                "$top": "1000",
            },
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        entities = resp.json().get("value", [])

        seen_names: dict[str, str] = {}  # name → first @iot.id
        deleted = 0
        for entity in entities:
            name = entity["name"]
            eid = str(entity["@iot.id"])
            if name in seen_names:
                req.delete(f"{base}/{entity_type}({eid})", headers=headers, timeout=30)
                deleted += 1
            else:
                seen_names[name] = eid
        result[entity_type] = {"total": len(entities), "duplicates_deleted": deleted, "kept": len(seen_names)}

    # Deduplicate observations on remaining Ohnics datastreams
    resp = req.get(
        f"{base}/Datastreams",
        params={"$filter": f"startswith(name,'{prefix}Ohnics')", "$select": "@iot.id,name", "$top": "200"},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    obs_deleted = 0
    for ds in resp.json().get("value", []):
        obs_resp = req.get(
            f"{base}/Datastreams({ds['@iot.id']})/Observations",
            params={"$select": "@iot.id,phenomenonTime,result", "$orderby": "@iot.id asc", "$top": "1000"},
            headers=headers, timeout=30,
        )
        obs_resp.raise_for_status()
        seen: set[str] = set()
        for obs in obs_resp.json().get("value", []):
            key = f"{obs['phenomenonTime']}|{obs['result']}"
            if key in seen:
                req.delete(f"{base}/Observations({obs['@iot.id']})", headers=headers, timeout=30)
                obs_deleted += 1
            else:
                seen.add(key)
    result["Observations"] = {"duplicates_deleted": obs_deleted}

    return result


@router.get("/debug-seed")
def debug_seed() -> dict:
    """Debug: test _seed_timestamps_from_frost."""
    import requests as req

    base = client._http.primary_base_url
    headers = client._headers()
    prefix = settings.entity_name_prefix

    # Get first 2 Ohnics datastreams
    resp = req.get(
        f"{base}/Datastreams",
        params={"$filter": f"startswith(name,'{prefix}Ohnics')", "$top": "2"},
        headers=headers, timeout=15,
    )
    datastreams = resp.json().get("value", []) if resp.ok else []

    debug = []
    for ds in datastreams:
        ds_id = ds.get("@iot.id")
        props = ds.get("properties", {})

        obs_resp = req.get(
            f"{base}/Datastreams({ds_id})/Observations",
            params={"$orderby": "phenomenonTime desc", "$top": "1", "$select": "phenomenonTime"},
            headers=headers, timeout=15,
        )
        obs_raw = obs_resp.json() if obs_resp.ok else {"error": obs_resp.status_code}

        debug.append({
            "ds_id": ds_id,
            "ds_name": ds.get("name"),
            "props": props,
            "obs_response": obs_raw,
        })

    # Also run the actual seed function
    from app.main import _seed_timestamps_from_frost
    timestamps = _seed_timestamps_from_frost("Ohnics")

    return {
        "datastream_debug": debug,
        "seed_result_count": len(timestamps),
        "seed_sample": {k: v.isoformat() for k, v in list(timestamps.items())[:4]},
    }


# --- Sensor metadata & image endpoints ---

_ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_SENSOR_IMAGES_DIR = Path("data/sensor_images")


def _find_sensor_config(sensor_id: str) -> tuple[dict, dict] | None:
    """Return (sensor_dict, entity_set) for *sensor_id*, or None."""
    from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS

    for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
        for sensor in entity_set["sensors"]:
            if sensor["sensor_id"] == sensor_id:
                return sensor, entity_set
    return None


@router.get("/sensors/{sensor_id}")
def get_sensor_info(sensor_id: str) -> dict:
    """Return the sensor config including properties and FROST registration id."""
    result = _find_sensor_config(sensor_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown sensor_id: {sensor_id}")
    sensor, entity_set = result
    sensor_name = client._prefixed_name(sensor["name"])
    iot_id = client._registered_entities.get("sensors", {}).get(sensor_name)
    return {
        **sensor,
        "prefixed_name": sensor_name,
        "@iot.id": iot_id,
        "site_key": entity_set["site_key"],
        "thing_name": entity_set["thing"]["name"],
    }


@router.post("/sensors/{sensor_id}/image")
async def upload_sensor_image(
    sensor_id: str,
    file: UploadFile,
    patch_frost: bool = Query(default=False),
) -> dict:
    """Upload an installation photo for a sensor."""
    result = _find_sensor_config(sensor_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown sensor_id: {sensor_id}")
    sensor, _ = result

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{suffix}' not allowed. Use: {', '.join(sorted(_ALLOWED_IMAGE_EXTENSIONS))}",
        )

    contents = await file.read()
    if len(contents) > _MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large (max {_MAX_IMAGE_SIZE // (1024 * 1024)} MB).")

    dest = _SENSOR_IMAGES_DIR / f"{sensor_id}{suffix}"
    dest.write_bytes(contents)

    image_url = f"{settings.public_base_url}/static/sensor-images/{sensor_id}{suffix}"

    frost_patched = False
    if patch_frost:
        existing_props = dict(sensor.get("properties") or {})
        existing_props["image_url"] = image_url
        patch_result = client.patch_sensor_properties(sensor["name"], existing_props)
        frost_patched = patch_result.get("ok", False)

    return {
        "sensor_id": sensor_id,
        "image_url": image_url,
        "frost_patched": frost_patched,
    }
