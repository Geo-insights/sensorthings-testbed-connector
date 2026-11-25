"""Test-suite wide setup.

Runs before any test module imports ``app.config``: keep the developer's .env
(real FROST URLs, credentials, Kafka device mappings) out of the tests, so the
suite behaves the same in the main checkout as in a clean worktree or CI.
"""

from __future__ import annotations

import os

os.environ["CONNECTOR_SKIP_DOTENV"] = "1"
