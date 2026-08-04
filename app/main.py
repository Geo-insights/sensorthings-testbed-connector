import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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

# Set by POST /connector/kafka-skip-to-latest. The persistent Kafka consumer
# checks this at the top of each cycle: when set, it seeks all partitions to
# their high watermark and commits, dropping the backlog and going live.
_kafka_skip_to_latest = threading.Event()


def _consume_and_push_once(consumer, max_messages: int = 500, timeout: float = 5.0) -> dict:
    """Consume one batch from an already-connected persistent consumer, push to
    FROST, and commit. Blocking; call via run_in_threadpool."""
    from app.sources.tgv_kafka_mapping import avro_batch_to_sensor_readings

    records = consumer.consume_batch(max_messages=max_messages, timeout=timeout)
    if not records:
        return {"pulled": 0}
    readings = avro_batch_to_sensor_readings(records)
    # Forward to the monitoring module FIRST, before the (potentially slow,
    # multi-target) FROST push below, so downstream freshness is not gated by
    # FROST push latency. Fire-and-forget + idempotent: if the FROST push fails
    # we do not commit and simply re-forward next cycle (monitoring dedups via
    # ON CONFLICT DO NOTHING).
    _push_to_monitoring(readings)
    result = client.push_observations(readings)
    # Commit only after push completes — failed observations are safely in the
    # DLQ, so re-consuming them would just create duplicates.
    consumer.commit()
    # How far behind real time is the newest observation we just pushed? A large
    # value means we're replaying a stale backlog (e.g. after a long stall), so
    # downstream consumers see an old "latest" timestamp.
    newest = max((r.timestamp for r in readings if r.timestamp is not None), default=None)
    lag_seconds = (datetime.now(UTC) - newest).total_seconds() if newest is not None else None
    return {
        "pulled": len(records),
        "mapped": len(readings),
        "pushed": result.get("total_sent", 0),
        "lag_seconds": lag_seconds,
    }


def _kafka_stale_threshold(poll_seconds: int) -> int:
    """Seconds of no Kafka data after which the source is considered stalled."""
    if settings.kafka_stale_seconds > 0:
        return settings.kafka_stale_seconds
    return poll_seconds + max(0, settings.freshness_grace_seconds)


async def _kafka_ingest_loop():
    """Background loop with a single persistent Kafka consumer.

    One long-lived consumer stays subscribed across cycles and is polled
    frequently (well within ``max.poll.interval.ms``) to keep group membership
    stable. This removes the per-cycle join/rebalance churn of the old
    create/subscribe/close-every-300s design — that pattern could exceed the
    max poll interval, get the consumer evicted from the group, and drop data.

    Self-healing: on any cycle error the consumer is torn down and rebuilt on
    the next iteration (auto-reconnect, with capped backoff). A stall watchdog
    fires a critical alert and forces a reconnect when no data arrives past the
    stale threshold, and a distinct alert fires on Kafka auth/authorization
    failures so a rotated Confluent key surfaces immediately instead of as a
    silent multi-day flatline.
    """
    from app.services.alerting import send_alert
    from app.services.health_monitor import health_monitor
    from app.services.kafka_tgv_consumer import KafkaTGVConsumer

    # Poll frequently regardless of the configured cycle interval so the
    # consumer keeps sending heartbeats and stays in the group.
    poll_seconds = max(5, min(30, int(settings.kafka_tgv_poll_seconds)))
    stale_threshold = _kafka_stale_threshold(max(10, int(settings.kafka_tgv_poll_seconds)))
    logger.info(
        "Kafka ingest loop started (persistent consumer, poll every %ds, stall threshold %ds)",
        poll_seconds, stale_threshold,
    )

    def _drop_consumer(c) -> None:
        try:
            c.close()
        except Exception:
            pass

    consumer = None
    consecutive_failures = 0
    last_reconnect_at = 0.0

    while True:
        try:
            if consumer is None:
                consumer = KafkaTGVConsumer()
                await run_in_threadpool(consumer.connect)
                last_reconnect_at = time.monotonic()
                logger.info("Kafka persistent consumer connected")
            # One-shot skip-to-latest: drop the backlog and go live from the
            # topic head. Requested via POST /connector/kafka-skip-to-latest.
            if _kafka_skip_to_latest.is_set():
                advanced = await run_in_threadpool(consumer.seek_to_end_and_commit)
                _kafka_skip_to_latest.clear()
                logger.warning("Kafka skip-to-latest: advanced %d partition(s) to head", advanced)
                send_alert(
                    "kafka.skip_to_latest",
                    f"Skipped Kafka group to latest on {advanced} partition(s); backlog dropped, now live.",
                    level="info",
                    context={"partitions": advanced, "group": settings.kafka_tgv_consumer_group},
                    force=True,
                )
            summary = await run_in_threadpool(
                _consume_and_push_once, consumer, settings.kafka_tgv_batch_max_messages
            )
            consecutive_failures = 0
            if summary.get("pulled", 0) > 0:
                health_monitor.record_source_success("kafka")
                lag_seconds = summary.get("lag_seconds")
                logger.info(
                    "Kafka push: pulled=%s mapped=%s pushed=%s lag=%ss",
                    summary.get("pulled"), summary.get("mapped"), summary.get("pushed"),
                    int(lag_seconds) if lag_seconds is not None else "?",
                )
                # Backlog-staleness guard: if the newest observation we just
                # pushed is far behind real time, we're replaying a stale
                # backlog (downstream sees an old "latest"). Optionally auto-skip
                # to the topic head to go live; otherwise alert a human.
                if (
                    lag_seconds is not None
                    and settings.kafka_max_lag_seconds > 0
                    and lag_seconds > settings.kafka_max_lag_seconds
                    and not _kafka_skip_to_latest.is_set()
                ):
                    lag_ctx = {
                        "lag_seconds": int(lag_seconds),
                        "threshold_seconds": settings.kafka_max_lag_seconds,
                    }
                    if settings.kafka_auto_skip_on_lag:
                        _kafka_skip_to_latest.set()
                        send_alert(
                            "kafka.lag",
                            f"Kafka observation lag {int(lag_seconds)}s exceeds "
                            f"{settings.kafka_max_lag_seconds}s \u2014 auto-skipping backlog to go live.",
                            level="warning",
                            context=lag_ctx,
                            dedup_key="kafka.lag",
                        )
                    else:
                        send_alert(
                            "kafka.lag",
                            f"Kafka observation lag {int(lag_seconds)}s exceeds "
                            f"{settings.kafka_max_lag_seconds}s \u2014 replaying stale backlog. "
                            "Consider POST /connector/kafka-skip-to-latest.",
                            level="warning",
                            context=lag_ctx,
                            dedup_key="kafka.lag",
                        )
        except Exception as exc:
            consecutive_failures += 1
            logger.exception("Kafka ingest cycle failed (failure #%d)", consecutive_failures)
            health_monitor.record_source_error("kafka", f"{type(exc).__name__}: {exc}")
            if _is_kafka_auth_error(exc):
                send_alert(
                    "kafka.auth_failure",
                    "Kafka authentication/authorization failed — check the Confluent "
                    "API key/secret (likely rotated or revoked).",
                    level="critical",
                    context={"error": f"{type(exc).__name__}: {exc}", "group": settings.kafka_tgv_consumer_group},
                    dedup_key="kafka.auth_failure",
                )
            # Auto-reconnect: tear down; the next iteration rebuilds the consumer.
            if consumer is not None:
                await run_in_threadpool(_drop_consumer, consumer)
                consumer = None
            # Capped backoff that grows with consecutive failures.
            await asyncio.sleep(min(60, poll_seconds * consecutive_failures))
            continue

        # Stall watchdog — fires when cycles "succeed" but return 0 records, AND
        # (critically) when the consumer has connected but never delivered a
        # single message since the last (re)connect. In that case
        # ``source_age_seconds`` is None, so the old ``age is not None`` guard
        # skipped the watchdog entirely: a consumer that stalls (e.g. a
        # rebalance stall) before its first push would stay wedged forever. Fall
        # back to "seconds since the consumer last connected" so a never-delivered
        # stall is still detected and reconnected.
        now = time.monotonic()
        age = health_monitor.source_age_seconds("kafka")
        stalled_for = age if age is not None else (now - last_reconnect_at)
        if stalled_for > stale_threshold:
            send_alert(
                "kafka.stall",
                f"No Kafka observations for {int(stalled_for)}s (threshold {stale_threshold}s). "
                "Reconnecting consumer; check upstream producer and Confluent topic.",
                level="critical",
                context={
                    "age_seconds": round(stalled_for, 1),
                    "threshold_seconds": stale_threshold,
                    "ever_delivered": age is not None,
                },
                dedup_key="kafka.stall",
            )
            # Force a reconnect at most once per stale window (independent of
            # whether the alert webhook is configured), so a genuinely-idle
            # upstream doesn't cause a reconnect storm every cycle.
            if (now - last_reconnect_at) > stale_threshold and consumer is not None:
                logger.warning(
                    "Kafka stall detected (%ds, ever_delivered=%s) — reconnecting consumer",
                    int(stalled_for), age is not None,
                )
                await run_in_threadpool(_drop_consumer, consumer)
                consumer = None
                last_reconnect_at = now

        await asyncio.sleep(poll_seconds)


def _is_kafka_auth_error(exc: BaseException) -> bool:
    """Heuristic: does this exception look like a Kafka auth/authorization failure?"""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("authentication", "authorization", "sasl", "_authentication", "not authorized", "unauthorized")
    )


def _resolve_monitoring_datastream_id(reading) -> str | None:
    """Resolve a reading's FROST datastream id for the primary base URL.

    Mirrors the resolution chain used by ``client.push_observations`` so the
    monitoring push and the FROST push agree on the same id:
      sensor_id::observed_property  →  stream_key  →  sensor_id.

    TGV Kafka readings map to pre-existing datastreams by ``stream_key`` (via
    ``SENSORTHINGS_DATASTREAM_IDS_JSON``), so the sensor_id::op key alone is not
    enough — the earlier implementation dropped every TGV reading here.

    When the static id map has no entry (e.g. Kafka datastreams that are only
    resolved live against FROST during the push), fall back to the same live
    OData lookup ``client.push_observations`` uses, so the monitoring forward
    agrees with the datastream the observation was actually pushed to.
    """
    ids = client._datastream_ids or dict(settings.datastream_ids)
    ds_id = (
        ids.get(client._datastream_key(reading.sensor_id, reading.observed_property))
        or ids.get(getattr(reading, "stream_key", "") or "")
        or ids.get(reading.sensor_id)
    )
    if ds_id:
        return ds_id

    base_url = client._http.primary_base_url or ""
    if base_url:
        try:
            return client._resolve_datastream_id_live(base_url, reading)
        except Exception:
            logger.debug("Live datastream resolve failed for monitoring push", exc_info=True)
    return None


def _push_to_monitoring(readings: list) -> None:
    """Fire-and-forget push of readings to the monitoring module.

    Runs after observations are successfully pushed to FROST. Failures are
    logged but never block the main ingest loop.
    """
    if not settings.monitoring_push_url or not settings.monitoring_push_key:
        return
    import requests as req

    base_url = client._http.primary_base_url or ""
    forwarded = []
    for r in readings:
        ds_id = _resolve_monitoring_datastream_id(r)
        if not ds_id:
            continue
        forwarded.append(
            {
                "server_url": base_url,
                "datastream_id": str(ds_id),
                "value": r.value,
                "timestamp": r.timestamp.isoformat().replace("+00:00", "Z"),
                "quality": getattr(r, "quality", "good") or "good",
            }
        )
    payload = {"readings": forwarded}
    if not payload["readings"]:
        return
    # The monitoring /readings/push endpoint caps each request at 2000 readings.
    # A single Kafka cycle can map to more than that, which previously returned
    # 422 and dropped the WHOLE cycle's data. Chunk so every reading is
    # delivered regardless of batch size.
    chunk_size = 1000
    forwarded_count = 0
    for start in range(0, len(forwarded), chunk_size):
        chunk = forwarded[start : start + chunk_size]
        try:
            resp = req.post(
                f"{settings.monitoring_push_url}/readings/push",
                json={"readings": chunk},
                headers={"X-Connector-Key": settings.monitoring_push_key},
                timeout=10,
            )
            if resp.ok:
                forwarded_count += len(chunk)
            else:
                logger.warning("Monitoring push failed: %s %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.warning("Monitoring push failed (connection error)", exc_info=True)
    if forwarded_count:
        logger.debug("Monitoring push: %d readings forwarded", forwarded_count)


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
                    from app.services.health_monitor import health_monitor
                    health_monitor.record_source_success(source.source_name.lower())
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
        except Exception as exc:
            logger.exception("%s polling cycle failed", source.source_name)
            from app.services.health_monitor import health_monitor
            health_monitor.record_source_error(source.source_name.lower(), f"{type(exc).__name__}: {exc}")
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


async def _failed_replay_loop():
    """Periodically replay dead-lettered observations back to FROST targets."""
    interval = max(60, settings.failed_replay_interval_seconds)
    logger.info("DLQ replay loop started (every %ds, max %d/pass)", interval, settings.failed_replay_max_lines)
    while True:
        await asyncio.sleep(interval)
        try:
            summary = await run_in_threadpool(
                client.replay_failed_observations, settings.failed_replay_max_lines
            )
            replayed = summary.get("replayed", 0)
            remaining = summary.get("remaining", 0)
            dropped = summary.get("dropped", 0)
            if replayed or remaining or dropped:
                logger.info("DLQ replay: %s replayed, %s dropped, %s remaining", replayed, dropped, remaining)
        except Exception:
            logger.exception("DLQ replay pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    if settings.kafka_tgv_enabled:
        tasks.append(asyncio.create_task(_kafka_ingest_loop()))

    for source in _get_enabled_polling_sources():
        tasks.append(asyncio.create_task(_polling_ingest_loop(source)))

    if settings.failed_replay_enabled and (settings.sensorthings_base_url or settings.sensorthings_base_urls):
        tasks.append(asyncio.create_task(_failed_replay_loop()))

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
