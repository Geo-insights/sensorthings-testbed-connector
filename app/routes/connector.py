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

import logging

logger = logging.getLogger("connector.events")

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


@router.post("/kafka-skip-to-latest")
def kafka_skip_to_latest() -> dict:
    """Signal the running Kafka consumer to drop its backlog and go live.

    Sets a one-shot flag the persistent consumer checks at the top of its next
    cycle: it seeks every assigned partition to its high watermark and commits,
    so the group resumes from the topic head instead of replaying the backlog.
    Use after a long stall when the old data is not worth backfilling.

    Returns immediately; the skip happens on the consumer's next poll (within
    one ``poll_seconds`` window). Confirm with ``GET /connector/kafka-diag``
    (``total_lag`` should drop to ~0).
    """
    if not settings.kafka_tgv_enabled:
        return {"requested": False, "reason": "kafka_tgv_enabled is false — no consumer is running."}
    from app.main import _kafka_skip_to_latest

    _kafka_skip_to_latest.set()
    logger.warning("kafka-skip-to-latest requested via API — consumer will drop backlog on next cycle")
    return {
        "requested": True,
        "group": settings.kafka_tgv_consumer_group,
        "note": "Backlog will be dropped on the consumer's next poll. Verify with GET /connector/kafka-diag (total_lag -> ~0).",
    }


@router.post("/collaborall-write-test")
def collaborall_write_test(
    datastream_id: int | None = Query(
        default=None, description="Override the target datastream; auto-picks a GEO_ stream when omitted."
    ),
) -> dict:
    """Decisive write test against the CollaborAll FROST target (fix-or-drop).

    Posts ONE diagnostic observation to a GEO_ datastream on CollaborAll using
    the exact payload coercions the push path uses (``resultQuality`` as an
    array, ``@iot.id`` as an int), then reads the datastream's observation count
    before/after to classify the outcome:

      * ``landed``           — accepted (2xx) and the count increased.
      * ``accepted_no_persist`` — accepted (2xx) but the count did not change
        (server swallows the write; explains "0 things ever populated").
      * ``rejected``         — non-2xx; ``response_body`` has the server reason.

    This is the signal for the keep-or-drop decision on CollaborAll.
    """
    import requests as req
    from datetime import datetime, timezone

    from app.services.sensorthings_client import _as_quality_list, _coerce_iot_id

    # 1. Locate the CollaborAll target.
    target = next(
        (t for t in settings.frost_targets if "collaborall" in t.url.lower()),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="No CollaborAll target configured.")

    base = target.url.rstrip("/")
    headers = {**client._headers_for_url(base), "Content-Type": "application/json"}
    prefix = settings.entity_name_prefix

    def _count(ds_id: int) -> int | None:
        try:
            resp = req.get(
                f"{base}/Datastreams({ds_id})/Observations",
                params={"$count": "true", "$top": "0"},
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 400:  # some servers reject $top=0
                resp = req.get(
                    f"{base}/Datastreams({ds_id})/Observations",
                    params={"$count": "true", "$top": "1"},
                    headers=headers,
                    timeout=30,
                )
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict) and "@iot.count" in body:
                return int(body["@iot.count"])
            return len(body.get("value", [])) if isinstance(body, dict) else None
        except Exception:
            return None

    # 2. Resolve a datastream to write to.
    if datastream_id is None:
        try:
            resp = req.get(
                f"{base}/Datastreams",
                params={
                    "$filter": f"startswith(name,'{prefix}')",
                    "$select": "@iot.id,name",
                    "$top": "1",
                },
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            streams = resp.json().get("value", [])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to list CollaborAll datastreams: {exc}") from exc
        if not streams:
            return {
                "target": base,
                "verdict": "no_geo_datastreams",
                "detail": f"No datastreams starting with '{prefix}' exist on CollaborAll.",
            }
        datastream_id = streams[0]["@iot.id"]
        datastream_name = streams[0].get("name")
    else:
        datastream_name = None

    ds_id_int = _coerce_iot_id(datastream_id)
    count_before = _count(ds_id_int)

    # 3. Build the observation with the same coercions the push path applies.
    now = datetime.now(timezone.utc)
    phenom = now.isoformat().replace("+00:00", "Z")
    payload = {
        "phenomenonTime": phenom,
        "resultTime": phenom,
        "result": 0.0,
        "resultQuality": _as_quality_list("diagnostic"),
        "parameters": {"diagnostic": True, "source": "connector-write-test"},
        "Datastream": {"@iot.id": ds_id_int},
    }

    # 4. POST it and capture the raw server response.
    try:
        post_resp = req.post(f"{base}/Observations", json=payload, headers=headers, timeout=30)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CollaborAll POST failed: {exc}") from exc

    location = post_resp.headers.get("Location")
    count_after = _count(ds_id_int)

    if not post_resp.ok:
        verdict = "rejected"
    elif count_before is not None and count_after is not None and count_after > count_before:
        verdict = "landed"
    elif location:
        verdict = "landed"
    else:
        verdict = "accepted_no_persist"

    return {
        "target": base,
        "datastream_id": ds_id_int,
        "datastream_name": datastream_name,
        "verdict": verdict,
        "status_code": post_resp.status_code,
        "location": location,
        "count_before": count_before,
        "count_after": count_after,
        "response_body": post_resp.text[:1000],
        "payload_sent": payload,
    }


@router.post("/collaborall-fix-locations")
def collaborall_fix_locations(
    dry_run: bool = Query(
        default=False, description="Preview the plan without writing when true."
    ),
    limit: int = Query(default=0, description="Cap the number of Things processed (0 = all)."),
) -> dict:
    """Repair CollaborAll: link a Location to every GEO_ Thing that lacks one.

    CollaborAll's FROST server rejects every observation with HTTP 409
    ``its Thing has no Location`` because its GEO_ Things were registered without
    a linked Location — that server ignores the nested ``Things`` deep-link on a
    Location POST (the primary Java FROST honours it). Without a Location the
    server cannot auto-resolve a FeatureOfInterest, so every write fails.

    This mirrors the Thing->Location pairing from the healthy primary target:
    for each unlinked CollaborAll Thing it finds the same-named Thing on the
    primary, copies its Location (creating it on CollaborAll if missing), and
    links it via a PATCH on the Thing's ``Locations`` navigation. Idempotent —
    Things that already have a Location are skipped.
    """
    import re
    import requests as req

    from app.services.sensorthings_client import _coerce_iot_id

    collab = next((t for t in settings.frost_targets if "collaborall" in t.url.lower()), None)
    if collab is None:
        raise HTTPException(status_code=404, detail="No CollaborAll target configured.")
    base_c = collab.url.rstrip("/")
    headers_c = {**client._headers_for_url(base_c), "Content-Type": "application/json"}

    base_p = client._http.primary_base_url.rstrip("/")
    headers_p = client._headers()
    prefix = settings.entity_name_prefix

    # List CollaborAll GEO_ Things with their current Locations.
    try:
        resp = req.get(
            f"{base_c}/Things",
            params={
                "$filter": f"startswith(name,'{prefix}')",
                "$expand": "Locations($select=@iot.id,name)",
                "$select": "@iot.id,name",
                "$top": "1000",
            },
            headers=headers_c,
            timeout=30,
        )
        resp.raise_for_status()
        things = resp.json().get("value", [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list CollaborAll Things: {exc}") from exc

    def _primary_location_for(thing_name: str) -> dict | None:
        """The Location of the same-named Thing on the primary (healthy) target."""
        try:
            r = req.get(
                f"{base_p}/Things",
                params={
                    "$filter": f"name eq '{thing_name}'",
                    "$expand": "Locations($select=name,description,encodingType,location)",
                    "$select": "@iot.id,name",
                    "$top": "1",
                },
                headers=headers_p,
                timeout=30,
            )
            r.raise_for_status()
            vals = r.json().get("value", [])
        except Exception:
            return None
        if not vals:
            return None
        locs = vals[0].get("Locations", [])
        return locs[0] if locs else None

    def _find_collab_location(name: str) -> int | None:
        try:
            r = req.get(
                f"{base_c}/Locations",
                params={"$filter": f"name eq '{name}'", "$select": "@iot.id", "$top": "1"},
                headers=headers_c,
                timeout=30,
            )
            r.raise_for_status()
            vals = r.json().get("value", [])
        except Exception:
            return None
        return vals[0]["@iot.id"] if vals else None

    def _id_from_response(r) -> int | None:
        try:
            body = r.json()
            if isinstance(body, dict) and body.get("@iot.id") is not None:
                return _coerce_iot_id(body["@iot.id"])
        except Exception:
            pass
        m = re.search(r"\((\d+)\)\s*$", r.headers.get("Location", ""))
        return int(m.group(1)) if m else None

    results: list[dict] = []
    linked = created = skipped = failed = processed = 0
    for thing in things:
        if limit and processed >= limit:
            break
        tname = thing.get("name")
        tid = thing.get("@iot.id")
        if thing.get("Locations"):
            skipped += 1
            continue
        processed += 1
        loc = _primary_location_for(tname)
        if not loc:
            failed += 1
            results.append({"thing": tname, "thing_id": tid, "status": "no_primary_location"})
            continue
        loc_name = loc.get("name")
        if dry_run:
            results.append({"thing": tname, "thing_id": tid, "status": "would_link", "location_name": loc_name})
            continue
        loc_id = _find_collab_location(loc_name)
        loc_created = False
        if loc_id is None:
            try:
                cr = req.post(
                    f"{base_c}/Locations",
                    json={
                        "name": loc_name,
                        "description": loc.get("description") or loc_name,
                        "encodingType": loc.get("encodingType") or "application/geo+json",
                        "location": loc.get("location"),
                    },
                    headers=headers_c,
                    timeout=30,
                )
                cr.raise_for_status()
                loc_id = _id_from_response(cr)
                loc_created = True
            except Exception as exc:
                failed += 1
                results.append({"thing": tname, "thing_id": tid, "status": "location_create_failed", "error": str(exc)[:300]})
                continue
        if loc_id is None:
            failed += 1
            results.append({"thing": tname, "thing_id": tid, "status": "location_id_unresolved"})
            continue
        try:
            pr = req.patch(
                f"{base_c}/Things({tid})",
                json={"Locations": [{"@iot.id": _coerce_iot_id(loc_id)}]},
                headers=headers_c,
                timeout=30,
            )
        except Exception as exc:
            failed += 1
            results.append({"thing": tname, "thing_id": tid, "status": "link_error", "error": str(exc)[:300]})
            continue
        if not pr.ok:
            failed += 1
            results.append({"thing": tname, "thing_id": tid, "status": "link_failed", "status_code": pr.status_code, "body": pr.text[:300]})
            continue
        linked += 1
        if loc_created:
            created += 1
        results.append({"thing": tname, "thing_id": tid, "status": "linked", "location_id": loc_id, "location_created": loc_created})

    return {
        "target": base_c,
        "primary": base_p,
        "dry_run": dry_run,
        "geo_things": len(things),
        "already_linked_skipped": skipped,
        "linked": linked,
        "locations_created": created,
        "failed": failed,
        "results": results,
    }


@router.post("/dlq-clear")
def dlq_clear(
    confirm: bool = Query(default=False, description="Must be true to actually truncate the DLQ."),
    keep_bytes: int = Query(
        default=0,
        ge=0,
        description="Keep this many bytes of the most recent DLQ tail (line-aligned); 0 = clear all.",
    ),
) -> dict:
    """Truncate the dead-letter queue to free disk space (full-disk recovery).

    ``failed_observations.jsonl`` can grow until it fills the mounted disk when a
    target rejects writes for an extended period (e.g. the CollaborAll 409
    storm before Locations were linked). A full disk then fails every disk write
    — registration cache, the DLQ itself — degrading the whole service.

    Without ``confirm=true`` this only reports the current size. With it, the
    file is truncated, optionally preserving the last ``keep_bytes`` (rounded up
    to the next newline so no partial JSON line survives). The truncate happens
    first, so space is freed even when the disk is full.
    """
    from pathlib import Path

    p = Path(settings.failed_observations_path)
    try:
        size_before = p.stat().st_size
    except FileNotFoundError:
        return {"cleared": False, "reason": "DLQ file does not exist", "size_bytes": 0}

    if not confirm:
        return {"cleared": False, "size_bytes": size_before, "note": "Pass confirm=true to truncate."}

    tail = b""
    if keep_bytes > 0 and size_before > keep_bytes:
        try:
            with p.open("rb") as handle:
                handle.seek(size_before - keep_bytes)
                tail = handle.read()
            # Drop a leading partial line so only whole JSON records remain.
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1:]
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read DLQ tail: {exc}") from exc

    try:
        # Open in write mode truncates to 0 first (frees the space), then we
        # write back the preserved tail (small enough to fit).
        with p.open("wb") as handle:
            if tail:
                handle.write(tail)
        size_after = p.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to truncate DLQ: {exc}") from exc

    logger.warning("DLQ truncated via API: freed %d bytes (kept %d)", size_before - size_after, size_after)
    return {
        "cleared": True,
        "freed_bytes": size_before - size_after,
        "size_before": size_before,
        "size_after": size_after,
    }


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
