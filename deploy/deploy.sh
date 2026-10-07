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

# Quick health check
sleep 5
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "connector" ]; then
    curl -sf http://localhost:8010/health && echo " connector OK" || echo " connector FAIL"
fi
if [ "$SERVICE" = "all" ] || [ "$SERVICE" = "monitoring" ]; then
    curl -sf http://localhost:8020/health && echo " monitoring OK" || echo " monitoring FAIL"
fi
