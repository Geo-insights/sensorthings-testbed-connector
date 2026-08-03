from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
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


def _looks_like_auth_error(exc: BaseException) -> bool:
    """Heuristic: does this Kafka exception look like an auth/authorization failure?"""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("authentication", "authorization", "sasl", "not authorized", "unauthorized")
    )


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
def replay_failed_observations(max_lines: int | None = None) -> dict:
    return client.replay_failed_observations(max_lines)


@router.get("/push-health")
def push_health() -> dict:
    """Circuit-breaker state per FROST target."""
    from app.services.sensorthings_client import observation_breaker

    return {"targets": observation_breaker.snapshot()}


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
    site_key: Literal["tgv", "blijdorp"] = Query(...),
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


@router.get("/kafka-diag")
def kafka_diagnostics() -> dict:
    """Metadata-only Kafka health probe (no consume, no commit, no rebalance).

    Reports, per partition of the TGV topic:
      * ``high`` watermark  — the latest produced offset (is the topic producing?)
      * ``committed``       — our consumer group's committed offset
      * ``lag``             — high - committed (unconsumed backlog)

    This distinguishes *upstream-producer-stopped* (high frozen, lag 0) from
    *connector-consumer-stalled* (lag > 0 and growing) without needing the
    Confluent console. Safe: it only issues metadata/offset requests using the
    group already permitted by the API-key ACL.
    """
    if not settings.kafka_tgv_enabled:
        raise HTTPException(
            status_code=503,
            detail="Kafka TGV source is disabled. Set KAFKA_TGV_ENABLED=true.",
        )

    from confluent_kafka import Consumer, KafkaException, TopicPartition

    topic = settings.kafka_tgv_topic
    consumer = Consumer({
        "group.id": settings.kafka_tgv_consumer_group,
        "client.id": f"{settings.kafka_tgv_client_id}-diag",
        "bootstrap.servers": settings.kafka_tgv_bootstrap_servers,
        "sasl.mechanisms": "PLAIN",
        "security.protocol": "SASL_SSL",
        "sasl.username": settings.kafka_tgv_api_key,
        "sasl.password": settings.kafka_tgv_api_password,
        "enable.auto.commit": "false",
    })

    try:
        try:
            md = consumer.list_topics(topic, timeout=10.0)
        except KafkaException as exc:
            return {
                "connect_ok": False,
                "auth_error": _looks_like_auth_error(exc),
                "error": str(exc),
                "group": settings.kafka_tgv_consumer_group,
                "topic": topic,
                "guidance": (
                    "Kafka metadata request failed. If auth_error is true the "
                    "Confluent API key/secret is likely rotated or revoked."
                ),
            }

        topic_md = md.topics.get(topic)
        if topic_md is None or topic_md.error is not None:
            return {
                "connect_ok": True,
                "topic": topic,
                "error": f"Topic not found or error: {getattr(topic_md, 'error', 'missing')}",
            }

        partitions: list[dict] = []
        total_high = 0
        total_lag = 0
        tps = [TopicPartition(topic, p) for p in topic_md.partitions.keys()]
        committed = {tp.partition: tp for tp in consumer.committed(tps, timeout=10.0)}

        for p in sorted(topic_md.partitions.keys()):
            tp = TopicPartition(topic, p)
            low, high = consumer.get_watermark_offsets(tp, timeout=10.0, cached=False)
            comm = committed.get(p)
            comm_offset = comm.offset if comm is not None else -1001
            has_commit = comm_offset is not None and comm_offset >= 0
            lag = (high - comm_offset) if has_commit else None
            total_high += high
            if lag is not None:
                total_lag += lag
            partitions.append({
                "partition": p,
                "low": low,
                "high": high,
                "committed": comm_offset if has_commit else None,
                "lag": lag,
            })

        if total_lag > 0:
            guidance = (
                "Unconsumed backlog present (lag > 0): the producer is/was active "
                "but the connector consumer is behind or stalled \u2014 investigate the "
                "ingest loop / consumer, not the upstream producer."
            )
        else:
            guidance = (
                "No backlog (lag 0). Call this endpoint again in ~30\u201360s: if the "
                "'high' watermark is unchanged the upstream producer is idle/stopped; "
                "if 'high' increased the pipeline is healthy and caught up."
            )

        return {
            "connect_ok": True,
            "auth_error": False,
            "topic": topic,
            "group": settings.kafka_tgv_consumer_group,
            "partition_count": len(partitions),
            "total_high_watermark": total_high,
            "total_lag": total_lag,
            "partitions": partitions,
            "guidance": guidance,
        }
    finally:
        try:
            consumer.close()
        except Exception:
            pass


@router.get("/freshness")
def freshness(response: Response) -> dict:
    """Per-source liveness for external uptime monitoring.

    Returns each enabled source's age since its last successful push and a
    ``stale`` flag against its expected interval. Responds HTTP 503 when any
    source is stale so a pull-based uptime monitor (or the monitoring module)
    catches stalls even if the outbound alert webhook can't fire (e.g. the
    process is degraded).
    """
    from app.main import _kafka_stale_threshold
    from app.services.health_monitor import health_monitor

    grace = max(0, settings.freshness_grace_seconds)
    thresholds: dict[str, float] = {}
    if settings.kafka_tgv_enabled:
        thresholds["kafka"] = _kafka_stale_threshold(max(10, int(settings.kafka_tgv_poll_seconds)))
    if settings.ohnics_enabled:
        thresholds["ohnics"] = settings.ohnics_poll_seconds + grace
    if settings.levellog_enabled:
        thresholds["levellog"] = settings.levellog_poll_seconds + grace

    result = health_monitor.source_freshness(thresholds)
    if result["any_stale"]:
        response.status_code = 503
    return result


# --- Diagnostics (temporary) ---


@router.get("/ohnics-diag")
def ohnics_diagnostics() -> dict:
    """Diagnostic: count Ohnics datastreams, check DELETE capability, inspect seed."""
    import requests as req
    from app.main import _seed_timestamps_from_frost

    base = client._http.primary_base_url
    headers = client._headers()
    prefix = settings.entity_name_prefix

    # 1. Count ALL Ohnics datastreams (paginate)
    all_ds: list[dict] = []
    url = f"{base}/Datastreams"
    params = {
        "$filter": f"startswith(name,'{prefix}Ohnics')",
        "$select": "@iot.id,name",
        "$top": "1000",
        "$orderby": "@iot.id asc",
    }
    try:
        resp = req.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        all_ds = resp.json().get("value", [])
    except Exception as exc:
        return {"error": f"Failed to list datastreams: {exc}"}

    # Group by name to find duplicates
    by_name: dict[str, list] = {}
    for ds in all_ds:
        name = ds["name"]
        by_name.setdefault(name, []).append(ds["@iot.id"])
    duplicates = {name: ids for name, ids in by_name.items() if len(ids) > 1}
    duplicate_ids = [iot_id for ids in duplicates.values() for iot_id in ids[1:]]  # keep first

    # 2. Try DELETE on one duplicate observation (if any)
    delete_test = None
    if duplicate_ids:
        test_ds_id = duplicate_ids[0]
        # Find an observation on this duplicate datastream
        try:
            obs_resp = req.get(
                f"{base}/Datastreams({test_ds_id})/Observations",
                params={"$top": "1", "$select": "@iot.id"},
                headers=headers, timeout=15,
            )
            obs = obs_resp.json().get("value", [])
            if obs:
                obs_id = obs[0]["@iot.id"]
                del_resp = req.delete(f"{base}/Observations({obs_id})", headers=headers, timeout=15)
                delete_test = {
                    "observation_id": obs_id,
                    "status_code": del_resp.status_code,
                    "response": del_resp.text[:500],
                }
            else:
                # Try deleting the empty duplicate datastream itself
                del_resp = req.delete(f"{base}/Datastreams({test_ds_id})", headers=headers, timeout=15)
                delete_test = {
                    "datastream_id": test_ds_id,
                    "status_code": del_resp.status_code,
                    "response": del_resp.text[:500],
                }
        except Exception as exc:
            delete_test = {"error": str(exc)}

    # 3. Seed timestamps
    seed = _seed_timestamps_from_frost("Ohnics")
    sample_seeds = {k: v.isoformat() for k, v in list(seed.items())[:5]}

    return {
        "total_datastreams": len(all_ds),
        "unique_names": len(by_name),
        "duplicate_names": len(duplicates),
        "duplicate_ds_ids_to_remove": duplicate_ids[:20],
        "delete_test": delete_test,
        "seed_count": len(seed),
        "seed_sample": sample_seeds,
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
