# Email ingestion via IMAP labels — design spec

**Date:** 2026-05-04
**Status:** Spec — implementation deferred (decomposed into four stages, each its own PR)
**Related:** Legacy `/api/uploads*` endpoints (intentionally retained as the substrate this spec finally consumes); the watched-folder model in [docs/superpowers/specs/2026-04-05-watched-folders-design.md](2026-04-05-watched-folders-design.md), which this design parallels for the email side; PR #279 (download UI feature-detect, which email docs inherit for free)

## Goal

Let a non-technical office wire up live email accounts to Harbor Clerk so that selected emails (and their attachments) are automatically ingested, indexed, and searchable alongside watched-folder documents — without ever pointing the appliance at a 50 GB Gmail account.

The MVP focuses on Gmail with a single auth method (app passwords + IMAP), generalized to any IMAP host, with Outlook/M365 deferred until OAuth (XOAUTH2) is added in a follow-up.

## Why this is needed

Today, Harbor Clerk has exactly one user-facing ingest path: drop a file in a watched folder. Knowledge that arrives by email — contracts, meeting notes, vendor agreements, decisions — only enters the corpus if a user manually exports it as `.eml` or saves the attachment to a watched folder. That's friction the modal user won't pay.

Email is also fundamentally different from filesystem state: it lives on a remote server, it has rich structured metadata (sender, recipient, subject, thread, send-date) that's load-bearing for search relevance, and "the file" can have several distinct knowledge artifacts attached to it.

The legacy `/api/uploads*` endpoints have been deliberately preserved in the codebase since the watched-folder-first refactor specifically to anchor this work — a non-interactive ingest path that bypasses the watcher. This spec is what those endpoints have been waiting for.

## Decisions log

The decisions below were settled in the brainstorm and are recorded here so the implementation plan and any future revisits don't re-litigate them.

| # | Decision | Rationale |
|---|---|---|
| Q1 | **User owns the mailbox** (not the appliance). App reads from the user's existing Gmail/iCloud/etc. via OAuth-or-app-password. The "appliance has its own mailbox, user forwards to it" model is parked. | No mailbox to provision; "create a label, drag emails in" matches workflows non-technical staff already understand. |
| Q2 | **IMAP + app password for MVP.** XOAUTH2 (and therefore Outlook/M365) parked for a follow-up. | Avoids the OAuth-client-registration burden entirely; works with Gmail today; generalizes to iCloud/Fastmail/Yahoo for free. The risk that Google deprecates app passwords is acknowledged but not designed around. |
| Q3 | **Each attachment is its own Document; email body is its own Document.** Linked via Message-ID and `email_parent_doc_id`. | Composes cleanly with the existing extract→chunk→embed pipeline (each attachment is just bytes with a mime type, exactly like a watched-folder file). Search and citation get the right semantics — operators want "the contract", not "the email someone sent the contract in". |
| Q4 | **Three-table data model**: `mail_accounts` (one per IMAP connection) → `watched_labels` (one per (account, label) pair, the unit users add/remove) → `watched_messages` (analog of `watched_files`, keyed by Message-ID). | Mirrors the existing `watched_folders` / `watched_files` shape. Credentials live once even when watching multiple labels in the same account. |
| Q5 | **Master-key envelope encryption.** Postgres holds Fernet ciphertext; the master key lives in macOS Keychain (Swift reads it once, passes to Python via `NATIVE_CONFIG_FILE`) or in `HARBOR_CLERK_MASTER_KEY` env (Docker). Every ciphertext is tagged with a key fingerprint so cross-deployment moves degrade gracefully. | One code path for secret persistence; Keychain integration is one read at startup, not one-per-process; backup/restore of Postgres still includes (encrypted) secrets. |
| Q6 | **IMAP IDLE with 2-minute poll fallback**, per-label `(uidvalidity, last_uid_seen)` cursor. Lifecycle = soft-delete via the existing 30-day reaper when messages leave watched labels. | IDLE is near-real-time; one socket per watched label is negligible at office scale; reuses the same reaper logic the watched-folder side already has. |
| — | **No new MCP tools for MVP.** Email docs flow through `kb_search` naturally; sender names appear as PERSON entities via spaCy, so `kb_entity_search` covers "docs from Alice". | Wait for real usage to tell us if dedicated `kb_email_search` (with from/to/date filters) is needed. |

## Architecture overview

Three new subsystems and one schema extension:

1. **Secrets / encryption helper** (`harbor_clerk/secrets/`) — Fernet envelope encryption, master-key sourcing from Keychain (macOS) or env (Docker), key fingerprinting for cross-deployment portability. Becomes the project's secrets primitive — usable for any future encrypted config (OAuth refresh tokens, off-host MinIO keys, etc.), not just mail.

2. **IMAP sync engine** (`harbor_clerk/mail/`) — IMAP client wrapper, label discovery, IDLE supervision with poll fallback, cursor management, lifecycle event detection. Runs as a new subsystem inside the existing `harbor-clerk-watcher` daemon, alongside the filesystem `Observer`. Same process, same supervision tree, same SSE event channel for UI updates.

3. **Email→Document conversion** (`harbor_clerk/mail/parser.py`) — RFC 5322 parsing into one email Document plus N attachment Documents, original-byte persistence through the existing storage backend, hand-off into the existing extract/chunk/embed pipeline. No changes needed to the seven pipeline stages — Tika already handles `message/rfc822`, and attachments are just bytes with mime types the pipeline already understands.

4. **Schema extension** — three new tables (`mail_accounts`, `watched_labels`, `watched_messages`) plus nullable email-metadata columns on `documents`.

The four PR stages (Foundation → Sync engine → Pipeline → UI) are described in *Decomposition* below.

## Data model

### New tables

```sql
CREATE TABLE mail_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT NOT NULL,
    provider TEXT NOT NULL,                    -- 'gmail' | 'icloud' | 'fastmail' | 'yahoo' | 'generic'
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL DEFAULT 993,
    imap_username TEXT NOT NULL,               -- usually the email address
    app_password_ciphertext BYTEA NOT NULL,    -- Fernet-encrypted
    key_fingerprint BYTEA NOT NULL,            -- 8 bytes; identifies which master key encrypted this row
    status TEXT NOT NULL DEFAULT 'active',     -- 'active' | 'auth_error' | 'key_mismatch' | 'paused'
    last_error TEXT,
    last_connected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (imap_host, imap_username)
);

CREATE TABLE watched_labels (
    label_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES mail_accounts(account_id) ON DELETE CASCADE,
    label_path TEXT NOT NULL,                  -- IMAP folder path, e.g. 'Clerk' or 'Clerk/Contracts'
    display_name TEXT NOT NULL,                -- user-friendly name for the UI
    uidvalidity BIGINT,                        -- IMAP UIDVALIDITY of the label; reset triggers full rescan
    last_uid_seen BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',     -- 'active' | 'paused' | 'error'
    last_error TEXT,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, label_path)
);

CREATE TABLE watched_messages (
    message_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label_id UUID NOT NULL REFERENCES watched_labels(label_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,                  -- RFC 5322 Message-ID; falls back to synthesized hash if absent
    imap_uid BIGINT NOT NULL,
    eml_sha256 BYTEA NOT NULL,                 -- dedup key for re-labeling churn
    email_doc_id UUID REFERENCES documents(doc_id),  -- NULL until extract begins
    status TEXT NOT NULL DEFAULT 'active',     -- 'active' | 'unlabeled' | 'reaped'
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    unlabeled_at TIMESTAMPTZ,                  -- when message left the label; reaper triggers 30 days later
    UNIQUE (label_id, message_id)
);

CREATE INDEX watched_messages_label_status ON watched_messages (label_id, status);
CREATE INDEX watched_messages_eml_sha ON watched_messages (eml_sha256);
```

### Document extensions

```sql
ALTER TABLE documents
    ADD COLUMN email_message_id TEXT,
    ADD COLUMN email_thread_id TEXT,
    ADD COLUMN email_parent_doc_id UUID REFERENCES documents(doc_id),
    ADD COLUMN email_from_address TEXT,
    ADD COLUMN email_from_name TEXT,
    ADD COLUMN email_to_addresses TEXT[],
    ADD COLUMN email_cc_addresses TEXT[],
    ADD COLUMN email_date_sent TIMESTAMPTZ,
    ADD COLUMN email_label_path TEXT;          -- denormalized; watched_messages is source of truth

CREATE INDEX documents_email_message_id ON documents (email_message_id) WHERE email_message_id IS NOT NULL;
CREATE INDEX documents_email_thread_id ON documents (email_thread_id) WHERE email_thread_id IS NOT NULL;
CREATE INDEX documents_email_parent ON documents (email_parent_doc_id) WHERE email_parent_doc_id IS NOT NULL;
```

For email Documents, `documents.created_at` is set to `email_date_sent` (not the ingest time) so the Documents page sorts by message date naturally. Attachment Documents inherit the parent email's `email_date_sent` for the same reason.

### Storage

Email originals (raw `.eml`) and detached attachment bytes go through the existing `StorageBackend` abstraction at `originals/<doc_id>/<filename>`. macOS deployments use the filesystem backend; Docker uses MinIO. Identical to how legacy uploads are stored — no new infrastructure.

`documents.source_path` stays NULL for both email Documents and attachment Documents — they don't have a filesystem path. This is what makes the existing Reveal-in-Finder gate work automatically (see *Reveal / Download* below).

## Secrets, encryption, and Keychain

### Master key sourcing

| Platform | Source | First-launch bootstrap |
|---|---|---|
| macOS native | `com.bitblot.harborclerk.master-key` in user's login Keychain. Swift reads it at startup; passes to all Python subprocesses via `NATIVE_CONFIG_FILE`. | Swift Harbor Clerk Server generates a 32-byte key on first launch and stores it. |
| Docker | `HARBOR_CLERK_MASTER_KEY` env var (base64-encoded 32 bytes). | Operator generates and stores via their existing Compose secrets workflow. |

Critically, **only Swift talks to Keychain**. The four Python processes (api, worker-io, worker-cpu, watcher) all read the master key from the config they already receive — no per-process Keychain authorization dance.

### Fingerprinting

Every encrypted blob is paired with `key_fingerprint = HMAC-SHA256(key, "harbor-clerk-master-key-fingerprint")[:8]`. This identifies *which* master key was used to encrypt without leaking the key itself.

### Decryption flow

```
def decrypt(ciphertext, stored_fingerprint):
    if stored_fingerprint != current_key_fingerprint:
        raise KeyMismatch(account_id)        # surfaced as account.status='key_mismatch'
    return Fernet(current_key).decrypt(ciphertext)
```

`KeyMismatch` is caught by the sync engine, the account is marked `key_mismatch`, and System Status surfaces a "reconnect mail accounts" banner — same UI affordance the auth-error path uses.

### Cross-deployment portability

| Scenario | UX |
|---|---|
| Docker DB restored to macOS, fresh Keychain | Status banner: "3 mail accounts need reconnecting." User clicks each → re-pastes app password. |
| macOS DB exported to Docker without master key | Same banner, same fix. |
| Either DB moved with master key carried over | Nothing — accounts just work. (This is the happy migration path.) |
| Keychain wiped on the same Mac | Same banner, same fix. |

**Optional escape hatch (stage 4):** an admin-only "Import master key" form in System Settings → Security. Paste a base64 32-byte key; it's written to Keychain (macOS) or echoed back as the env var to set (Docker, where we can't persist for the user). Useful for migrating into the appliance from another deployment to avoid re-entering N app passwords. Not load-bearing — the re-entry path is always available.

### Non-secret DB portability

Untouched. A `pg_dump` from Docker restored to Mac brings over all documents, chunks, embeddings, search history, and audit logs without ceremony. Only the encrypted secret columns require the fingerprint dance.

## IMAP sync engine

### Process model

The sync engine is a new subsystem inside the existing `harbor-clerk-watcher` daemon, mounted alongside the filesystem `Observer`. Same process, same lifecycle, same supervision:

```
harbor-clerk-watcher
├── filesystem Observer (existing) — watchdog/FSEvents/inotify
└── mail Observer (new)
    ├── per-account connection pool (one IMAP TCP per account)
    └── per-label IDLE supervisor (one IDLE per watched_label.label_id)
```

Connection counts at office scale: ≤3 accounts × ≤10 labels = ≤30 sockets. Negligible.

### Per-label state machine

```
[INITIAL_SYNC]
    Connect → SELECT label → fetch (UID 1:*) → ingest each → record uidvalidity, last_uid_seen
    ↓
[IDLE]
    Issue IDLE; wait for EXISTS / EXPUNGE / FETCH notifications.
    On notification: SELECT, fetch (UID last_uid_seen+1:*), ingest each, update last_uid_seen.
    On disconnect: reconnect with backoff; if 5 consecutive failures, → ERROR.
    On UIDVALIDITY change: → INITIAL_SYNC.
[ERROR]
    Stop polling. Mark watched_label.status='error', last_error=...
    Recovery: user-triggered "reconnect" button in /folders, or admin-triggered rescan.
[PAUSED]
    User-toggled. Connection released; cursor preserved. Resume returns to IDLE.
```

### Polling fallback

If the IMAP server doesn't advertise IDLE (uncommon for the providers we care about), or IDLE drops repeatedly, fall back to a 2-minute poll: SELECT, fetch (UID last_uid_seen+1:*), update cursor.

### Cursor semantics

`last_uid_seen` is monotonic per `(account, label, uidvalidity)`. UIDVALIDITY change forces full rescan because IMAP UIDs are no longer comparable across UIDVALIDITY epochs. UIDVALIDITY changes are rare on Gmail (effectively never in normal operation) but possible on other providers after server-side moves; the rescan is dedup-safe via the `eml_sha256` check.

### Lifecycle events

| IMAP event | Action |
|---|---|
| New message in label | Ingest path: pull `.eml`, parse, save originals, create email Document + N attachment Documents, enqueue extract on each. |
| Message removed from label (still exists in account, just unlabeled) | `watched_messages.status='unlabeled'`, set `unlabeled_at=now()`. The 30-day soft-delete reaper (already used for watched files) sees this and reaps both the email Document and its attachment Documents on schedule. |
| Message permanently deleted in account | Indistinguishable from "unlabeled" via IMAP. Same handling. |
| Message edited (rare; some clients can resend) | Different SHA → re-ingest path. Same SHA → no-op via existing dedup. |
| Whole label deleted server-side | Mark all child `watched_messages` as removed (sets `unlabeled_at`); the `watched_labels` row stays so the user can see what happened in the UI and remove it explicitly. |
| User pauses or removes the watched label in the UI | All child Documents stay forever. They're already in the corpus; removing the label just stops future syncs. |

### Re-labeling churn and multi-label messages

A user un-labels then re-labels a message in the *same* label: the second labeling re-runs the ingest path, but the `eml_sha256` matches the existing `watched_messages` row, so we restore `status='active'`, clear `unlabeled_at`, and the existing email Document and attachment Documents are reused. Effectively free.

A message that lives in *two* watched labels at once (Gmail's native multi-label model) gets *two* `watched_messages` rows — one per `(label_id, message_id)` pair — but both rows point to the *same* `email_doc_id`. The `eml_sha256` index is the discovery mechanism: when ingesting a new message in label B, the sync engine first checks whether any existing `watched_messages` row already has the same `eml_sha256`; if so, it reuses that row's `email_doc_id` rather than creating a duplicate Document. The downstream effect: search returns the email exactly once, but UI surfaces (the document detail page, the per-label drawer) can show all the labels it appears in.

A consequence for the denormalized `documents.email_label_path` field: when a message is in multiple labels, this field holds the *first-seen* label only. Filtering by label in the UI uses the `watched_messages` join, not this field.

### Per-account error budget

Auth failure (wrong app password, account locked, app passwords disabled) → `mail_accounts.status='auth_error'`, `last_error=<reason>`, polling stops for that account, surfaced in System Status. Doesn't crash the watcher; doesn't retry-forever. User-triggered reconnect resumes.

## Email → Document conversion

### Per inbound message

1. **Pull the raw `.eml`** via IMAP `FETCH UID BODY[]`.
2. **SHA256 the bytes** → `eml_sha256`. Dedup check against `watched_messages` for this label.
3. **Parse with `email.parser.BytesParser`** (stdlib). Extract:
   - Message-ID (fall back to `sha256(date + from + subject)` synthesized hash if absent — broken senders exist)
   - Subject, From, To, Cc, Date, Thread (Gmail's `X-GM-THRID` if present, else IMAP THREAD)
   - Body (prefer `text/plain` part, fall back to `text/html` → strip via `html2text`)
   - Attachment parts (anything with `Content-Disposition: attachment`)
4. **Save originals** to storage:
   - `originals/<email_doc_id>/<safe_subject>.eml`
   - `originals/<attachment_doc_id>/<original_filename>` for each attachment
5. **Create the email Document**:
   - `title` = subject (or `(no subject)`)
   - `email_message_id`, `email_from_*`, `email_to_addresses`, `email_cc_addresses`, `email_date_sent`, `email_thread_id`, `email_label_path`
   - `created_at` = `email_date_sent` (not ingest time)
   - `original_object_key` = the `.eml` storage key
   - Mime type: `message/rfc822`
6. **Create attachment Documents** (one per attached part):
   - `title` = attachment filename
   - `email_parent_doc_id` = email Document's id
   - `email_message_id` = same as parent (for cross-references)
   - `created_at` = parent's `email_date_sent`
   - `original_object_key` = the attachment's storage key
   - Mime type: from the part's `Content-Type`
7. **Enqueue extract** on each new Document. Tika handles `message/rfc822` for the email body and the attachment mime types as-is. The existing seven-stage pipeline runs unchanged.

### Inline images

Treated as noise for MVP. Only parts with `Content-Disposition: attachment` become attachment Documents. Inline images (signatures, embedded screenshots) are ignored.

> **Future work:** validate the "inline images are mostly noise" assumption empirically. Some workflows (design reviews, screenshot-driven bug reports) may genuinely need them ingested. If we add support, the boundary is whether to lift the `Content-Disposition: attachment` filter to also accept `Content-ID`-referenced inline parts above some size threshold.

### Empty-body emails

Still create the email Document. The summarizer will generate a low-value summary; relevance ranking will naturally bury empty-body emails. We don't try to be clever about "see attached" detection.

### Subject hygiene

Subject text is used both as `documents.title` and (sanitized) as the storage filename for the `.eml`. Sanitization: replace `/`, `\`, control chars with `_`; truncate to 200 chars; preserve Unicode.

## UI surface

### `/folders` page

The existing Folders page gains a second section beneath the filesystem-folders section:

```
Watched folders                              [Add folder]
─────────────────────────────────
📁 Contracts        12 files    Active
📁 Meeting notes     8 files    Active

Email                                        [Add email source]
─────────────────────────────────
✉️  alex@gmail.com / Clerk           23 messages    Active
✉️  alex@gmail.com / Clerk/Contracts   5 messages    Active
✉️  legal@firm.com / Inbox             ⚠ Auth error
```

Each `watched_label` row shows: account email, label path, message count, status (Active / Paused / Auth error / Key mismatch). Click → drawer with details, pause/resume/remove controls, and recent-message log.

### "Add email source" wizard

Four-step modal, mirrors the spirit of the watched-folder add flow:

1. **Pick provider.** Five tiles: Gmail · iCloud · Fastmail · Yahoo · Other IMAP. Outlook deliberately absent (would mislead users into thinking app-passwords work; comes back when XOAUTH2 ships).
2. **Enter address + app password.** With a "How do I get an app password?" link to the provider's docs (URLs in the table earlier in this spec). For "Other IMAP", also asks for host/port.
3. **Test connection.** Backend attempts IMAP login. Success → proceed. Failure → show server's error, let user fix.
4. **Pick labels.** Multi-select tree of every label the IMAP server returns via `LIST "" "*"`. Nested labels render as a tree (`Clerk/Contracts` is a child of `Clerk`). System folders (`[Gmail]/All Mail`, `[Gmail]/Trash`, `INBOX`) are shown but greyed-with-warning — picking `INBOX` on a 50 GB account is exactly the foot-gun this whole design is meant to prevent. User confirms; one `watched_labels` row per ticked label.

### Document detail page

For email and attachment Documents, the existing `SourceFileSection` does the right thing automatically because `source_path` is NULL: no Reveal-in-Finder button. A new **email-metadata block** appears above the page content:

```
From: Alice Anderson <alice@firm.com>
To:   alex@gmail.com, bob@firm.com
Cc:   legal@firm.com
Date: 2026-04-30 14:23
Subject: Q3 Vendor Agreement — please review

[View in Gmail ↗]    [Download .eml]
```

For attachment Documents, the metadata block also includes a "Part of email: <subject>" link to the parent email's Document.

### Reveal / Download / Open in Gmail

| Affordance | Watched-folder doc | Email doc | Attachment doc |
|---|---|---|---|
| Reveal in Finder (macOS) | Yes (when `revealInFinder` bridge present) | No (`source_path` is NULL — auto-hidden) | No (same) |
| Download | Yes (gated by `ALLOW_SOURCE_DOWNLOAD`) | Yes (downloads `.eml`, gated by same flag) | Yes (downloads attachment bytes, gated by same flag) |
| View in Gmail ↗ | n/a | Yes, when `mail_account.provider == 'gmail'` AND `email_message_id IS NOT NULL` | Yes, same condition — opens the parent email |

**View in Gmail** constructs:

```
https://mail.google.com/mail/u/?authuser={url-encoded mail_account.imap_username}#search/rfc822msgid:{url-encoded message-id}
```

`authuser=` is the *watched account's* own address (i.e., `mail_accounts.imap_username`), not the email's sender. This ensures Gmail selects the right logged-in account when the user has multiple, and that the search runs in a mailbox that actually contains the message. No equivalent affordance exists for iCloud Mail / Fastmail / Yahoo (no documented deep-link search operators), so it's Gmail-only — graceful absence for other providers.

The download UI for email and attachment docs is gated by `ALLOW_SOURCE_DOWNLOAD` exactly like watched-folder downloads. The recently-shipped frontend feature-detection ([PR #279](https://github.com/r0shi/harborclerk/pull/279)) hides the button when the flag is off — email docs inherit this for free.

### System Settings

- **Status** page gains "Mail accounts" section showing per-account connection state, last sync, error counts.
- **Security** page gains "Encryption" section: surface "X mail accounts need reconnecting" banner when key-mismatch is detected; offer the optional "Import master key" form.

## MCP surface

**No new tools for MVP.** Email Documents flow through the existing 16 tools naturally:

- `kb_search` returns email and attachment Documents alongside watched-folder Documents. Email metadata is included in the result payload so the calling LLM can cite "email from Alice Anderson on 2026-04-30 with subject 'Q3 Vendor Agreement'".
- Sender names are extracted as PERSON entities by spaCy NER during the existing `entities` stage, so `kb_entity_search` handles "find docs from Alice" without any email-specific code.
- `kb_get_document` and `kb_read_document` include the email metadata block in their output for both email and attachment Documents.
- `kb_list_recent` orders by `created_at` which (per the data-model decision) is set to `email_date_sent`, so emails appear in chronological send order — not ingest order — when listed alongside other docs.

A dedicated `kb_email_search` tool with from/to/date/thread filters is parked for after MVP. Wait for real usage to tell us the existing tool surface isn't enough.

## Decomposition

Four PRs, each independently shippable. Stages 1 and 2 are headless (no UI affordance, no end-user ingest); they exist to land foundational primitives that are valuable on their own. Stage 3 closes the ingest loop. Stage 4 makes it accessible.

### Stage 1 — Foundation (schema + secrets + Keychain bootstrap)

- Alembic migration adding `mail_accounts`, `watched_labels`, `watched_messages`, the email-metadata columns on `documents`, and the `key_fingerprint` columns on any encrypted column.
- `harbor_clerk/secrets/cipher.py` — Fernet encrypt/decrypt with fingerprinting.
- `harbor_clerk/secrets/keysource.py` — `EnvKeySource` (Docker, reads `HARBOR_CLERK_MASTER_KEY`) and `ConfigFileKeySource` (macOS, reads from the config Swift writes).
- macOS Swift Harbor Clerk Server: Keychain bootstrap on first launch, key passed into Python subprocesses' config.
- Tests: encrypt/decrypt round-trip, fingerprint determinism, `KeyMismatch` raised on fingerprint mismatch.

**Shippable on its own** as the project's secrets-encryption primitive — usable for any future encrypted config.

### Stage 2 — IMAP sync engine (headless)

- `harbor_clerk/mail/imap_client.py` — async IMAP wrapper (`aioimaplib` for IDLE; thin wrapper).
- `harbor_clerk/mail/sync.py` — per-label state machine, IDLE supervisor, poll fallback, cursor management.
- `harbor_clerk/watcher/mail_observer.py` — integration into the existing watcher daemon.
- `harbor_clerk/api/routes/mail.py` — admin endpoints for create/list/test mail accounts, list/add/remove watched labels, manual rescan. No UI yet — purely API-driven, exercised via tests.
- Tests: integration against a Dovecot-in-Docker fake IMAP server (similar shape to existing Tika integration tests). Cursor advancement, UIDVALIDITY rescan, IDLE drop + reconnect.

**Shippable on its own** as a headless sync engine. Operators can hand-craft `mail_accounts` rows via API and watch the engine populate `watched_messages`. Documents are not yet created.

### Stage 3 — Email → Document pipeline

- `harbor_clerk/mail/parser.py` — `.eml` parsing into email + attachment Document specs.
- Wiring from `watched_messages` insertion → Document creation → existing extract pipeline.
- Lifecycle handlers: unlabeled → `watched_messages.status` transition + Document soft-delete via existing reaper.
- Per-account-error and per-label-error handling.
- Tests: a curated `.eml` test corpus (multipart, encoded headers, inline-image-bearing, attachment-only, threaded conversations) following the [test-corpora pattern from PR #276](https://github.com/r0shi/harborclerk/pull/276).

**Shippable on its own** — operators with API access can now drive end-to-end ingest. Still no UI.

### Stage 4 — UI

- `/folders` page email section.
- "Add email source" four-step wizard with provider presets.
- Document detail page: email-metadata block, View-in-Gmail deep link.
- System Settings → Security: encryption status, "Import master key" affordance.
- System Settings → Status: per-account / per-label connection state.

**This is the user-facing release.** End-to-end "office staff opens Harbor Clerk, adds Gmail account, picks 'Clerk' label, drags email into label in Gmail, sees it in Documents" works.

## Out of scope / future work

- **Outlook / Microsoft 365 support.** Requires XOAUTH2 (basic auth disabled). Belongs in a follow-up that adds OAuth client management for both Microsoft Entra and Google Cloud (the latter as a path forward when/if Google deprecates app passwords).
- **Inline-image ingestion.** Treated as noise in MVP. Empirical research needed: are inline images actually noise, or do some workflows depend on them? If the latter, lift the `Content-Disposition: attachment` filter conditionally.
- **App-owned mailbox / forwarding model** ("Forward to clerk@firm.com"). Q1 alternative — parked. Could revisit if real users find label-tagging too high-friction.
- **`kb_email_search` MCP tool** with from/to/date/thread filters. Wait for usage signal.
- **Local mbox file ingestion.** `.eml` already works through the existing watcher. mbox itself (concatenated emails) requires a splitter. Skip unless a user asks.
- **Per-message attachment-stripping.** Some users may want "ingest the email but don't store the 50 MB PDF". Not in MVP.
- **Email reply / send.** Hard no — Harbor Clerk is read-only. The MCP surface is read-only and that's a load-bearing security property.
- **Gmail Pub/Sub push notifications.** Overkill for a single-tenant local appliance. IDLE is fine.
- **Per-label ingest filters** ("only emails from these senders", "only emails larger than X"). Adds surface area; Gmail filter rules can do this server-side already.

## Risks

- **Google deprecates app passwords.** Has been "considered" by Google for years without action; if it lands, MVP becomes a hard wall and we have to ship XOAUTH2 fast. Mitigation: stage 1's secrets primitive is OAuth-token-ready; the UI wizard is structured so adding a "Sign in with Google" tile alongside the app-password input is incremental, not a rewrite.
- **IMAP IDLE flakiness on cellular / hotel networks.** Some networks drop long-lived TCP unceremoniously. Mitigation: 2-minute poll fallback, exponential reconnect backoff, status surfaced clearly.
- **Subject-line storage filename conflicts.** Two emails with identical sanitized subjects in the same `originals/<doc_id>/` directory would collide if we stored multiple parts under the same prefix — but each Document gets its own UUID directory, so this isn't actually a conflict path. Worth a regression test.
- **UIDVALIDITY churn on niche providers.** Could trigger expensive full-rescans. Mitigation: rescan is dedup-safe via `eml_sha256`; the cost is IMAP bandwidth, not duplicated Documents.
- **Gmail's `[Gmail]/All Mail` is a foot-gun.** A user picking it indexes their entire account, defeating the whole filtering premise. Mitigation: greyed-with-warning treatment in the label picker; an explicit "Are you sure?" confirmation if the user picks any system folder or any label with > 1000 messages reported by IMAP `STATUS`.

## Open questions deferred to implementation

- Exact choice between `imapclient` (mature, sync) and `aioimaplib` (async, IDLE-friendly) — likely both, with `imapclient` for one-shot ops in API endpoints and `aioimaplib` for the long-lived sync engine. Decided during stage 2.
- Whether to render the "Open in Gmail" link via `mail/u/?authuser=<watched-account-address>` or `mail/u/0/`. The former requires the watched account to be logged in to Gmail in the user's browser (typical case for a user with their own Gmail open in another tab); the latter assumes the first logged-in account. Likely use `?authuser=` and accept that users without the watched account logged in see a Gmail account-picker. Decided during stage 4.
- Whether `email_to_addresses` and `email_cc_addresses` should be `TEXT[]` or normalized into a separate `email_recipients` table. `TEXT[]` for MVP simplicity; normalize later only if a `kb_email_search` tool needs efficient recipient lookups.
