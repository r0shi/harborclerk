# Cloud Model Boundaries

Harbor Clerk supports cloud LLMs today through controlled integrations. It does
not need to call a cloud provider from inside Ask or Research for the initial
release.

This distinction matters because Harbor Clerk's trust story is about local
documents, scoped tool access, and citations. Cloud models can be useful
operators over the corpus, but users should always know what leaves the
machine, which interface sent it, and how citations are preserved.

## Current Release

Supported cloud path:

- A cloud LLM or connector calls Harbor Clerk through MCP.
- The connector authenticates with a scoped API key or OAuth flow.
- Harbor Clerk returns tool responses: retrieved snippets, citations, titles,
  folder labels, relative paths, and structured metadata needed for the
  request.
- The provider can see those tool responses and the tool arguments it is asked
  to run.
- The full corpus is not uploaded by Harbor Clerk.

This is the right release path for ChatGPT, Claude, Claude Desktop, Gemini CLI,
and other MCP-capable clients. It keeps cloud usage operator-controlled and
lets the MCP/CLI citation contract remain the source of truth.

## In-App Cloud Mode

In-app cloud mode means Harbor Clerk itself would call a provider from Ask or
Research using a user-supplied provider API key. This could be valuable: it
would combine Harbor Clerk retrieval and first-class citations with stronger
cloud synthesis.

It is also a different product surface from MCP. In-app cloud mode should not
ship until the core path is stable enough that cloud answers are no more
confusing than local answers.

Release go criteria:

- Ask and Research preserve clickable, inspectable citations through the same
  source contract used by search, MCP, and CLI results.
- The model selector clearly distinguishes local AI from cloud AI before the
  user sends a prompt.
- Provider API keys have an acceptable storage path for public release. On
  macOS, prefer Keychain or the existing encrypted-secret machinery; avoid
  surprising plaintext files.
- Each cloud run shows a boundary disclosure: provider, model, prompt/context
  flow, and whether retrieved source snippets are being sent.
- Absolute local filesystem paths are not sent to cloud-visible prompts,
  results, logs, or traces by default.
- Errors, logs, telemetry, and diagnostics do not persist provider keys or full
  prompt/context bodies.
- Invalid keys, rate limits, network loss, cancellation, long-context overflow,
  provider errors, and offline behavior fail gracefully without breaking local
  search or local AI.
- At least one provider path has mocked tests plus a live smoke checklist for
  no-key, invalid-key, successful answer, citation rendering, long context,
  email and attachment citations, cancellation, and offline behavior.

Defer in-app cloud mode if:

- Citation chips depend on parsing the cloud model's prose after the fact.
- "Private AI" copy could be read as applying to cloud runs.
- Provider-key storage is still unsettled.
- Path disclosure is not enforced by tests.
- Cloud failures create confusing Ask or Research states.
- The release story depends on cloud mode to compensate for local model
  quality.

For the initial release, the default decision is to keep cloud model usage in
MCP/connectors and document in-app cloud mode as a fast-follow.

## Copy Rules

Use language like:

- "Cloud LLMs can use Harbor Clerk through MCP and receive scoped, cited tool
  responses."
- "Local AI keeps prompts and retrieved snippets on the machine."
- "Future in-app cloud mode would send the selected prompt and retrieved
  context to the chosen provider."

Avoid language like:

- "All AI is private."
- "Cloud models never see document content."
- "In-app cloud Ask/Research is release-ready" unless the go criteria above
  have been met.
