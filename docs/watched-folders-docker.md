# Watched Folders on Docker

Harbor Clerk's Docker deployment ingests documents from watched folders that you mount into the `watcher` container. There is no "Add Folder" button in the web UI on Docker — folders appear automatically when you mount them.

## How it works

The `watcher` service watches `WATCH_ROOT` (default `/data/watch`) for new top-level subdirectories. Each subdirectory is registered as a watched folder. Files dropped into any subdirectory are ingested through the normal pipeline (extract → ocr → chunk → entities → embed → summarize → finalize).

## Adding a folder

1. Create the directory on the host:

   ```bash
   mkdir -p ./data/watch/contracts
   ```

2. The default `docker-compose.yml` already mounts `./data/watch` into the `watcher` container at `/data/watch`. New subdirectories of `./data/watch` are auto-discovered every 60 seconds — no restart needed.

3. To mount a directory that lives elsewhere on the host, edit `docker-compose.yml` (or your `compose.override.yml`):

   ```yaml
   services:
     watcher:
       volumes:
         - ./data/watch:/data/watch
         - /Users/alice/Documents/Contracts:/data/watch/contracts
   ```

   Restart the `watcher` service:

   ```bash
   docker compose up -d --force-recreate watcher
   ```

4. Within ~60 seconds, the new folder appears at `/folders` in the web UI with the **auto-discovered** badge.

## Removing a folder

Remove the bind mount or delete the host directory. Within ~60 seconds the folder appears as **unmounted** (red pill) in the web UI; you can then click **Delete** on its row to remove the registry entry. Documents that were ingested from that folder remain in the corpus and stay queryable.

> Note: while the folder is still actively mounted (auto-discovered with no `unavailable_reason`), the API rejects `DELETE` with **409 Conflict**. Unmount first, then delete.

## Mounting non-local filesystems

NFS, SMB, fuse mounts, and other "exotic" filesystems are supported. The watcher detects when its native filesystem observer (inotify on Linux) fails to install on a given mount and automatically falls back to a 2-second polling loop for that folder. Polling is slower to detect changes but works on filesystems that don't deliver kernel events.

To confirm which observer is in use for a given folder, check the watcher logs:

```bash
docker compose logs watcher | grep -E 'native observer|polling'
```

You should see one line per folder at startup, e.g.:

```
watcher: native observer started for /data/watch/contracts
watcher: native observer failed for /data/watch/nfs-share, falling back to polling
```

## Resource limits

The watcher process is single-threaded and lightweight. Even with thousands of files across many folders, the bottleneck is the ingestion pipeline (`worker-io`, `worker-cpu`, `worker-llm`) — not the watcher itself. Sizing the watcher container is rarely necessary.

## Configuration reference

| Env var      | Default        | Description                                                                |
| ------------ | -------------- | -------------------------------------------------------------------------- |
| `WATCH_ROOT` | `/data/watch`  | Directory inside the container whose top-level subdirs become folders.     |
| `LOG_LEVEL`  | `INFO`         | Watcher log verbosity. Use `DEBUG` to see per-event details when debugging.|

## Related endpoints

| Method | Path                                        | Purpose                                                       |
| ------ | ------------------------------------------- | ------------------------------------------------------------- |
| GET    | `/api/watch/system`                         | Returns `{platform, picker, watch_root}` for the frontend.    |
| GET    | `/api/watch/folders`                        | List all watched folders.                                     |
| GET    | `/api/watch/folders/{id}/progress`          | Per-folder ingestion stats across all 7 pipeline stages.      |
| GET    | `/api/watch/folders/stream`                 | SSE stream of per-folder progress deltas.                     |
| DELETE | `/api/watch/folders/{id}`                   | Remove the folder. 409 while still actively mounted.          |
