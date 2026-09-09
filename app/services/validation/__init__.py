"""Validation harness for the 14-day Geonovum SLO test.

Instruments the ingest → push path so uptime, per-path latency, error-rate
breakouts, circuit-breaker activity, and DLQ activity can be reported at the
end of a multi-day run.

Public surface:
  * ``recorder`` — singleton that captures per-push attempts + incidents and
    keeps rolling per-(source, target) aggregates. Safe no-op when the harness
    is disabled (``settings.validation_harness_enabled == False``).
  * ``classify_error`` — buckets exceptions/HTTP statuses into a stable
    taxonomy used by the report.
  * ``snapshot_loop`` — async task started from lifespan(); writes one
    consolidated snapshot line to ``data/validation/snapshots.jsonl`` per
    interval.

Storage layout under ``settings.validation_data_dir``:
  * ``events.jsonl``      — append-only, one line per non-clean push attempt.
  * ``incidents.jsonl``   — append-only, one line per notable incident.
  * ``snapshots.jsonl``   — append-only, one line per snapshot interval.

The report generator (``scripts/generate_validation_report.py``) reads these
three files (plus rotated backups) to produce the final Geonovum addendum.
"""

from app.services.validation.error_classifier import classify_error
from app.services.validation.recorder import MONITORING_TARGET, recorder

__all__ = ["MONITORING_TARGET", "classify_error", "recorder"]
