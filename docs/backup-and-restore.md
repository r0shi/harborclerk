# Backup and restore

This is cautious initial guidance, not a polished backup product flow. Harbor
Clerk reads watched-folder source files in place, so a complete backup has two
parts:

1. Harbor Clerk application data.
2. The original files in every watched folder.

## macOS native app

Harbor Clerk application data lives at:

```text
~/Library/Application Support/Harbor Clerk/
```

That directory contains the local database, downloaded models, logs, settings,
and other app-managed state. Your source documents are not copied there; they
stay in the folders you selected.

Basic backup:

1. Quit Harbor Clerk Server from the menubar app.
2. Copy `~/Library/Application Support/Harbor Clerk/` to backup storage.
3. Back up every watched-folder source directory separately.
4. If you use mail ingestion or any future encrypted secrets, also preserve the
   master key. See [Secrets and the master key](secrets-and-keys.md).

Basic restore:

1. Quit Harbor Clerk Server.
2. Restore the saved `Harbor Clerk/` application-support directory to
   `~/Library/Application Support/`.
3. Restore the watched-folder source files to the same paths where possible.
4. Launch Harbor Clerk Server and let the watcher reconcile changes.

If source files moved, Harbor Clerk may need folder access repaired or watched
folders re-added. Restore should be tested before relying on it for critical
data recovery.

## Docker Compose

Docker deployments are operator-managed. Back up:

- The Postgres data volume or a proper `pg_dump`.
- Any object-storage volume if your deployment still uses legacy uploads.
- The host directories mounted into the watcher.
- `.env`, especially `SECRET_KEY` and `HARBOR_CLERK_MASTER_KEY` if configured.

Do not treat `docker compose down -v` as a normal stop command; it deletes named
volumes. Use `docker compose down` to stop while keeping data.
