---
name: build-macos
description: Build macOS apps (Harbor Clerk Server + Harbor Clerk) and report results. Optionally launch the server app.
---

# Build macOS Apps

Run `make apps` in `macos/` to build both macOS apps. Parse the output for
build success/failure and report a summary. All paths are relative to the
repository root.

## Steps

1. Run `(cd macos && make apps 2>&1)`
2. Check output for `BUILD SUCCEEDED` or `BUILD FAILED` / `error:` lines
3. Report:
   - Whether each app (HarborClerkServer, HarborClerk) built successfully
   - Any build errors (show the actual error lines)
   - App sizes from the "Server app" / "Client app" lines
4. If the user said "and run" or "and launch", open the server app:
   `open macos/build/output/HarborClerkServer.app`

If the build stops at `configure: error: C compiler cannot create executables`,
the Command Line Tools SDK is newer than Xcode's linker; see the SDKROOT note
in `macos/AGENTS.md`. Signing (`make sign`) is never run by an agent.
