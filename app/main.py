import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.logging_config import setup_logging
from app.routes.connector import router as connector_router
from app.routes.health import router as health_router
from app.services.sensorthings_client import client

setup_logging(debug=settings.debug)
logger = logging.getLogger("connector.events")


def _kafka_push_cycle(max_messages: int = 500, timeout: float = 5.0) -> dict:
    """Run one Kafka consume → FROST push cycle (blocking)."""
    from app.services.kafka_tgv_consumer import KafkaTGVConsumer
    from app.sources.tgv_kafka_mapping import avro_batch_to_sensor_readings

    with KafkaTGVConsumer() as consumer:
        records = consumer.consume_batch(max_messages=max_messages, timeout=timeout)
    if not records:
        return {"pulled": 0}
    readings = avro_batch_to_sensor_readings(records)
    result = client.push_observations(readings)
    # Forward to monitoring module (fire-and-forget)
    _push_to_monitoring(readings)
    return {"pulled": len(records), "mapped": len(readings), "pushed": result.get("total_sent", 0)}


async def _kafka_ingest_loop():
    """Background loop: consume Kafka and push to FROST every KAFKA_TGV_POLL_SECONDS."""
    poll_seconds = max(10, int(settings.kafka_tgv_poll_seconds))
    logger.info("Kafka ingest loop started (every %ds)", poll_seconds)
    while True:
        try:
            summary = await run_in_threadpool(_kafka_push_cycle)
            if summary.get("pulled", 0) > 0:
                logger.info(
                    "Kafka push: pulled=%s mapped=%s pushed=%s",
                    summary.get("pulled"), summary.get("mapped"), summary.get("pushed"),
                )
        except Exception:
            logger.exception("Kafka ingest cycle failed")
        await asyncio.sleep(poll_seconds)


def _push_to_monitoring(readings: list) -> None:
    """Fire-and-forget push of readings to the monitoring module.

    Runs after observations are successfully pushed to FROST. Failures are
    logged but never block the main ingest loop.
    """
    if not settings.monitoring_push_url or not settings.monitoring_push_key:
        return
    import requests as req

    base_url = client._http.primary_base_url or ""
    payload = {
        "readings": [
            {
                "server_url": base_url,
                "datastream_id": str(
                    client._datastream_ids.get(
                        f"{r.sensor_id}::{r.observed_property}", ""
                    )
                ),
                "value": r.value,
                "timestamp": r.timestamp.isoformat().replace("+00:00", "Z"),
                "quality": getattr(r, "quality", "good") or "good",
            }
            for r in readings
            if client._datastream_ids.get(f"{r.sensor_id}::{r.observed_property}")
        ],
    }
    if not payload["readings"]:
        return
    try:
        resp = req.post(
            f"{settings.monitoring_push_url}/readings/push",
            json=payload,
            headers={"X-Connector-Key": settings.monitoring_push_key},
            timeout=10,
        )
        if resp.ok:
            logger.debug("Monitoring push: %d readings forwarded", len(payload["readings"]))
        else:
            logger.warning("Monitoring push failed: %s %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.warning("Monitoring push failed (connection error)", exc_info=True)


def _dedup_readings(
    readings: list,
    last_timestamps: dict[str, datetime],
) -> list:
    """Filter out readings whose timestamp is <= the last-pushed value."""
    new = []
    for r in readings:
        key = f"{r.sensor_id}::{r.observed_property}"
        last_ts = last_timestamps.get(key)
        if last_ts is None or r.timestamp > last_ts:
            new.append(r)
    return new


def _update_timestamps(
    readings: list,
    last_timestamps: dict[str, datetime],
) -> None:
    """Record the latest pushed timestamp per sensor+property."""
    for r in readings:
        key = f"{r.sensor_id}::{r.observed_property}"
        last_timestamps[key] = r.timestamp


def _seed_timestamps_from_frost(source_name: str) -> dict[str, datetime]:
    """Query FROST for the latest observation per datastream to avoid re-pushing on restart."""
    import requests as req

    base = client._http.primary_base_url
    if not base:
        return {}

    headers = client._headers()
    prefix = settings.entity_name_prefix

    # Step 1: get all datastreams for this source (with properties for key mapping)
    try:
        resp = req.get(
            f"{base}/Datastreams",
            params={
                "$filter": f"startswith(name,'{prefix}{source_name}')",
                "$top": "200",
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception:
        logger.warning("Failed to fetch datastreams from FROST for %s", source_name)
        return {}

    datastreams = resp.json().get("value", [])
    timestamps: dict[str, datetime] = {}

    # Step 2: for each datastream, get the latest observation
    for ds in datastreams:
        props = ds.get("properties", {})
        sensor_id = props.get("sensor_id", "")
        observed_property = props.get("observed_property", "")
        if not sensor_id or not observed_property:
            continue

        ds_id = ds.get("@iot.id") or ds.get("id")
        if not ds_id:
            continue

        try:
            obs_resp = req.get(
                f"{base}/Datastreams({ds_id})/Observations",
                params={"$orderby": "phenomenonTime desc", "$top": "1", "$select": "phenomenonTime"},
                headers=headers,
                timeout=15,
            )
            if not obs_resp.ok:
                continue
            observations = obs_resp.json().get("value", [])
        except Exception:
            continue

        if observations:
            raw_ts = observations[0].get("phenomenonTime", "")
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                key = f"{sensor_id}::{observed_property}"
                # Keep the newest timestamp if multiple datastreams share a key
                if key not in timestamps or ts > timestamps[key]:
                    timestamps[key] = ts
            except (ValueError, TypeError):
                pass

    logger.info("Seeded %d timestamps from FROST for %s", len(timestamps), source_name)
    return timestamps


async def _polling_ingest_loop(source):
    """Background loop for a REST API polling source."""
    from app.services.polling_source import PollingSource

    poll_seconds = source.poll_interval()
    logger.info("Polling loop started for %s (every %ds)", source.source_name, poll_seconds)

    # Eager registration: register entities before the first data fetch so we
    # don't lose readings if the first fetch succeeds but registration would
    # have been the next step.
    entities_registered = False
    entity_sets = source.entity_sets()
    if entity_sets:
        for attempt in range(1, 4):
            try:
                logger.info("%s: eagerly registering %d entity sets in FROST (attempt %d)", source.source_name, len(entity_sets), attempt)
                for entity_set in entity_sets:
                    await run_in_threadpool(client.register_entity_set, entity_set)
                entities_registered = True
                break
            except Exception:
                logger.exception("%s: eager registration attempt %d failed", source.source_name, attempt)
                if attempt < 3:
                    await asyncio.sleep(10)
    else:
        logger.info("%s: no static entity sets; will register on first data fetch", source.source_name)

    # Seed from FROST so we don't re-push observations after a restart
    last_timestamps = await run_in_threadpool(
        _seed_timestamps_from_frost, source.source_name.capitalize()
    )

    while True:
        try:
            readings = await source.fetch_readings()
            if readings:
                # If eager registration failed, retry on data arrival
                if not entities_registered:
                    entity_sets = source.entity_sets()
                    if entity_sets:
                        try:
                            logger.info("%s: retrying registration of %d entity sets", source.source_name, len(entity_sets))
                            for entity_set in entity_sets:
                                await run_in_threadpool(client.register_entity_set, entity_set)
                            entities_registered = True
                            if not last_timestamps:
                                last_timestamps = await run_in_threadpool(
                                    _seed_timestamps_from_frost, source.source_name.capitalize()
                                )
                        except Exception:
                            logger.exception("%s: registration retry failed; pushing with cached IDs", source.source_name)

                # Push readings regardless — if datastream IDs are cached from
                # a previous run, observations can be pushed even before
                # registration completes.
                new_readings = _dedup_readings(readings, last_timestamps)
                if new_readings:
                    result = await run_in_threadpool(client.push_observations, new_readings)
                    _update_timestamps(new_readings, last_timestamps)
                    logger.info(
                        "%s push: readings=%d new=%d pushed=%s",
                        source.source_name,
                        len(readings),
                        len(new_readings),
                        result.get("total_sent", 0),
                    )
                    # Forward to monitoring module (fire-and-forget)
                    await run_in_threadpool(_push_to_monitoring, new_readings)
                else:
                    logger.debug("%s: all %d readings already pushed, skipping", source.source_name, len(readings))
        except Exception:
            logger.exception("%s polling cycle failed", source.source_name)
        await asyncio.sleep(poll_seconds)


def _get_enabled_polling_sources():
    """Instantiate and return all enabled polling sources."""
    sources = []

    from app.services.ohnics_source import OhnicsPollingSource
    ohnics = OhnicsPollingSource()
    if ohnics.is_enabled():
        sources.append(ohnics)

    from app.services.levellog_source import LevellogPollingSource
    levellog = LevellogPollingSource()
    if levellog.is_enabled():
        sources.append(levellog)

    return sources


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    if settings.kafka_tgv_enabled:
        tasks.append(asyncio.create_task(_kafka_ingest_loop()))

    for source in _get_enabled_polling_sources():
        tasks.append(asyncio.create_task(_polling_ingest_loop(source)))

    yield
    for t in tasks:
        t.cancel()


app = FastAPI(
    title=settings.connector_name,
    version="0.1.0",
    description="Climate-adaptation proof of concept for connecting TGV and Blijdorp sensors to a central OGC SensorThings API server.",
    lifespan=lifespan,
)

SENSOR_IMAGES_DIR = Path("data/sensor_images")
SENSOR_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/sensor-images", StaticFiles(directory=str(SENSOR_IMAGES_DIR)), name="sensor-images")

app.include_router(health_router)
app.include_router(connector_router)


@app.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()


@app.get("/frost/capabilities")
def frost_capabilities() -> dict:
    return client.check_capabilities()
