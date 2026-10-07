#!/bin/bash
# Initial server setup for Hetzner CX22 (Ubuntu 24.04 LTS).
# Run as root on a fresh server.
#
# Usage:
#   curl -sSL <raw-github-url> | bash
#   OR: scp this file to server, then: bash setup-server.sh

set -euo pipefail

echo "=== Geo-Insights Server Setup ==="

# 1. System updates
apt-get update && apt-get upgrade -y

# 2. Install Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker

# 3. Install Docker Compose plugin (comes with Docker now, but ensure)
docker compose version || apt-get install -y docker-compose-plugin

# 4. Create deploy user
if ! id -u geo &>/dev/null; then
    useradd -m -s /bin/bash -G docker geo
    echo "Created user 'geo' with docker access"
fi

# 5. Setup UFW firewall
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (Caddy)
ufw allow 443/tcp   # HTTPS (Caddy)
ufw --force enable
echo "UFW enabled: SSH + HTTP + HTTPS only"

# 6. Create project directory
mkdir -p /opt/geo-insights
chown geo:geo /opt/geo-insights

# 7. Clone repositories (as geo user)
su - geo -c '
    cd /opt/geo-insights
    git clone https://github.com/Geo-insights/sensorthings-testbed-connector.git 2>/dev/null || echo "connector repo already exists"
    git clone https://github.com/Geo-insights/monitoring_module.git 2>/dev/null || echo "monitoring repo already exists"
'

# 8. Copy deployment files
su - geo -c '
    cd /opt/geo-insights
    cp sensorthings-testbed-connector/deploy/docker-compose.yml .
    cp sensorthings-testbed-connector/deploy/Caddyfile .
    cp sensorthings-testbed-connector/deploy/deploy.sh .
    cp sensorthings-testbed-connector/deploy/webhook.py .
    chmod +x deploy.sh
'

# 9. Create .env file templates
su - geo -c '
    cd /opt/geo-insights
    if [ ! -f connector.env ]; then
        echo "# Copy env vars from Render dashboard" > connector.env
        echo "# SENSORTHINGS_BASE_URL=" >> connector.env
        echo "# KAFKA_TGV_BOOTSTRAP_SERVERS=" >> connector.env
        echo "Created connector.env template"
    fi
    if [ ! -f monitoring.env ]; then
        echo "# Copy env vars from Render dashboard" > monitoring.env
        echo "# DATABASE_URL=" >> monitoring.env
        echo "# SUPABASE_URL=" >> monitoring.env
        echo "Created monitoring.env template"
    fi
    chmod 600 connector.env monitoring.env
'

# 10. Setup webhook as systemd service
cat > /etc/systemd/system/geo-insights-webhook.service << 'EOF'
[Unit]
Description=Geo-Insights GitHub Webhook Listener
After=network.target docker.service

[Service]
Type=simple
User=geo
WorkingDirectory=/opt/geo-insights
Environment=WEBHOOK_SECRET=changeme
ExecStart=/usr/bin/python3 /opt/geo-insights/webhook.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "Webhook service installed (edit WEBHOOK_SECRET in /etc/systemd/system/geo-insights-webhook.service)"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Add your SSH key:  ssh-copy-id geo@<this-server>"
echo "  2. Fill in env vars:  ssh geo@<IP> 'nano /opt/geo-insights/connector.env'"
echo "  3. Fill in env vars:  ssh geo@<IP> 'nano /opt/geo-insights/monitoring.env'"
echo "  4. Set webhook secret: edit /etc/systemd/system/geo-insights-webhook.service"
echo "  5. Start webhook:     systemctl enable --now geo-insights-webhook"
echo "  6. Start services:    cd /opt/geo-insights && docker compose up -d"
echo "  7. Add DNS A records: connector.geo-insights.nl + monitor.geo-insights.nl -> <IP>"
echo "  8. Verify:            curl https://connector.geo-insights.nl/health"
echo ""
