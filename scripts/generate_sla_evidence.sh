#!/usr/bin/env bash
# Generate SLA evidence artifacts for WBSO KB-02.
# Usage: ./scripts/generate_sla_evidence.sh [--start YYYY-MM-DD] [--end YYYY-MM-DD]
#
# This script:
#   1. Runs the validation report generator
#   2. Copies generated report + metrics to a timestamped staging directory
#   3. Prints a summary of what was generated and where to commit it
#
# Prerequisites:
#   - Python 3.12+ with project dependencies installed
#   - Validation JSONL data in data/validation/
#   - VALIDATION_HARNESS_ENABLED must have been true during the measurement window

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Parse arguments ──────────────────────────────────────────────────
REPORT_ARGS=()
START_LABEL=""
END_LABEL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --start)
            REPORT_ARGS+=(--start "$2")
            START_LABEL="$2"
            shift 2
            ;;
        --end)
            REPORT_ARGS+=(--end "$2")
            END_LABEL="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--start YYYY-MM-DD] [--end YYYY-MM-DD]"
            echo ""
            echo "Generate SLA evidence artifacts from validation harness data."
            echo "Optional --start/--end restrict the window (ISO dates, passed to"
            echo "generate_validation_report.py)."
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# ── Paths ────────────────────────────────────────────────────────────
REPORT_OUT="$PROJECT_ROOT/reports/geonovum_14d_validation.md"
METRICS_OUT="$PROJECT_ROOT/data/validation/metrics.json"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGING_DIR="$PROJECT_ROOT/reports/sla_evidence_${TIMESTAMP}"

# ── Preflight checks ────────────────────────────────────────────────
DATA_DIR="$PROJECT_ROOT/data/validation"
if [[ ! -d "$DATA_DIR" ]]; then
    echo "ERROR: Validation data directory not found: $DATA_DIR" >&2
    echo "Has the validation harness been enabled? (VALIDATION_HARNESS_ENABLED=true)" >&2
    exit 1
fi

SNAPSHOTS="$DATA_DIR/snapshots.jsonl"
if [[ ! -f "$SNAPSHOTS" ]]; then
    echo "ERROR: No snapshots file at $SNAPSHOTS" >&2
    echo "The harness must run for at least one snapshot interval (5 min) to produce data." >&2
    exit 1
fi

SNAP_LINES="$(wc -l < "$SNAPSHOTS" 2>/dev/null || echo 0)"
echo "Found $SNAP_LINES snapshot lines in $SNAPSHOTS"

# ── Step 1: Generate report ──────────────────────────────────────────
echo ""
echo "=== Generating validation report ==="
python "$PROJECT_ROOT/scripts/generate_validation_report.py" "${REPORT_ARGS[@]+"${REPORT_ARGS[@]}"}"

# ── Step 2: Stage artifacts ──────────────────────────────────────────
echo ""
echo "=== Staging evidence artifacts ==="
mkdir -p "$STAGING_DIR"

cp "$REPORT_OUT" "$STAGING_DIR/validation_report.md"
echo "  Copied report  -> $STAGING_DIR/validation_report.md"

if [[ -f "$METRICS_OUT" ]]; then
    cp "$METRICS_OUT" "$STAGING_DIR/metrics.json"
    echo "  Copied metrics -> $STAGING_DIR/metrics.json"
fi

# Include a manifest with generation metadata
cat > "$STAGING_DIR/manifest.json" <<MANIFEST
{
  "generated_at": "$TIMESTAMP",
  "generator": "scripts/generate_sla_evidence.sh",
  "start_filter": "${START_LABEL:-auto}",
  "end_filter": "${END_LABEL:-auto}",
  "source_snapshots": "$SNAPSHOTS",
  "snapshot_lines": $SNAP_LINES
}
MANIFEST
echo "  Wrote manifest -> $STAGING_DIR/manifest.json"

# ── Step 3: Summary ─────────────────────────────────────────────────
echo ""
echo "=== Evidence generation complete ==="
echo ""
echo "Staged artifacts:"
ls -lh "$STAGING_DIR/"
echo ""
echo "Next steps:"
echo "  1. Review the report:    less $STAGING_DIR/validation_report.md"
echo "  2. Copy to WBSO dossier: cp -r $STAGING_DIR/ ../Geo-Insights-MVP/docs/WBSO/evidence/"
echo "  3. Commit in both repos"
echo ""
echo "Note: Any completed measurement window produces usable evidence."
echo "The report documents actual metrics, not pass/fail."
