---
name: build-macos
description: Build macOS apps (Harbor Clerk Server + Harbor Clerk) and report results. Optionally launch the server app.
---

# Build macOS Apps

Run `cd macos && make apps` to build both macOS apps. Parse the output for build success/failure and report a summary.

## Steps

1. Run `cd /Users/alex/mcp-gateway/macos && make apps 2>&1`
2. Check output for `BUILD SUCCEEDED` or `BUILD FAILED` / `error:` lines
3. Report:
   - Whether each app (HarborClerkServer, HarborClerk) built successfully
   - Any build errors (show the actual error lines)
   - App sizes from the "Server app" / "Client app" lines
4. If the user said "and run" or "and launch", open the server app:
   `open /Users/alex/mcp-gateway/macos/build/output/HarborClerkServer.app`
