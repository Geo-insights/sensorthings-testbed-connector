"""Lightweight GitHub webhook listener for auto-deploy.

Listens on port 9000 for GitHub push events, verifies the webhook secret,
and runs deploy.sh. Runs as a systemd service.

Setup:
    export WEBHOOK_SECRET="your-github-webhook-secret"
    python3 webhook.py
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
DEPLOY_SCRIPT = "/opt/geo-insights/deploy.sh"
PORT = 9000

# Map repo names to deploy targets
REPO_MAP = {
    "sensorthings-testbed-connector": "connector",
    "monitoring_module": "monitoring",
}


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify signature
        if WEBHOOK_SECRET:
            signature = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET, body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Invalid signature")
                return

        # Parse event
        event = self.headers.get("X-GitHub-Event", "")
        if event != "push":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Ignored (not a push event)")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        ref = payload.get("ref", "")
        repo_name = payload.get("repository", {}).get("name", "")

        # Only deploy on push to main
        if ref != "refs/heads/main":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Ignored (ref={ref}, not main)".encode())
            return

        target = REPO_MAP.get(repo_name, "all")
        print(f"Deploying {target} (repo={repo_name}, ref={ref})")

        # Run deploy.sh in background
        subprocess.Popen(
            [DEPLOY_SCRIPT, target],
            stdout=open("/var/log/geo-insights-deploy.log", "a"),
            stderr=subprocess.STDOUT,
        )

        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"Deploy triggered: {target}".encode())

    def log_message(self, format, *args):
        print(f"{self.log_date_time_string()} {format % args}")


if __name__ == "__main__":
    if not WEBHOOK_SECRET:
        print("WARNING: WEBHOOK_SECRET not set, signature verification disabled")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    print(f"Webhook listener on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
