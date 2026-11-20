import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.frost.odata_fields import FrostODataFields, odata_fields_for
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
    if settings.frost_async_push_enabled:
        # Decoupled path: hand the readings to the background FROST worker and
        # return immediately so the consume loop can pull the next batch. Kafka
        # stays live regardless of how slow the SensorThings push is. Committing
        # after the hand-off is at-least-once: FROST failures are dead-lettered
        # by push_observations (DLQ + replay). Backpressure: if the worker queue
        # is full we push inline instead of dropping.
        from app.services.frost_worker import frost_worker

        queued = frost_worker.enqueue(readings, settings.frost_worker_enqueue_timeout_seconds)
        if not queued:
            frost_worker.record_inline_fallback()
            client.push_observations(readings, source="kafka")
        pushed: int | str = "queued" if queued else "inline"
    else:
        # Legacy coupled path: push inline before commit.
        result = client.push_observations(readings, source="kafka")
        pushed = result.get("total_sent", 0)
    # Commit only after the push has been handed off (async) or completed
    # (inline) — failed observations are safely in the DLQ, so re-consuming them
    # would just create duplicates.
    consumer.commit()
    # How far behind real time is the newest observation we just handled? A large
    # value means we're replaying a stale backlog (e.g. after a long stall), so
    # downstream consumers see an old "latest" timestamp.
    newest = max((r.timestamp for r in readings if r.timestamp is not None), default=None)
    lag_seconds = (datetime.now(UTC) - newest).total_seconds() if newest is not None else None
    return {
        "pulled": len(records),
        "mapped": len(readings),
        "pushed": pushed,
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
    # Cycle timeout: hard cap on a single consume+push cycle to prevent
    # exceeding max.poll.interval.ms when a FROST target is slow/stuck.
    cycle_timeout = max(120, settings.kafka_tgv_max_poll_interval_ms // 1000 - 120)
    logger.info(
        "Kafka ingest loop started (persistent consumer, poll every %ds, stall threshold %ds, cycle timeout %ds)",
        poll_seconds, stale_threshold, cycle_timeout,
    )

    def _drop_consumer(c) -> None:
        with suppress(Exception):
            c.close()

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
            try:
                summary = await asyncio.wait_for(
                    run_in_threadpool(
                        _consume_and_push_once, consumer, settings.kafka_tgv_batch_max_messages
                    ),
                    timeout=cycle_timeout,
                )
            except TimeoutError:
                logger.error(
                    "Kafka consume+push cycle exceeded %ds timeout — forcing reconnect",
                    cycle_timeout,
                )
                send_alert(
                    "kafka.cycle_timeout",
                    f"Kafka consume+push cycle exceeded {cycle_timeout}s — likely a stuck FROST target. "
                    "Reconnecting consumer to avoid max.poll.interval eviction.",
                    level="critical",
                    context={"timeout_seconds": cycle_timeout},
                    dedup_key="kafka.cycle_timeout",
                )
                if consumer is not None:
                    await run_in_threadpool(_drop_consumer, consumer)
                    consumer = None
                await asyncio.sleep(poll_seconds)
                continue
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


# -- Restart seeding ------------------------------------------------------------
# Each polling loop drops readings at or before its high-water mark per
# sensor+property (``_dedup_readings``). The marks live in memory, so after a
# restart they are seeded from FROST: without them every reading still inside a
# source's look-back window is posted again, and FROST stores the duplicates.
_SEED_PAGE_SIZE = 500
_SEED_TIME_BUDGET_SECONDS = 120.0
_SEED_REQUEST_TIMEOUT_SECONDS = 30.0
# Each Datastream's newest Observation, expanded into the page. FROST answers it
# with one index probe per Datastream where OBSERVATIONS is indexed on
# (DATASTREAM_ID, PHENOMENON_TIME_START), as on frost.geo-insights.nl; without
# that index it reads all of each Datastream's Observations.
_SEED_NEWEST_OBSERVATION = "Observations($select=phenomenonTime;$orderby=phenomenonTime desc;$top=1)"


def _odata_string(value: str) -> str:
    """Quote ``value`` as an OData string literal."""
    return "'" + value.replace("'", "''") + "'"


def _seed_filter(source_name: str) -> str:
    """OData ``$filter`` selecting a polling source's Datastreams.

    Every mapper in ``app/sources`` writes its source's ``source_name`` (lower
    case) to the ``properties.source`` of its Things, so the name is matched in
    lower case and callers may pass "Ohnics". The entity name prefix, when one
    is configured, keeps the selection to this connector's entities on a shared
    server.
    """
    clauses = [f"Thing/properties/source eq {_odata_string(source_name.strip().lower())}"]
    prefix = settings.entity_name_prefix.strip()
    if prefix:
        clauses.insert(0, f"startswith(name,{_odata_string(prefix)})")
    return " and ".join(clauses)


def _phenomenon_time_end(raw: Any) -> datetime | None:
    """End of a ``phenomenonTime`` value as an aware datetime, or None.

    SensorThings v1.1 renders an instant as ``"t"`` and an interval as
    ``"start/end"``; v2.0 renders both as ``{"start": ..., "end": ...}``, an
    instant without ``end``. A time without an offset is taken as UTC.
    """
    if isinstance(raw, dict):
        raw = raw.get("end") or raw.get("start")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.strip().rsplit("/", 1)[-1].replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _newest_observation_time(datastream: dict[str, Any]) -> datetime | None:
    """phenomenonTime of the newest Observation expanded into a Datastream row."""
    observations = datastream.get("Observations")
    if isinstance(observations, list) and observations and isinstance(observations[0], dict):
        return _phenomenon_time_end(observations[0].get("phenomenonTime"))
    return None


def _entity_id(row: dict[str, Any], fields: FrostODataFields) -> Any:
    """``@iot.id`` (v1.1) or ``id`` (v2.0) of an entity row, or None."""
    for name in (fields.id, "@iot.id", "id"):
        if row.get(name) is not None:
            return row[name]
    return None


def _describe_request_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"[:200]


def _seed_timestamps_from_frost(
    source_name: str,
    *,
    page_size: int = _SEED_PAGE_SIZE,
    time_budget_seconds: float = _SEED_TIME_BUDGET_SECONDS,
    request_timeout_seconds: float = _SEED_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, datetime]:
    """Newest phenomenonTime in FROST per ``sensor_id::observed_property`` of a source.

    Seeds a polling loop's high-water marks after a restart. Pages through all
    of the source's Datastreams on the primary target (``_seed_filter``) in id
    order, ``page_size`` per request, continuing after the last id while the
    server reports a next page; works against SensorThings v1.1 and v2.0. Each
    Datastream counts with the later of its newest Observation, expanded into
    the page, and its own phenomenonTime. FROST's triggers keep the latter, but
    it lags when Observations of one Datastream are inserted concurrently. When
    a page with the expansion fails, it is asked again without it, and the rest
    of the sweep relies on the Datastreams' phenomenonTime alone.

    Best effort and bounded: stops when ``time_budget_seconds`` are used up or a
    page fails, keeps what it has and never raises. A partial seed only means
    that readings for the Datastreams it did not reach may be posted again.
    """
    base = (client._http.primary_base_url or "").rstrip("/")
    if not base:
        return {}
    stack = client._target_stack_for_url(base)
    fields = stack.fields if stack else odata_fields_for("1.1")
    headers = client._headers_for_url(base)
    url = f"{base}{settings.datastreams_path}"
    source_filter = _seed_filter(source_name)

    started = time.monotonic()
    deadline = started + time_budget_seconds
    timestamps: dict[str, datetime] = {}
    last_id: Any = None
    expand = True
    pages = datastreams = without_key = without_observations = lagging = 0
    stopped: str | None = None
    not_expanded: str | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stopped = f"time budget of {time_budget_seconds:g}s used up"
            break
        page_filter = source_filter
        if last_id is not None:
            literal = str(last_id) if isinstance(last_id, int) else _odata_string(str(last_id))
            page_filter = f"{source_filter} and {fields.id} gt {literal}"
        params = {
            "$filter": page_filter,
            "$select": f"{fields.id},properties,phenomenonTime",
            "$orderby": f"{fields.id} asc",
            "$top": str(page_size),
        }
        if expand:
            params["$expand"] = _SEED_NEWEST_OBSERVATION
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=min(request_timeout_seconds, remaining)
            )
            response.raise_for_status()
            body = response.json()
            page = body["value"]
            if not isinstance(page, list):
                raise TypeError("'value' is not a list")
        except Exception as exc:
            if expand:
                expand = False
                not_expanded = f"page {pages + 1} on ({_describe_request_error(exc)})"
                continue
            stopped = f"page {pages + 1} failed: {_describe_request_error(exc)}"
            break
        pages += 1
        for ds in page:
            datastreams += 1
            props = ds.get("properties") if isinstance(ds, dict) else None
            if not isinstance(props, dict) or not props.get("sensor_id") or not props.get("observed_property"):
                without_key += 1
                continue
            ds_time = _phenomenon_time_end(ds.get("phenomenonTime"))
            observation_time = _newest_observation_time(ds)
            if ds_time is not None and observation_time is not None and observation_time > ds_time:
                lagging += 1
            ts = max((t for t in (ds_time, observation_time) if t is not None), default=None)
            if ts is None:
                without_observations += 1
                continue
            key = f"{props['sensor_id']}::{props['observed_property']}"
            # Several Datastreams can share a key: keep the newest.
            if key not in timestamps or ts > timestamps[key]:
                timestamps[key] = ts
        if not (body.get(fields.next_link) or body.get("@iot.nextLink") or body.get("@nextLink")):
            break
        next_id = _entity_id(page[-1], fields) if page and isinstance(page[-1], dict) else None
        if next_id is None or next_id == last_id:
            stopped = f"page {pages} gives no id to continue after"
            break
        last_id = next_id

    counts = (
        f"{datastreams} datastreams in {pages} page(s), {without_observations} without observations, "
        f"{without_key} without sensor_id/observed_property, {lagging} with a phenomenonTime behind "
        f"their newest Observation, {time.monotonic() - started:.1f}s"
    )
    if not_expanded:
        counts += (
            f"; newest Observations not expanded from {not_expanded}, so those datastreams count "
            "with their own phenomenonTime, which can lag"
        )
    if stopped:
        logger.warning(
            "Seeded %d timestamps from FROST for %s, incomplete (%s; %s): readings for the "
            "datastreams not reached may be posted again",
            len(timestamps), source_name, stopped, counts,
        )
    elif not_expanded:
        logger.warning("Seeded %d timestamps from FROST for %s (%s)", len(timestamps), source_name, counts)
    else:
        logger.info("Seeded %d timestamps from FROST for %s (%s)", len(timestamps), source_name, counts)
    return timestamps


# -- Entity set registration ----------------------------------------------------
# A polling loop registers the entity sets its source reports before it pushes
# their readings: at startup the sets known then, and in every cycle with
# readings the sets discovered since, plus those whose registration failed.
_REGISTRATION_FAILURES_BEFORE_DEFERRING = 3


def _entity_set_datastream_keys(entity_set: dict[str, Any]) -> frozenset[str]:
    """``sensor_id::observed_property`` of every Datastream an entity set defines."""
    return frozenset(
        f"{sensor.get('sensor_id')}::{observed_property}"
        for sensor in entity_set.get("sensors") or []
        if isinstance(sensor, dict)
        for observed_property in sensor.get("observed_properties") or []
    )


def _registration_complete(result: Any) -> bool:
    """Whether ``client.register_entity_set`` registered a whole set on the primary target.

    The client reports a failed Thing as ``primary_ok`` false and a failed
    Location, Sensor or Datastream as a missing id; it skips observed
    properties that are not canonical, which therefore do not count. Without a
    FROST target it only previews, and there is nothing to register.
    """
    if not isinstance(result, dict):
        return False
    if result.get("mode") == "preview":
        return True
    if result.get("mode") != "live" or not result.get("primary_ok"):
        return False
    ids = [result.get("thing_id"), result.get("location_id")]
    ids += list((result.get("sensor_ids") or {}).values())
    ids += list((result.get("datastream_ids") or {}).values())
    return all(ids)


@dataclass
class _EntitySetRegistry:
    """The entity sets a polling loop registered on the primary FROST target.

    ``registered`` maps a set's ``site_key`` to the datastream keys its
    registrations covered, so a set that gains a sensor or an observed property
    counts as new again. ``failures`` counts failed registrations per
    ``site_key``.
    """

    registered: dict[str, frozenset[str]] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def is_registered(self, entity_set: dict[str, Any]) -> bool:
        covered = self.registered.get(str(entity_set.get("site_key")))
        return covered is not None and _entity_set_datastream_keys(entity_set) <= covered

    def pending(self, entity_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The sets not registered yet; those that failed before go last, so a
        set that keeps failing cannot hold up new ones."""
        return sorted(
            (entity_set for entity_set in entity_sets if not self.is_registered(entity_set)),
            key=lambda entity_set: self.failures.get(str(entity_set.get("site_key")), 0),
        )

    def record(self, entity_set: dict[str, Any], complete: bool) -> None:
        site_key = str(entity_set.get("site_key"))
        if complete:
            covered = self.registered.get(site_key, frozenset())
            self.registered[site_key] = covered | _entity_set_datastream_keys(entity_set)
            self.failures.pop(site_key, None)
        else:
            self.failures[site_key] = self.failures.get(site_key, 0) + 1


async def _register_new_entity_sets(source: Any, registry: _EntitySetRegistry) -> int:
    """Register the source's entity sets that ``registry`` does not have yet.

    Returns how many stay unregistered: those whose registration failed, and,
    after ``_REGISTRATION_FAILURES_BEFORE_DEFERRING`` failures in a row (the
    target is probably down), the rest. The next call tries them again.
    """
    pending = registry.pending(source.entity_sets())
    if not pending:
        return 0
    logger.info("%s: registering %d new entity sets in FROST", source.source_name, len(pending))
    failed: list[str] = []
    failures_in_a_row = deferred = 0
    for index, entity_set in enumerate(pending):
        if failures_in_a_row >= _REGISTRATION_FAILURES_BEFORE_DEFERRING:
            deferred = len(pending) - index
            break
        try:
            result = await run_in_threadpool(client.register_entity_set, entity_set)
        except Exception:
            logger.warning(
                "%s: registering entity set %s raised", source.source_name, entity_set.get("site_key"), exc_info=True
            )
            result = None
        complete = _registration_complete(result)
        registry.record(entity_set, complete)
        if complete:
            failures_in_a_row = 0
        else:
            failures_in_a_row += 1
            failed.append(str(entity_set.get("site_key")))
    if failed or deferred:
        logger.warning(
            "%s: %d of %d new entity sets not registered (%d failed%s); retrying next cycle: %s",
            source.source_name,
            len(failed) + deferred,
            len(pending),
            len(failed),
            f", {deferred} deferred after {failures_in_a_row} failures in a row" if deferred else "",
            ", ".join(failed[:5]) + (", ..." if len(failed) > 5 else ""),
        )
    else:
        logger.info("%s: registered %d new entity sets", source.source_name, len(pending))
    return len(failed) + deferred


def _polling_stale_threshold(poll_seconds: int) -> int:
    """Seconds of no data after which a polling source is considered stalled."""
    return poll_seconds + max(0, settings.freshness_grace_seconds)


async def _polling_ingest_loop(source):
    """Background loop for a REST API polling source."""

    poll_seconds = source.poll_interval()
    stale_threshold = _polling_stale_threshold(poll_seconds)
    logger.info(
        "Polling loop started for %s (every %ds, stall threshold %ds)",
        source.source_name, poll_seconds, stale_threshold,
    )

    # Eager registration: register entities before the first data fetch so we
    # don't lose readings if the first fetch succeeds but registration would
    # have been the next step.
    registry = _EntitySetRegistry()
    if source.entity_sets():
        for attempt in range(1, 4):
            logger.info("%s: eagerly registering entity sets in FROST (attempt %d)", source.source_name, attempt)
            if not await _register_new_entity_sets(source, registry):
                break
            if attempt < 3:
                await asyncio.sleep(10)
    else:
        logger.info("%s: no static entity sets; will register on first data fetch", source.source_name)

    # Seed from FROST so we don't re-push observations after a restart
    last_timestamps = await run_in_threadpool(
        _seed_timestamps_from_frost, source.source_name
    )

    while True:
        try:
            readings = await source.fetch_readings()
            if readings:
                # Register the entity sets discovered since the last cycle, and
                # retry those that failed, before pushing their readings.
                registered_before = bool(registry.registered)
                await _register_new_entity_sets(source, registry)
                if registry.registered and not registered_before and not last_timestamps:
                    # Nothing was seeded at startup; seed now that the entities exist.
                    last_timestamps = await run_in_threadpool(
                        _seed_timestamps_from_frost, source.source_name
                    )

                # Only push if entities are registered or we have cached IDs
                # from a previous run. Pushing before registration generates
                # thousands of DLQ entries on first boot for no reason.
                if not registry.registered and not last_timestamps:
                    logger.info(
                        "%s: skipping push (%d readings) — entities not yet registered",
                        source.source_name, len(readings),
                    )
                    new_readings = []
                else:
                    new_readings = _dedup_readings(readings, last_timestamps)
                    if not new_readings:
                        logger.debug("%s: all %d readings already pushed, skipping", source.source_name, len(readings))
                if new_readings:
                    _push_source = source.source_name.lower()
                    def _do_push(nr: list = new_readings, ps: str = _push_source) -> dict:
                        return client.push_observations(nr, source=ps)

                    result = await run_in_threadpool(_do_push)
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
        except Exception as exc:
            logger.exception("%s polling cycle failed", source.source_name)
            from app.services.health_monitor import health_monitor
            health_monitor.record_source_error(source.source_name.lower(), f"{type(exc).__name__}: {exc}")

        # Stall watchdog — mirrors the Kafka watchdog pattern. Fires when the
        # source hasn't delivered fresh data for longer than (poll_interval +
        # freshness_grace). Unlike Kafka there's no consumer to reconnect, so
        # the action is alert-only; the poller will keep retrying on the next
        # cycle regardless.
        from app.services.health_monitor import health_monitor
        age = health_monitor.source_age_seconds(source.source_name.lower())
        if age is not None and age > stale_threshold:
            from app.services.alerting import send_alert
            send_alert(
                f"{source.source_name.lower()}.stall",
                f"No {source.source_name} observations for {int(age)}s "
                f"(threshold {stale_threshold}s). Check upstream API availability.",
                level="warning",
                context={
                    "age_seconds": round(age, 1),
                    "threshold_seconds": stale_threshold,
                    "api_url": (getattr(source, '_client', None) and "configured") or "unknown",
                },
                dedup_key=f"{source.source_name.lower()}.stall",
            )

        await asyncio.sleep(poll_seconds)


def _get_enabled_polling_sources() -> list:
    """Instantiate and return all enabled polling sources."""
    sources: list = []

    from app.services.ohnics_source import OhnicsPollingSource
    ohnics = OhnicsPollingSource()
    if ohnics.is_enabled():
        sources.append(ohnics)

    from app.services.levellog_source import LevellogPollingSource
    levellog = LevellogPollingSource()
    if levellog.is_enabled():
        sources.append(levellog)

    from app.services.luchtmeetnet_source import LuchtmeetnetPollingSource
    luchtmeetnet = LuchtmeetnetPollingSource()
    if luchtmeetnet.is_enabled():
        sources.append(luchtmeetnet)

    from app.services.sensor_community_source import SensorCommunityPollingSource
    sensor_community = SensorCommunityPollingSource()
    if sensor_community.is_enabled():
        sources.append(sensor_community)

    from app.services.meet_je_stad_source import MeetJeStadPollingSource
    meet_je_stad = MeetJeStadPollingSource()
    if meet_je_stad.is_enabled():
        sources.append(meet_je_stad)

    from app.services.bro_groundwater_source import BROGroundwaterPollingSource
    bro = BROGroundwaterPollingSource()
    if bro.is_enabled():
        sources.append(bro)

    from app.services.samen_meten_source import SamenMetenPollingSource
    samen_meten = SamenMetenPollingSource()
    if samen_meten.is_enabled():
        sources.append(samen_meten)

    from app.services.knmi_weather_source import KNMIWeatherPollingSource
    knmi_weather = KNMIWeatherPollingSource()
    if knmi_weather.is_enabled():
        sources.append(knmi_weather)

    return sources


async def _failed_replay_loop():
    """Periodically replay dead-lettered observations back to FROST targets.

    Self-healing: when a pass hits its per-pass line cap and more remain, the
    loop comes back sooner (``min(interval, 60s)``) to drain faster instead of
    waiting the full interval. If the backlog stays above the configured alert
    threshold, a deduplicated alert fires so a stuck DLQ surfaces.
    """
    from app.services.alerting import send_alert

    interval = max(60, settings.failed_replay_interval_seconds)
    logger.info("DLQ replay loop started (every %ds, max %d/pass)", interval, settings.failed_replay_max_lines)
    drain_fast = False
    backlog_alerting = False
    while True:
        await asyncio.sleep(min(interval, 60) if drain_fast else interval)
        try:
            summary = await run_in_threadpool(
                client.replay_failed_observations, settings.failed_replay_max_lines
            )
            replayed = summary.get("replayed", 0)
            remaining = summary.get("remaining", 0)
            dropped = summary.get("dropped", 0)
            if replayed or remaining or dropped:
                logger.info("DLQ replay: %s replayed, %s dropped, %s remaining", replayed, dropped, remaining)
            # More than one pass' worth is still queued — come back sooner.
            drain_fast = remaining >= settings.failed_replay_max_lines
            alert_threshold = settings.failed_replay_backlog_alert
            if alert_threshold > 0 and remaining >= alert_threshold:
                backlog_alerting = True
                send_alert(
                    "dlq.backlog",
                    f"Dead-letter queue not draining: {remaining} observations still "
                    f"queued after a replay pass (threshold {alert_threshold}).",
                    level="warning",
                    context={"remaining": remaining, "replayed": replayed, "dropped": dropped},
                    dedup_key="dlq.backlog",
                )
            elif backlog_alerting and remaining == 0:
                backlog_alerting = False
                send_alert(
                    "dlq.backlog",
                    "Dead-letter queue drained — all replayed observations delivered.",
                    level="info",
                    context={"remaining": 0},
                    dedup_key="dlq.backlog",
                    force=True,
                )
        except Exception:
            logger.exception("DLQ replay pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    has_frost_target = bool(settings.sensorthings_base_url or settings.sensorthings_base_urls)

    # --- Validation harness (14-day Geonovum SLO test) ---
    # Configure the recorder before ingest starts so the very first push cycle
    # is captured. When VALIDATION_HARNESS_ENABLED=false this is a no-op.
    from app.services.validation import recorder as validation_recorder
    from app.services.validation.collector import snapshot_loop as _validation_snapshot_loop

    validation_recorder.configure(
        enabled=settings.validation_harness_enabled,
        data_dir=settings.validation_data_dir,
        events_max_bytes=settings.validation_events_max_bytes,
        incidents_max_bytes=settings.validation_incidents_max_bytes,
        sample_reservoir_size=settings.validation_sample_reservoir_size,
    )

    # Start the background FROST push worker before the ingest loops so they can
    # hand off immediately (Phase 2 decoupling).
    if settings.frost_async_push_enabled and has_frost_target:
        from app.services.frost_worker import frost_worker

        frost_worker.start(
            settings.frost_worker_queue_max,
            settings.frost_worker_coalesce_max,
            settings.frost_worker_backlog_alert_ratio,
        )

    # Warm the datastream-id cache so the first cycles don't do a serial live
    # OData lookup per reading (Phase 1c). Best-effort; never blocks startup.
    if has_frost_target:
        try:
            warmed = await run_in_threadpool(client.warm_datastream_cache)
            logger.info("Datastream cache warm-up complete (%d ids)", warmed)
        except Exception:
            logger.warning("Datastream cache warm-up failed (non-fatal)", exc_info=True)

    if settings.kafka_tgv_enabled:
        tasks.append(asyncio.create_task(_kafka_ingest_loop()))

    for source in _get_enabled_polling_sources():
        tasks.append(asyncio.create_task(_polling_ingest_loop(source)))

    if settings.failed_replay_enabled and has_frost_target:
        tasks.append(asyncio.create_task(_failed_replay_loop()))

    # Start the validation snapshot loop last so all sources are up.
    if settings.validation_harness_enabled:
        tasks.append(asyncio.create_task(_validation_snapshot_loop()))
        validation_recorder.record_incident(
            kind="app_start",
            level="info",
            context={"has_frost_target": has_frost_target, "kafka_enabled": settings.kafka_tgv_enabled},
        )

    yield
    for t in tasks:
        t.cancel()
    # Drain and stop the FROST worker so in-flight readings are flushed on a
    # graceful shutdown instead of lost.
    if settings.frost_async_push_enabled and has_frost_target:
        from app.services.frost_worker import frost_worker

        await run_in_threadpool(frost_worker.stop)


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

from app.routes.validation import router as _validation_router  # noqa: E402

app.include_router(_validation_router)


@app.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()


@app.get("/frost/capabilities")
def frost_capabilities() -> dict:
    return client.check_capabilities()
