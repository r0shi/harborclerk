# Secrets and the master key

Harbor Clerk encrypts sensitive values (currently mail-account app passwords; in
the future, OAuth refresh tokens, off-host MinIO keys, and similar) with a
master key before storing them in Postgres. This document explains how the
master key works on each deployment, how to back it up, and what to do if you
lose it.

## How the master key works

- The master key is **32 random bytes**, base64-encoded for transport.
- Every encrypted column in Postgres stores `(ciphertext, key_fingerprint)`. The
  fingerprint is an HMAC-SHA256 of the master key, truncated to 8 bytes — it
  identifies which key was used without revealing the key itself.
- On decrypt, the fingerprint is compared to the active key's fingerprint
  before any decryption is attempted. A mismatch raises `KeyMismatch`, which
  the application surfaces in Status as "reconnect mail accounts".

## Where the master key lives

### Docker

Operator sets `HARBOR_CLERK_MASTER_KEY` in the Compose env. Generate one:

```bash
python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

Add it to `.env` or your secrets workflow:

```dotenv
HARBOR_CLERK_MASTER_KEY=YOUR_BASE64_KEY_HERE
```

The `app`, `worker-io`, `worker-cpu`, and `watcher` services all need this env
var. `docker-compose.yml` documents this on each service.

**Back this up.** A Postgres backup without the master key is useless for any
encrypted column. The simplest workflow: store the env var alongside your
Postgres backup credentials in your secrets manager.

### macOS native

The Harbor Clerk Server menubar app generates the master key on first launch
and stores it in your login Keychain under the service identifier
`com.harborclerk.master-key`. On every subsequent launch, Swift reads
it from Keychain and exports it as `HARBOR_CLERK_MASTER_KEY` to all Python
subprocesses (api, worker-io, worker-cpu, watcher).

**You don't need to do anything.** The key is generated, persisted, and rotated
into subprocesses automatically.

**To inspect the key** (for backup or debugging):

```bash
security find-generic-password -s 'com.harborclerk.master-key' -w
```

This prints the 44-character base64 representation. Keep it somewhere safe if
you intend to migrate this DB to another machine or to Docker.

**To back it up**, copy that base64 string into a secure location (1Password,
your secrets manager, an encrypted note). The Keychain item is bound to this
device (`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`) — it does not roam via
iCloud Keychain and cross-machine moves require manual export.

## What happens if you lose the master key

You'll see "X mail accounts need reconnecting" in Status. The encrypted
secrets are unrecoverable, but everything else is intact (documents, chunks,
embeddings, search history, audit logs are all unencrypted).

The fix: click each affected mail account and re-paste the app password. New
ciphertexts are tagged with the new master key's fingerprint and the account
becomes active again.

## Migrating between deployments

### Docker → Docker (same key)

Carry `HARBOR_CLERK_MASTER_KEY` over to the new deployment. Restore Postgres.
Mail accounts work without intervention.

### Docker → macOS

Two options.

**Option A — Re-enter (simplest):** Restore Postgres. Launch the macOS app.
Status shows "X mail accounts need reconnecting." Re-paste app passwords.

**Option B — Import the master key (no re-entry):** *Not yet implemented.* See
the email-ingestion spec; an "Import master key" admin form is planned for
Stage 4. Until that ships, Option A is the only path.

### macOS → Docker

Same shape as the reverse. Export the macOS Keychain key with `security
find-generic-password -w`, set it as `HARBOR_CLERK_MASTER_KEY` on the Docker
side, restore Postgres.

## Key rotation

Not implemented in MVP. When it becomes necessary, the design is: a maintenance
command iterates every encrypted column, decrypts with the old key, re-encrypts
with the new key, updates the fingerprint. Until then, treat the master key as
generate-once-and-keep-forever.
