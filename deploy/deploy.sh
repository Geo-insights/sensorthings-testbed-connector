#!/bin/bash
# Deploy script for geo-insights services on Hetzner VPS.
# Triggered by GitHub webhook on push to main, or run manually via SSH.
#
# Usage:
#   ./deploy.sh              # pull + build + restart all
#   ./deploy.sh connector    # rebuild only the connector
#   ./deploy.sh monitoring   # rebuild only the monitoring module

set -euo pipefail

cd /opt/geo-insights

SERVICE="${1:-all}"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Deploy started (service=$SERVICE)"

# Pull latest code
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "connector" ]; then
    git -C sensorthings-testbed-connector pull --ff-only
fi
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "monitoring" ]; then
    git -C monitoring_module pull --ff-only
fi
# The MCP server runs on the FROST host, not here: see deploy/README.md.

# Build and restart
if [ "$SERVICE" = "all" ]; then
    docker compose build --parallel
    docker compose up -d --remove-orphans
else
    docker compose build "$SERVICE"
    docker compose up -d --no-deps "$SERVICE"
fi

# Cleanup old images
docker image prune -f

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Deploy complete"

# Health check. Ports 8010/8020 are not published on the host (only Caddy's
# 80/443 are), so curling localhost always failed and every deploy printed FAIL.
# Read the container's own Docker healthcheck (GET /health inside the container,
# every 30 s after a 30-60 s start period) and wait until it settles.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
check_health() {
    local name="$1" waited=0 cid status
    while :; do
        cid="$(docker compose ps -q "$name" 2>/dev/null || true)"
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo missing)"
        case "$status" in
            healthy)   echo " $name OK (healthy after ${waited}s)"; return 0 ;;
            unhealthy) echo " $name FAIL (unhealthy after ${waited}s)"; return 0 ;;
            none)      echo " $name UNKNOWN (no healthcheck defined)"; return 0 ;;
        esac
        if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
            echo " $name FAIL (still '$status' after ${HEALTH_TIMEOUT}s)"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
}
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "connector" ]; then
    check_health connector
fi
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "monitoring" ]; then
    check_health monitoring
fi
