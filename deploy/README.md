# Hetzner VPS Deployment

Runs the sensorthings-testbed-connector and monitoring-module on a single Hetzner CX22 VPS behind Caddy (auto-TLS).

## Quick Start

```bash
# On a fresh Ubuntu 24.04 server (as root):
bash setup-server.sh

# Then as geo user:
cd /opt/geo-insights
nano connector.env    # paste env vars from Render
nano monitoring.env   # paste env vars from Render
docker compose up -d
```

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Caddy + connector + monitoring |
| `Caddyfile` | Reverse proxy with auto Let's Encrypt |
| `deploy.sh` | Pull + build + restart (webhook or manual) |
| `webhook.py` | GitHub push webhook listener (port 9000) |
| `setup-server.sh` | Initial server provisioning |
| `connector.env` | Connector env vars (not committed) |
| `monitoring.env` | Monitoring env vars (not committed) |

## Manual Deploy

```bash
ssh geo@<IP> "cd /opt/geo-insights && ./deploy.sh"
```

## Monitoring

```bash
# Health checks
curl https://connector.geo-insights.nl/health
curl https://monitor.geo-insights.nl/health

# Source freshness
curl https://connector.geo-insights.nl/connector/freshness

# Logs
ssh geo@<IP> "cd /opt/geo-insights && docker compose logs -f connector --tail 50"

# Resource usage
ssh geo@<IP> "docker stats --no-stream"
```
# webhook test 2026-10-07T19:47:54Z
