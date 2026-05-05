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

# All eight downloaded models, by Harbor Clerk model_id. Phase 4 sweeps over
# this list. Phase 5 uses TOP_MODELS only.
ALL_MODELS = [
    "qwen3-8b",
    "qwen3-4b",
    "phi-4-mini",
    "deepseek-r1-8b",
    "gemma-26b",
    "smollm3-3b",
    "gpt-oss-20b",
    "qwen3.6-35b",
]

# Two largest by parameter count. Used for Phases 5 and 6 parity comparison.
TOP_MODELS = ["qwen3.6-35b", "gemma-26b"]

DEPTHS = ["light", "standard", "thorough"]
DEFAULT_DEPTH = "standard"
DEFAULT_TIME_LIMIT_SECONDS = 30 * 60  # 30 minutes
SAMPLE_EVERY_N = 5  # in-flight stdout sampling cadence

JUDGE_MODEL = "claude-sonnet-4-6"
BASELINE_MODEL = "claude-sonnet-4-6"

WORKDIR_DEFAULT = Path("~/Library/Application Support/Harbor Clerk/test-corpora").expanduser()
RESULTS_DIR_NAME = "results"
