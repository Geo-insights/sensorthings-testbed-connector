"""Catch up Kafka backlog → FROST + monitoring DB.

Consumes all pending Kafka messages, pushes observations to FROST
using concurrent HTTP requests, and inserts directly into the
monitoring DB in bulk for immediate visibility.

Usage:
    python scripts/catchup_kafka_frost.py [--max-batches 200] [--workers 20]
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

from app.config import settings  # noqa: E402
from app.services.kafka_tgv_consumer import KafkaTGVConsumer  # noqa: E402
from app.sources.tgv_kafka_mapping import avro_batch_to_sensor_readings  # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────
FROST_BASE = "https://sta.wbd-rd.nl/FROST-Server/v1.1"
FROST_AUTH = (settings.auth_username, settings.auth_password)
DS_IDS = dict(settings.datastream_ids)  # stream_key → frost DS id

DB_URL = (
    "postgresql://postgres.upjxsqjnrnamsexrlkqx:"
    "nietzotochpa%21%40%23@aws-1-eu-central-2.pooler.supabase.com:5432/postgres"
)

# monitoring sensor_id by frost_datastream_id
MONITORING_SENSOR_MAP: dict[str, str] = {}


def _init_monitoring_map():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, metadata->>'frost_datastream_id'
           FROM monitoring.sensors
           WHERE metadata->>'server' = 'https://sta.wbd-rd.nl/FROST-Server/v1.1'"""
    )
    for row in cur.fetchall():
        if row[1]:
            MONITORING_SENSOR_MAP[row[1]] = str(row[0])
    conn.close()


def _push_one_frost(session: requests.Session, reading) -> str:
    ds_id = DS_IDS.get(reading.stream_key or "")
    if not ds_id:
        return "skip"
    payload = {
        "phenomenonTime": reading.timestamp.isoformat().replace("+00:00", "Z"),
        "result": reading.value,
        "resultQuality": reading.quality,
        "Datastream": {"@iot.id": int(ds_id)},
    }
    try:
        resp = session.post(f"{FROST_BASE}/Observations", json=payload, timeout=15)
        return "ok" if resp.ok else f"fail:{resp.status_code}"
    except Exception:
        return "error"


def run(max_batches: int = 200, workers: int = 20):
    _init_monitoring_map()
    print(f"Monitoring sensor map: {MONITORING_SENSOR_MAP}")

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    session = requests.Session()
    session.auth = FROST_AUTH

    total_frost_ok = 0
    total_frost_fail = 0
    total_monitoring = 0
    batches = 0

    with KafkaTGVConsumer() as consumer:
        while batches < max_batches:
            records = consumer.consume_batch(max_messages=500, timeout=8.0)
            if not records:
                print("No more records — caught up!")
                break
            batches += 1
            readings = avro_batch_to_sensor_readings(records)
            last_ts = datetime.fromtimestamp(
                records[-1]["timestamp"] / 1000, tz=timezone.utc
            )

            # ── FROST push (concurrent) ─────────────────────────────
            ok = fail = skip = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_push_one_frost, session, r): r for r in readings
                }
                for f in as_completed(futures):
                    result = f.result()
                    if result == "ok":
                        ok += 1
                    elif result == "skip":
                        skip += 1
                    else:
                        fail += 1

            # ── Monitoring DB insert (bulk) ─────────────────────────
            mon_inserted = 0
            for r in readings:
                frost_ds_id = DS_IDS.get(r.stream_key or "")
                if not frost_ds_id:
                    continue
                mon_sensor_id = MONITORING_SENSOR_MAP.get(frost_ds_id)
                if not mon_sensor_id:
                    continue
                cur.execute(
                    """INSERT INTO monitoring.sensor_readings
                       (sensor_id, timestamp, value, quality, source)
                       VALUES (CAST(%s AS uuid), %s, %s, %s, 'kafka')
                       ON CONFLICT DO NOTHING""",
                    (mon_sensor_id, r.timestamp, r.value, r.quality),
                )
                mon_inserted += cur.rowcount
            conn.commit()

            total_frost_ok += ok
            total_frost_fail += fail
            total_monitoring += mon_inserted
            now = datetime.now(timezone.utc)
            delay = (now - last_ts).total_seconds()
            print(
                f"Batch {batches}: {len(records)} rec | "
                f"FROST ok={ok} fail={fail} skip={skip} | "
                f"Mon +{mon_inserted} | "
                f"last={last_ts.strftime('%H:%M:%S')} delay={delay:.0f}s"
            )

            if delay < 30:
                print("Caught up to real-time!")
                break

    conn.close()
    print(
        f"\nDone: {batches} batches | "
        f"FROST: {total_frost_ok} ok, {total_frost_fail} fail | "
        f"Monitoring: {total_monitoring} inserted"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-batches", type=int, default=200)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()
    run(max_batches=args.max_batches, workers=args.workers)
