# IMAP Read-Only Safeguards

Harbor Clerk's mail ingestion is strictly read-only. This document explains the layers of protection in place, what they guarantee, and what self-hosters can do to add a further server-side layer.

## What we guarantee

Across four independent layers, Harbor Clerk will not mutate your mail server:

1. **Protocol layer.** Every mailbox is opened with the IMAP `EXAMINE` command, not `SELECT`. The IMAP server itself rejects `STORE`, `EXPUNGE`, `COPY`, `MOVE`, `APPEND` on an `EXAMINE`'d selection — no matter what bytes the client sends.

2. **Subclass layer.** Harbor Clerk's `ReadOnlyIMAP4_SSL` overrides every mutating method on the underlying aioimaplib client (`select`, `store`, `copy`, `move`, `expunge`, `append`, `create`, `delete`, `rename`, `subscribe`, `unsubscribe`) to raise before any bytes leave the process. The polymorphic `uid()` entry point inspects its subcommand and refuses `STORE / COPY / MOVE / EXPUNGE`. A drift-guard test enumerates aioimaplib's public surface and fails CI if a new method is added that we haven't classified.

3. **Wrapper layer.** All IMAP access goes through `IMAPConnection`, which exposes only the read-only operations we need (`examine`, `fetch`, `uid` (gated), `uid_search`, `list_mailboxes`, `has_capability`, `idle_start`, `idle_done`, `wait_server_push`). There is no escape hatch to the underlying client. A static AST scan (`tests/mail/test_no_mutation_static.py`) walks every `.py` file in `src/harbor_clerk/mail/` and fails the test if any code calls `.store()`, `.expunge()`, `.uid_store()`, `.uid_expunge()`, or bare `.select()`.

4. **Test invariants.** `FakeIMAP` (the in-process IMAP fake used by every unit test) mirrors the same constraints — calls to mutating methods raise `ReadOnlyViolation` rather than silently returning OK. This means production drift is caught by the unit-test layer, not just by a real server.

Additionally, every IMAP command is recorded to the `imap_command_log` table for 30 days, with credentials redacted (`LOGIN` and `XOAUTH2` arguments have their last positional value replaced with `[redacted]`). Response bodies are never persisted — only the byte count. If something does go wrong, there is an auditable record of every operation.

## Additional server-side safeguards (for self-hosters)

The layers above are client-side defenses. If you self-host your mail server (Dovecot, Cyrus, etc.), you can add a fifth, server-enforced layer by giving Harbor Clerk a read-only account.

### Dovecot

Create a dedicated read-only IMAP user and grant `lookup read` ACL on the folders Harbor Clerk should index:

```
doveadm acl set INBOX user=harborclerk@example.org lookup read
```

This guarantees that even if Harbor Clerk were compromised, the credentials it holds cannot perform writes.

### Cyrus IMAPd

```
cyradm> setaclmailbox user.alice harborclerk lrs
```

Replace `user.alice` with the actual mailbox and `harborclerk` with the Harbor Clerk account's username.

### Courier-IMAP

Courier does not support per-folder ACLs. Use OS-level permissions on the maildir to restrict the Harbor Clerk account to read access.

### SaaS providers

Most SaaS IMAP providers do not offer per-user read-only credentials over IMAP. Use the strongest available access:

- **Gmail:** Use an app password. When OAuth support is added to Harbor Clerk, switch to the `https://www.googleapis.com/auth/gmail.readonly` scope.
- **Microsoft 365 / Outlook.com:** Use an app password. When OAuth support is added, use the `Mail.Read` Graph scope.
- **iCloud Mail:** Use an app-specific password. iCloud has no per-credential scope mechanism over IMAP.
- **Fastmail:** Use an app password with the "Mail" scope only — Fastmail supports OAuth scopes for some integrations, but Harbor Clerk uses IMAP today, not Fastmail's JMAP API.

## Verifying read-only behavior

If you have local Dovecot running on a non-production address, you can run the no-mutation invariant test:

```bash
DOVECOT_HOST=your.mail.host \
DOVECOT_PORT=993 \
DOVECOT_USER=harborclerk@example.org \
DOVECOT_PASSWORD=... \
uv run pytest tests/integration/test_mail_e2e_dovecot.py::test_no_mutation_invariant_against_dovecot -v -m integration
```

This snapshots the mailbox state via an independent raw IMAP client, runs a full Harbor Clerk ingestion cycle, snapshots again, and asserts that the folder list, UIDVALIDITY, UIDNEXT, message UIDs, and per-message flags are all unchanged. Any divergence fails the test.

## Where the audit log lives

The audit log table is `imap_command_log` in the application database. Each row records:

| Column | What it is |
|---|---|
| `log_id` | UUID primary key |
| `account_id` | FK to `mail_accounts` (CASCADE delete) |
| `label_path` | The folder the command operated on (null for non-mailbox commands) |
| `command` | The IMAP verb (`EXAMINE`, `FETCH`, `UID`, `LOGIN`, ...) |
| `args_redacted` | Space-joined args with credentials masked |
| `response_status` | `OK` / `NO` / `BAD` / `ERROR` |
| `response_bytes` | Byte count of the response (not the response itself) |
| `duration_ms` | Wall-clock time of the call |
| `error` | `repr(exception)` if the call raised, else NULL |
| `created_at` | Timestamp |

The reaper runs hourly and drops rows older than 30 days. To change the retention window, edit `RETENTION_DAYS` in `src/harbor_clerk/mail/audit.py`. The reaper picks the new value up on its next tick.

## What is NOT audited

- `wait_server_push` — IDLE notifications fire continuously; auditing each would swamp the log. The bracketing `idle_start` and `idle_done` calls are recorded so an admin can see how many IDLE sessions ran.
- `idle_done` — a synchronous local state-flush with no IMAP round trip.
- `has_capability` — a local lookup against the capability list cached at connect/login time. Cheap and frequent.
