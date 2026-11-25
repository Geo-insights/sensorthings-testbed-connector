"""Concurrent dead-letter appends must not lose or interleave lines.

Push threads dead-letter observations at the same time. Append mode alone is not
atomic on Windows, so `_write_failed_observation` serialises writers with a lock.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.services.sensorthings_client import SensorThingsClient

THREADS = 16
PER_THREAD = 200


def _bare_client(dlq: Path) -> SensorThingsClient:
    client = SensorThingsClient.__new__(SensorThingsClient)  # only the DLQ path is used
    client._failed_observations_path = dlq
    return client


def test_concurrent_dead_letter_appends_keep_every_line(tmp_path: Path) -> None:
    dlq = tmp_path / "nested" / "failed_observations.jsonl"
    # Two instances: the lock must hold across clients writing the same file.
    clients = [_bare_client(dlq), _bare_client(dlq)]
    start = threading.Barrier(THREADS)

    def writer(t: int) -> None:
        start.wait()
        for i in range(PER_THREAD):
            clients[t % 2]._write_failed_observation(
                {"datastream_id": str(t), "seq": i, "payload": {"result": "x" * 512}}
            )

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    lines = dlq.read_text(encoding="utf-8").splitlines()
    assert len(lines) == THREADS * PER_THREAD
    seen = {(rec["datastream_id"], rec["seq"]) for rec in map(json.loads, lines)}
    assert len(seen) == THREADS * PER_THREAD
