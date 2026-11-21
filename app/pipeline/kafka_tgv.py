"""Concrete pipeline components for TGV Kafka Avro data source.

Also registers the TGV entity sets on the primary FROST target and lets the
Kafka loop push TGV readings only into their datastreams there
(:class:`TGVPrimaryRegistration`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.models import SensorReading
from app.pipeline.base import Decapsulator, Deserializer, Parser
from app.sta.canonical import resolve

if TYPE_CHECKING:
    from app.services.sensorthings_client import SensorThingsClient

logger = logging.getLogger("connector.debug")


class AvroUnionDeserializer(Deserializer):
    """Unwrap fastavro union dicts like ``{"float": 3.14}`` to plain values."""

    def deserialize(self, record: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in record.items():
            if isinstance(value, dict) and len(value) == 1:
                value = next(iter(value.values()))
            out[key] = value
        return out


class TGVDecapsulator(Decapsulator):
    """Extract individual measurement dicts from a GreenVillageRecord envelope."""

    def decapsulate(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        device_id = record.get("device_id", "")
        timestamp = record.get("timestamp", 0)
        measurements: list[dict[str, Any]] = record.get("measurements", [])
        return [
            {**m, "device_id": device_id, "timestamp": timestamp}
            for m in measurements
        ]


class TGVMeasurementParser(Parser):
    """Parse a single TGV measurement dict into a SensorReading.

    Requires a device mapping dict to resolve sensor/thing metadata.
    """

    def __init__(self, device_mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = device_mapping

    def parse(self, raw: dict[str, Any]) -> SensorReading:
        device_id = raw.get("device_id", "")
        measurement_id = raw.get("measurement_id", "")
        timestamp_ms = raw.get("timestamp", 0)

        # Resolve mapping
        exact_key = f"{device_id}/{measurement_id}"
        wildcard_key = f"*/{measurement_id}"
        mapping = self._mapping.get(exact_key) or self._mapping.get(wildcard_key)
        if mapping is None:
            raise ValueError(f"No mapping for {device_id}/{measurement_id}")

        # Extract numeric value
        raw_value = raw.get("value")
        if isinstance(raw_value, dict) and len(raw_value) == 1:
            raw_value = next(iter(raw_value.values()))
        if raw_value is None or isinstance(raw_value, bool):
            value = float(raw_value) if raw_value is not None else 0.0
        elif isinstance(raw_value, (int, float, str)):
            value = float(raw_value)
        else:
            raise ValueError(f"Cannot convert {raw_value!r} to float")

        try:
            ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
        except (OSError, ValueError, OverflowError):
            ts = datetime.now(UTC)

        canonical = resolve(mapping["observed_property"])
        if canonical is None:
            raise ValueError(
                f"Mapping for {device_id}/{measurement_id} uses non-canonical "
                f"observed_property {mapping['observed_property']!r}"
            )
        meta = canonical.meta
        unit_from_avro = raw.get("unit") or None
        if unit_from_avro and unit_from_avro != meta.unit:
            logger.info(
                "Avro unit %r for %s/%s disagrees with canonical %r — using canonical",
                unit_from_avro, device_id, measurement_id, meta.unit,
            )

        return SensorReading(
            sensor_id=mapping["sensor_id"],
            sensor_name=mapping.get("sensor_name", mapping["sensor_id"]),
            observed_property=canonical.value,
            unit=meta.unit,
            value=value,
            timestamp=ts,
            quality="good",
            location="tgv",
            thing_name=mapping["thing_name"],
            device_eui=device_id,
            stream_key=canonical.value,
            observed_property_name=meta.display_name,
        )


# ---------------------------------------------------------------------------
# TGV entity sets on the primary FROST target
# ---------------------------------------------------------------------------
# The Kafka loop pushes TGV readings only into the datastreams of the TGV
# entity sets (entity_configs/tgv_*.yaml) that this process registered on the
# primary target, and pins each TGV ``sensor_id::observed_property`` key to the
# datastream id the primary returned for it. FROST numbers datastreams per
# server, so an id kept from an earlier target (entity cache or
# SENSORTHINGS_DATASTREAM_IDS_JSON) names another source's datastream on the
# current one; and with no TGV datastreams on the primary, the old warm-up's
# bare property keys sent TGV readings into other sources' datastreams.
#
# Contract with the observation cleanup: TGV Things carry properties.site 'tgv'
# and properties.source 'kafka'. The cleanup deletes TGV-labelled rows in
# datastreams whose Thing lacks them, so a set counts as registered only once
# its Thing on the primary carries both.

TGV_SITE_KEY = "tgv"
TGV_THING_PROPERTIES: dict[str, str] = {"site": "tgv", "source": "kafka"}
# Sets failing in a row in one pass before the rest wait for the next batch
# (the target is probably down), as in app/main.py's polling loops.
_FAILURES_BEFORE_DEFERRING = 3
# A set whose registration failed waits 30 s, 60 s, 120 s, 240 s, then 5 min
# before each next attempt.
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 300.0
_ALERT_EVENT = "kafka.tgv_registration"

_events = logging.getLogger("connector.events")


class TGVRegistrationPending(RuntimeError):
    """No TGV entity set is registered on the primary FROST target yet.

    Raised before the push, so the Kafka loop does not commit the batch: it
    rebuilds the consumer and Kafka delivers the batch again.
    """


def tgv_datastream_key(sensor_id: str, observed_property: str) -> str:
    """``sensor_id::observed_property`` with the property's canonical key, as a TGV reading carries it."""
    canonical = resolve(observed_property)
    return f"{sensor_id}::{canonical.value if canonical is not None else observed_property}"


def tgv_entity_sets(entity_sets: Iterable[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The TGV entity sets (``site_key`` tgv) among ``entity_sets``, by default those in entity_configs/."""
    if entity_sets is None:
        from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS

        entity_sets = CLIMATE_ADAPTATION_ENTITY_SETS
    return [
        entity_set
        for entity_set in entity_sets
        if str(entity_set.get("site_key", "")).strip().lower() == TGV_SITE_KEY
    ]


def entity_set_datastream_keys(entity_set: dict[str, Any]) -> dict[str, str]:
    """Each datastream of an entity set: its canonical key -> the key ``register_entity_set`` reports it under."""
    keys: dict[str, str] = {}
    for sensor in entity_set.get("sensors") or []:
        if not isinstance(sensor, dict):
            continue
        sensor_id = str(sensor.get("sensor_id", ""))
        for observed_property in sensor.get("observed_properties") or []:
            keys[tgv_datastream_key(sensor_id, str(observed_property))] = f"{sensor_id}::{observed_property}"
    return keys


def _thing_name(entity_set: dict[str, Any]) -> str:
    thing = entity_set.get("thing")
    return str(thing.get("name", "")) if isinstance(thing, dict) else ""


def registration_problem(
    entity_set: dict[str, Any],
    result: Any,
    fetch_thing: Callable[[str], dict[str, Any] | None],
) -> str | None:
    """Why ``register_entity_set(entity_set)`` returning ``result`` did not register the whole set; None if it did.

    The rules of app/main.py's ``_registration_complete`` (the primary target
    took the Thing, the Location, every Sensor and every Datastream it reports),
    plus two: every datastream the set defines has an id, and the Thing on the
    primary, read back through ``fetch_thing``, carries TGV_THING_PROPERTIES.
    """
    if not isinstance(result, dict) or result.get("mode") != "live":
        return "no live registration result"
    if not result.get("primary_ok"):
        return str(result.get("primary_error") or "the primary target failed")
    thing_id = result.get("thing_id")
    if not thing_id:
        return "no Thing id"
    if not result.get("location_id"):
        return "no Location id"
    sensor_ids = result.get("sensor_ids") or {}
    failed_sensors = sorted(str(sensor) for sensor, sensor_id in sensor_ids.items() if not sensor_id)
    if failed_sensors:
        return f"no Sensor id for {', '.join(failed_sensors)}"
    datastream_ids = result.get("datastream_ids") or {}
    missing = sorted(
        {str(key) for key, ds_id in datastream_ids.items() if not ds_id}
        | {key for key in entity_set_datastream_keys(entity_set).values() if not datastream_ids.get(key)}
    )
    if missing:
        return f"no Datastream id for {', '.join(missing)}"
    thing = fetch_thing(str(thing_id))
    if not isinstance(thing, dict):
        return f"Thing {thing_id} could not be read back"
    properties = thing.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    differing = {key: properties.get(key) for key, value in TGV_THING_PROPERTIES.items() if properties.get(key) != value}
    if differing:
        return f"Thing {thing_id} has properties {differing}, not {TGV_THING_PROPERTIES}"
    return None


class TGVPrimaryRegistration:
    """Registers the TGV entity sets on the primary FROST target and keeps TGV readings to their datastreams.

    The Kafka loop passes every mapped batch through :meth:`prepare_push`
    before the push. It registers the sets not registered yet (all of them on
    the first batch after a start; later only those whose registration
    failed, each after its retry delay) and returns the readings that have a
    datastream in a registered set. ``register_entity_set`` finds the entities
    of an earlier run in the entity cache or by name, so a restart creates
    nothing twice.

    While no set is registered, :meth:`prepare_push` raises
    :class:`TGVRegistrationPending`, and the Kafka loop leaves the batch
    uncommitted. Once some are, readings of the others are left out of the
    push (there is no datastream for them) until their sets are registered.
    """

    def __init__(
        self,
        entity_sets: list[dict[str, Any]] | None = None,
        *,
        client: SensorThingsClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entity_sets = entity_sets
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._base_url: str | None = None
        # Thing name -> canonical "sensor_id::observed_property" -> datastream id on the primary
        self._registered: dict[str, dict[str, str]] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._reported: set[str] = set()
        self._holding = False

    def _frost(self) -> SensorThingsClient:
        if self._client is not None:
            return self._client
        from app.services.sensorthings_client import client

        return client

    def _sets(self) -> list[dict[str, Any]]:
        if self._entity_sets is None:
            self._entity_sets = tgv_entity_sets()
        return self._entity_sets

    def datastream_ids(self) -> dict[str, str]:
        """Canonical ``sensor_id::observed_property`` -> datastream id on the primary, for the registered sets."""
        with self._lock:
            return self._routes()

    def _routes(self) -> dict[str, str]:
        return {key: ds_id for ids in self._registered.values() for key, ds_id in ids.items()}

    def prepare_push(self, readings: list[SensorReading]) -> list[SensorReading]:
        """Register the pending TGV entity sets, then return the readings that have a registered datastream.

        Raises :class:`TGVRegistrationPending` when there are readings and no
        set is registered. Without a FROST target the readings pass unchanged
        (``push_observations`` only previews them).
        """
        frost = self._frost()
        base_url = frost._primary_base_url()
        if not base_url:
            return readings
        with self._lock:
            if base_url != self._base_url:
                self._reset(base_url)
            self._register_pending(frost, base_url)
            routes = self._routes()
            # Kept by register_entity_set; pinned again so that nothing else
            # (cache, env mapping, fallbacks) can route a TGV key elsewhere.
            pinned = frost._datastream_ids
            for key, ds_id in routes.items():
                if pinned.get(key) != ds_id:
                    pinned[key] = ds_id
            if not routes:
                if readings:
                    self._hold(base_url, len(readings))
                return []
            if self._holding:
                self._holding = False
                self._alert(
                    f"TGV entity sets registered on {base_url}; Kafka batches are pushed again.",
                    level="info",
                    force=True,
                )
            return self._routable(base_url, readings, routes)

    def _reset(self, base_url: str) -> None:
        self._base_url = base_url
        self._registered.clear()
        self._failures.clear()
        self._retry_at.clear()
        self._reported.clear()

    def _is_registered(self, entity_set: dict[str, Any]) -> bool:
        return set(entity_set_datastream_keys(entity_set)) <= set(self._registered.get(_thing_name(entity_set), {}))

    def _register_pending(self, frost: SensorThingsClient, base_url: str) -> None:
        now = self._clock()
        pending = [entity_set for entity_set in self._sets() if not self._is_registered(entity_set)]
        # Sets that failed before go last, so one that keeps failing cannot hold up the others.
        due = sorted(
            (entity_set for entity_set in pending if self._retry_at.get(_thing_name(entity_set), 0.0) <= now),
            key=lambda entity_set: self._failures.get(_thing_name(entity_set), 0),
        )
        failures_in_a_row = 0
        for index, entity_set in enumerate(due):
            if failures_in_a_row >= _FAILURES_BEFORE_DEFERRING:
                _events.warning(
                    "TGV: %d entity set(s) left for the next batch after %d failed in a row on %s",
                    len(due) - index, failures_in_a_row, base_url,
                )
                break
            if self._register(frost, base_url, entity_set, now):
                failures_in_a_row = 0
            else:
                failures_in_a_row += 1
        if due and all(self._is_registered(entity_set) for entity_set in self._sets()):
            _events.info(
                "TGV: all %d entity sets registered on %s (%d datastreams); TGV readings go to these only",
                len(self._registered), base_url, len(self._routes()),
            )

    def _register(self, frost: SensorThingsClient, base_url: str, entity_set: dict[str, Any], now: float) -> bool:
        from app.config import settings

        name = _thing_name(entity_set)
        result: Any = None
        try:
            result = frost.register_entity_set(entity_set)
            problem = registration_problem(
                entity_set, result, lambda thing_id: frost._fetch_entity_by_id(settings.things_path, thing_id)
            )
        except Exception as exc:
            _events.debug("TGV: registering entity set %r raised", name, exc_info=True)
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            ids = result["datastream_ids"]
            self._registered[name] = {
                canonical: str(ids[reported]) for canonical, reported in entity_set_datastream_keys(entity_set).items()
            }
            self._failures.pop(name, None)
            self._retry_at.pop(name, None)
            statuses = Counter(
                str(entry.get("status")) for entry in result.get("datastream_results") or [] if isinstance(entry, dict)
            )
            _events.info(
                "TGV entity set %r registered on %s: Thing %s (%s), Location %s (%s), %d datastreams (%s)",
                frost._prefixed_name(name), base_url,
                result.get("thing_id"), result.get("thing_status"),
                result.get("location_id"), result.get("location_status"),
                len(self._registered[name]),
                ", ".join(f"{count} {status}" for status, count in sorted(statuses.items())),
            )
            return True
        failures = self._failures[name] = self._failures.get(name, 0) + 1
        delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * 2 ** (failures - 1))
        self._retry_at[name] = now + delay
        _events.warning(
            "TGV entity set %r not registered on %s (failure %d): %s; next attempt in %ds",
            frost._prefixed_name(name), base_url, failures, problem, int(delay),
        )
        return False

    def _hold(self, base_url: str, count: int) -> None:
        pending = sorted(_thing_name(entity_set) for entity_set in self._sets() if not self._is_registered(entity_set))
        _events.warning(
            "TGV: no entity set registered on %s yet (pending: %s); %d readings left uncommitted for Kafka "
            "to deliver again",
            base_url, ", ".join(pending) or "none configured", count,
        )
        if not self._holding:
            self._holding = True
            self._alert(
                f"TGV Kafka batches are held: no TGV entity set is registered on {base_url} yet "
                f"({len(pending)} pending). Kafka keeps them until registration succeeds.",
                level="critical",
            )
        # Static text: the Kafka loop scans the message for auth errors.
        raise TGVRegistrationPending(
            f"no TGV entity set registered on the primary FROST target yet ({len(pending)} pending); "
            f"{count} readings left uncommitted"
        )

    def _routable(self, base_url: str, readings: list[SensorReading], routes: dict[str, str]) -> list[SensorReading]:
        routable: list[SensorReading] = []
        left_out: Counter[str] = Counter()
        for reading in readings:
            key = tgv_datastream_key(reading.sensor_id, reading.observed_property)
            if key in routes:
                routable.append(reading)
            else:
                left_out[key] += 1
        if left_out:
            new_keys = [key for key in sorted(left_out) if key not in self._reported]
            self._reported.update(new_keys)
            owners = {key: _thing_name(s) for s in self._sets() for key in entity_set_datastream_keys(s)}
            reasons = [
                f"{key} (entity set {owners[key]!r} not registered yet)" if key in owners else f"{key} (in no TGV entity set)"
                for key in sorted(left_out)[:10]
            ]
            _events.log(
                logging.WARNING if new_keys else logging.DEBUG,
                "TGV: %d of %d readings not pushed to %s, no registered TGV datastream: %s",
                sum(left_out.values()), len(readings), base_url, ", ".join(reasons),
            )
        return routable

    def _alert(self, message: str, *, level: str, force: bool = False) -> None:
        try:
            from app.services.alerting import send_alert

            send_alert(_ALERT_EVENT, message, level=level, context={"target": self._base_url}, force=force)
        except Exception:
            _events.debug("TGV: alert %s failed", _ALERT_EVENT, exc_info=True)


# Used by app/main.py's Kafka loop.
tgv_primary = TGVPrimaryRegistration()
