"""Shared configuration for the test-corpora sweep harness.

Imported by both the runner and the tests. Single source of truth for
constants like the API base URL and the model list. Override via env
vars or CLI flags in sweep.py.
"""

from __future__ import annotations

import os
from pathlib import Path

# Default targets the macOS native Harbor Clerk Server on its default api_port.
# Override via HC_API_BASE if you've changed the port (Preferences → API port)
# or if you're running Harbor Clerk via Docker Compose (use "https://localhost"
# and pass --insecure on sweep invocations to tolerate Caddy's self-signed cert).
API_BASE = os.environ.get("HC_API_BASE", "http://localhost:8100")
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# The curated downloaded models, by Harbor Clerk model_id. These MUST match
# the canonical ids in src/harbor_clerk/llm/models.py — anything else 404s on
# PUT /api/chat/models/<id>/activate. Phase 4 sweeps over this list. Phase 5
# uses TOP_MODELS only. The list shrank from 8 to 6 in PR #434 when phi4-mini
# and smollm3-3b were dropped after the 2026-05-29 sweep showed both well
# below the quality threshold.
ALL_MODELS = [
    "qwen3-8b",
    "qwen3-4b",
    "gemma4-26b-a4b",
    "gpt-oss-20b",
    "qwen36-35b-a3b",
]

# Two largest by parameter count. Used for Phases 5 and 6 parity comparison.
TOP_MODELS = ["qwen36-35b-a3b", "gemma4-26b-a4b"]

DEPTHS = ["light", "standard", "thorough"]
DEFAULT_DEPTH = "standard"
DEFAULT_TIME_LIMIT_SECONDS = 30 * 60  # 30 minutes
SAMPLE_EVERY_N = 5  # in-flight stdout sampling cadence

JUDGE_MODEL = "claude-sonnet-4-6"
BASELINE_MODEL = "claude-sonnet-4-6"

WORKDIR_DEFAULT = Path("~/Library/Application Support/Harbor Clerk/test-corpora").expanduser()
RESULTS_DIR_NAME = "results"
