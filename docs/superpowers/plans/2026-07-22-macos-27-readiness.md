# macOS 27 "Golden Gate" Readiness — Harbor Clerk

**Status:** Assessment. Report only, from documentation + this repo's build.
**Date:** 2026-07-22 (public beta live; ships ~Sept 2026).
**Method:** Apple docs / WWDC26 sessions / MDM-vendor recaps (external research,
confidence-tagged) crossed with the actual Apple dependency surface in this repo
and the signatures on the built `.app`. Nothing was run on the beta.

Confidence: **(a)** Apple docs/notes · **(b)** credible devs · **(c)** inference.

---

## Headline — the intuition inverts

The thing that sounds scariest — the Apple Intelligence overhaul churning the
Foundation Models API the app depends on — is the **best-documented and safest**
item. The API surface the app uses (`SystemLanguageModel.default`,
`.availability`/`.isAvailable`, `LanguageModelSession`, `respond(to:)`,
`response.content`) is **confirmed present and not deprecated in macOS 27**; the
overhaul is *additive* (a new `LanguageModel` provider protocol layered on top).
No hard API break, no new entitlement for on-device use. **(a)**

The real exposure is quieter:

1. **Behavioral drift** — a genuinely new on-device model generation (AFM 3 Core)
   ships this cycle. Same API, different output. **(a)** Confirmed the model
   changed; the app-level impact is ours to measure.
2. **Helper-process spawning** under a tightening hardened-runtime/JIT regime —
   the Tika JVM is the exposed one. **(b)** Precedent exists; doc-unresolvable.

Across all seven dependencies: **zero confirmed hard API breaks.** Nothing is
known-broken. This is a "de-risk, don't panic" situation.

---

## Per-dependency

### 1. Foundation Models (`apple-summarize/main.swift`) — API stable; model drifts
- Exact symbols the helper calls are doc-confirmed present, un-deprecated,
  through macOS 27.0. **(a)** So the helper keeps **compiling and running**.
- New **AFM 3 Core** (~3B dense, successor to today's `.default`) is markedly
  better on Apple's own prefs, but output length/style/**refusal behavior**
  change; guardrails "refined to reduce false positives." On-device context
  still ~4K. `.default` keeps returning the small model (the 20B "Core Advanced"
  is opt-in). **(a)/(b)**
- **Impact here:** no crash. Summaries change in style/length/refusal rate;
  prompts in `apple-summarize` tuned against the macOS 26 model may regress.
  **Re-run the eval corpus against the beta model.** Directly relevant to the
  just-filed summary-degradation work (#559 AI→LLM fallback, #560 drop
  extractive): a shifting AFM makes graceful degradation matter more.

### 2. WKWebView loading `http://localhost:8100` — probably fine, critical if not
- WebKit traffic is **exempt from Local Network Privacy**; loopback is not "local
  network" and has been ATS-exempt for cleartext across recent macOS. No 27
  change surfaced. **(b)** established / **(a)** absence.
- **But:** this repo declares **no ATS keys at all** in the client `Info.plist` —
  it free-rides on WebKit's *implicit* localhost allowance, and WKWebView's ATS
  handling for the top-level *document* load is under-documented. LOW likelihood,
  **CRITICAL** blast radius (blank client — the only Mac UI). → #F, and a top
  beta-test item.

### 3. TCC / watched-folder access — probably fine, watch
- No 27-specific new folder-access gate for user-chosen folders found. Security-
  scoped bookmarks + `NSOpenPanel` continue. **(a)** absence / **(c)**.
- The 27 privacy change (PPPC → declarative `Privacy` config) is **MDM-managed**
  territory; unmanaged dev Macs unaffected. **(a)**
- Pre-existing: Apple periodically invalidates bookmarks in point releases — the
  watcher must already tolerate stale bookmarks and re-prompt.

### 4. Code signing / Gatekeeper — mostly pre-existing
- No 27 change removes running Development-signed apps. **(a)** absence.
- **Pre-existing and biting:** an *Apple Development*-signed app only launches on
  Macs registered in its provisioning profile → won't launch on an unregistered
  second machine. The dev DMG is fragile for this reason **today** (already noted
  when the DMG was built). **(c)**
- Apple Silicon requires every executable to be ≥ ad-hoc signed; 27 is all-arm64,
  so **every bundled helper must carry a signature.** → #G.
- New in 27: Endpoint-Security binary allow/deny — **MDM fleets only.**

### 5. Subprocess spawning under hardened runtime — Tika JVM is the sharp edge, but already defended
- Apple is tightening JIT / W+X memory on Apple Silicon across the 26 line into
  27; real reports of V8-based tools crashing (SIGTRAP) until re-signed with JIT
  entitlements. The **Tika JVM (HotSpot)** is a JIT engine → most exposed helper,
  and **Tika is a non-negotiable requirement**. **(b)**
- **Mitigator verified in this build:** the bundled Temurin JRE 21 `java` and
  `jspawnhelper` are **hardened-runtime-signed with `allow-jit`,
  `allow-unsigned-executable-memory`, and `disable-library-validation`** already.
  So the protection the research said matters is present. Residual risk: if 27
  changes the *semantics* of `allow-jit` (the forum thread was literally about
  this), the entitlement's presence may not be sufficient. Downgrades this from
  "probable break" to **"likely fine, verify on beta."**
- llama.cpp Metal compiles shaders in the GPU driver out-of-process → generally
  doesn't need `allow-jit`; less exposed. **(c)**

### 6. Metal / llama.cpp — low risk
- Metal 4 arrived in macOS 26 and is opt-in; the classic `MTLDevice` API
  llama.cpp uses keeps working. No 27 Metal deprecations found. **(a)/(b)**
  Benchmark for perf on the beta; not a correctness worry.

### 7. SDK / deployment target 15.0 — fine, with a real gotcha
- Xcode 27 supports deployment targets 15.0 → 27.0; the 15.0 target is exactly
  the new floor. Forward-compat on 27 holds. **(a)/(c)**
- **Gotcha worth a rule:** many stricter 27 privacy/security defaults are gated
  **"linked-on-or-after" the new SDK.** A binary still built against the 15.x SDK
  is **grandfathered into the looser behavior** — protective. **Rebuilding
  against the 27 SDK is what flips on stricter ATS/TCC/JIT behavior.** So do not
  casually bump the SDK; when you do, re-test #2–#5. **(c)** established pattern.
  (Candidate for `macos/AGENTS.md`.)
- **Rosetta:** fine on 27; macOS 28 (fall 2027) removes it. Any x86_64 helper or
  Python wheel must be arm64 before 28. Audit now, low urgency. → #G.

---

## Ranked risk (adjusted for the in-repo build check)

| # | Risk | Likelihood | Severity | Settles by |
|---|---|---|---|---|
| 1 | AFM 3 behavioral drift → summary quality/refusal change | HIGH | MEDIUM | eval on beta |
| 2 | Tika JVM under 27 JIT tightening | LOW-MED (JRE already allow-jit-signed) | HIGH (Tika required) | beta only |
| 3 | Dev-signed app won't launch on unregistered 2nd Mac; arm64 helpers need signing | MED (pre-existing) | MED-HIGH | build + registration |
| 4 | WKWebView `http://localhost` document load | LOW | CRITICAL | beta only |
| 5 | Watched-folder TCC re-prompt for the background watcher | LOW-MED | HIGH | beta only |
| — | Metal, SDK-15-on-27, Rosetta-on-27 | — | — | probably fine |

Nothing is confirmed-broken. Items 2/4/5 are **doc-unresolvable** — only the beta
settles them.

---

## Beta verdict

**Yes, install it on a secondary machine — moderate urgency, "de-risk" not
"fire-drill."** Docs already settled everything docs *can* settle (FM API,
Metal, SDK range). The beta buys the doc-unresolvable answers, in priority order:

1. Does the **Tika JVM** spawn and run inference under 27? (gates a required
   subsystem; the one with real crash precedent)
2. Does the **WKWebView load `http://localhost:8100`** with no prompt/block?
   (binary pass/fail, critical blast radius)
3. Do **Postgres / llama-server / bundled Python** all spawn from the menubar
   app? (signature/quarantine)
4. Does the **background watcher** read folders without new TCC prompts?
5. How does **AFM 3 Core** score on the eval corpus vs the macOS 26 baseline?
   (highest long-term value — but needs a fresh baseline first; the last local
   sweep is ~8 weeks old, see the model survey)

**Two practical constraints on the test rig:**

- The app is **Development-signed**, so the beta Mac must be **registered in the
  provisioning profile** (or use a Developer-ID build) or it won't launch — risk
  #3 bites before any other test can run.
- **The Mac mini is already spoken for** by the dogfooding field investigation
  (#552–557, the field handoff). Putting a beta on it conflates "is this bug ours
  or the OS's." **Sequence it:** finish the field investigation on the stable OS
  first, *then* upgrade the mini to the beta — or use a separate machine / VM for
  the beta so the two efforts don't contaminate each other.

---

## Not verified

- Whether `SystemLanguageModel()` direct-init supersedes `.default` on a quiet
  deprecation path (docs show both; sessions foreground the new init). **(c)**
- Exact 27 `Availability` enum cases vs 26 (inferred, not diffed). **(c)**
- Whether on-device context is *exactly* 4K in 27 (dev-reported). **(b)**
- Any 27-specific ATS/WebKit cleartext change (absence across sources, not a
  positive "unchanged"). **(a-absence)**
- Whether *these specific* bundled binaries crash under 27 — the JIT tightening
  is real in the 26 line; no 27 test of them exists. **(b)** → the whole reason
  to run the beta.
- Apple's own prose 27 security release notes (JS-rendered SPA, unreadable to the
  research tool; relied on the doc JSON endpoint + MDM-vendor recaps).
