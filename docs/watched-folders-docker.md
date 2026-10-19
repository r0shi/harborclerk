# Watched Folders on Docker

Harbor Clerk's Docker deployment ingests documents from watched folders that you mount under `/data/watch`. There is no "Add Folder" button in the web UI on Docker — folders appear automatically when you mount them.

Documents are read in place, never copied, so every service that reads them sees the same mounts: `watcher` (discovery), `worker-io` (extract), `worker-cpu` (OCR) and `app` (downloads). All of them mount the watch root read-only, at the same container path; Harbor Clerk never writes to your files.

## How it works

The `watcher` service watches `WATCH_ROOT` (default `/data/watch`) for new top-level subdirectories. `WATCH_ROOT` is where folders are discovered, not where `./data/watch` is mounted: the mount is always at `/data/watch`, so `WATCH_ROOT` must be `/data/watch` or a directory under it. Each subdirectory is registered as a watched folder. Files dropped into any subdirectory are ingested through the normal pipeline (extract → ocr → chunk → entities → embed → summarize → finalize).

## Adding a folder

1. Create the directory on the host:

   ```bash
   mkdir -p ./data/watch/contracts
   ```

   On Linux, if Docker created `./data/watch` itself (the stack was started before the directory existed), it is owned by root and the `mkdir` needs `sudo`; `sudo chown "$USER" ./data/watch` once avoids that from then on.

2. The default `docker-compose.yml` already mounts `./data/watch` read-only at `/data/watch` in `watcher`, `app`, `worker-io` and `worker-cpu`. New subdirectories of `./data/watch` are auto-discovered every 60 seconds — no restart needed.

3. To mount a directory that lives elsewhere on the host, first create its mountpoint under `./data/watch` on the host (step 1, e.g. `mkdir -p ./data/watch/contracts`). `./data/watch` is mounted read-only, so Docker cannot create the mountpoint for a nested mount itself, and the container fails to start without it.

   Then edit `docker-compose.yml` (or your `compose.override.yml`) and add the same mount, read-only and at the same container path, to `watcher` and to every service that reads document bytes — `app` (downloads), `worker-io` (extract) and `worker-cpu` (OCR):

   ```yaml
   services:
     watcher:
       volumes:
         - ./data/watch:/data/watch:ro
         - /Users/alice/Documents/Contracts:/data/watch/contracts:ro
     app:
       volumes:
         - llama_models:/models
         - ./data/watch:/data/watch:ro
         - /Users/alice/Documents/Contracts:/data/watch/contracts:ro
     worker-io:
       volumes:
         - ./data/watch:/data/watch:ro
         - /Users/alice/Documents/Contracts:/data/watch/contracts:ro
     worker-cpu:
       volumes:
         - ./data/watch:/data/watch:ro
         - /Users/alice/Documents/Contracts:/data/watch/contracts:ro
   ```

   Watched documents are read in place from the path the watcher recorded, so a folder mounted only into `watcher` is discovered but every document in it fails extraction and cannot be downloaded. Use the same container path everywhere. The target must be a top-level subdirectory of `WATCH_ROOT`: `/data/watch/<name>` by default, `/data/watch/library/<name>` with `WATCH_ROOT=/data/watch/library`.

   Recreate the services:

   ```bash
   docker compose up -d --force-recreate watcher app worker-io worker-cpu
   ```

4. Within ~60 seconds, the new folder appears at `/folders` in the web UI with the **auto-discovered** badge.

## Removing a folder

Remove the bind mount (from every service it was added to, then remove its now-empty mountpoint under `./data/watch`, or the folder stays registered as an empty folder) or delete the host directory. Within ~60 seconds the folder appears as **unmounted** (red pill) in the web UI; you can then click **Delete** on its row to remove the registry entry. Documents that were ingested from that folder remain in the corpus and stay queryable.

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
| `WATCH_ROOT` | `/data/watch`  | Directory inside the containers whose top-level subdirs become folders. Must be `/data/watch` (the mount) or a directory under it, such as `/data/watch/library`, that exists on the host. Anything else is not visible to the workers: the watcher and the API log an ERROR at startup. |
| `LOG_LEVEL`  | `INFO`         | Watcher log verbosity. Use `DEBUG` to see per-event details when debugging.|
| `WATCH_RESCAN_INTERVAL_SECS` | `300` | Seconds between reconciliations of each folder with the index (new and changed files indexed, deleted ones removed). `0` keeps only the one when a folder's observer attaches. |

## Related endpoints

| Method | Path                                        | Purpose                                                       |
| ------ | ------------------------------------------- | ------------------------------------------------------------- |
| GET    | `/api/watch/system`                         | Returns `{platform, picker, watch_root}` for the frontend.    |
| GET    | `/api/watch/folders`                        | List all watched folders.                                     |
| POST   | `/api/watch/folders`                        | 409 on Docker: folders are discovered, never added by hand.   |
| GET    | `/api/watch/folders/{id}/progress`          | Per-folder ingestion stats across all 7 pipeline stages.      |
| GET    | `/api/watch/folders/stream`                 | SSE stream of per-folder progress deltas.                     |
| DELETE | `/api/watch/folders/{id}`                   | Remove the folder. 409 while still actively mounted.          |
