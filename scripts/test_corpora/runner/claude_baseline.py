# scripts/test_corpora/runner/claude_baseline.py
"""Back-compat shim — the real code lives in providers/anthropic_provider.py.

`BaselineGenerator` is exported as an alias for `AnthropicProvider` so that
`sweep.py`'s 30+ import sites (and `tests/test_baseline.py`) continue to
work unchanged. A follow-up PR can migrate `sweep.py` to use
`make_provider()` directly; that's a larger blast radius — deferred.

See docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md
for the design rationale.
"""

from __future__ import annotations

from scripts.test_corpora.runner.providers.anthropic_provider import (
    AnthropicProvider as BaselineGenerator,
)
from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT as SYSTEM_PROMPT,
)
from scripts.test_corpora.runner.providers.base import (
    BaselineResult,
)

__all__ = ["BaselineGenerator", "BaselineResult", "SYSTEM_PROMPT"]
