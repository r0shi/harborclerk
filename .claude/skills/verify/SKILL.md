---
name: verify
description: Run full CI checks locally (Python lint/format + frontend lint/typecheck/prettier). Use before committing or creating PRs.
---

# Verify

Run the full local CI check suite and report results. All commands run from
the repository root.

## Steps

1. Run Python checks:
   ```bash
   uv run ruff check . 2>&1 && uv run ruff format --check . 2>&1
   ```

2. Run frontend checks:
   ```bash
   (cd frontend && npm run lint 2>&1 && npm run type-check 2>&1 && npm run format:check 2>&1)
   ```

3. Report a summary:
   - Python lint: pass/fail (show errors if any)
   - Python format: pass/fail
   - ESLint: pass/fail (show errors, ignore warnings)
   - TypeScript: pass/fail (show errors)
   - Prettier: pass/fail (show files that need formatting)
   - Overall: PASS if all green, FAIL if any errors

CI additionally verifies generated docs (`uv run python -m scripts.gen_docs
--check`) and runs the test suites; run those too when the change touches
tools, routes, models, stages, compose services, or tested code.
