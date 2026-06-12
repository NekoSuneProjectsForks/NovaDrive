# Remote Download ("Add by URL") — Roadmap / TODO

Goal: let a user paste a URL (or magnet/torrent) and have NovaDrive fetch the
content **server-side** and store it into the configured backend (Discord
chunked, or S3 single-object) exactly like a normal upload.

This is the same capability seedbox / "leech" / debrid services (e.g. SonicBit)
offer. The long list of supported sites those services advertise is essentially
the **yt-dlp extractor list** plus a torrent client plus a couple of generic
downloaders — we do not hand-write 400 integrations.

## Source types to support

| Source | Tool / approach | Notes |
| --- | --- | --- |
| Direct HTTP/HTTPS | `aria2c` (or streaming `requests`) | Range + resume, multi-connection |
| FTP / SFTP | `aria2c` (ftp/sftp), or `paramiko` for SFTP auth | Optional user/pass in URL |
| Torrent `.torrent` / `magnet:` | `aria2c --enable-dht`, or `libtorrent` / Transmission daemon | Needs disk scratch + seeding policy |
| File hosts (1Fichier, Mediafire, Mega, ...) & media sites (YouTube, Dailymotion, ...) | **`yt-dlp`** | Its extractor list == the big host list. Some hosts need premium creds / are "best effort". |
| OneDrive / Google Drive / Dropbox public links | `yt-dlp` or direct link resolution | Public/shared links only |
| OneDrive / Google Drive / Dropbox / Nextcloud (private accounts) | `rclone` with per-user OAuth, or WebDAV for Nextcloud (`novadrive/services/webdav_service.py` already exists) | Requires storing user tokens / OAuth apps |

## Why this needs new infrastructure

Remote downloads are **long-running and unbounded** (minutes to hours, multi-GiB).
The current request path is synchronous (Flask + waitress). We must not block a
web worker on a download. So:

1. **Job model** — new `RemoteDownload` table: `id, owner_id, shared_drive_id,
   folder_id, source_url, source_type, status (queued|running|completed|failed|
   canceled), progress_bytes, total_bytes, error, file_id (result), created_at`.
2. **Worker** — a background process/thread that claims queued jobs, runs the
   right downloader into a scratch temp dir, then calls
   `FileService.upload_single_file(...)`-equivalent to ingest the finished file
   (respecting quota — quota must be re-checked at completion, and ideally
   pre-reserved). Could be a simple DB-polling worker thread, or RQ/Celery.
3. **Progress + control API** — endpoints to create a job, list jobs, poll
   progress, and cancel. UI: an "Add by URL" modal + a downloads panel.
4. **Safety / abuse** — SSRF guard (block internal IPs/metadata endpoints),
   per-user concurrency + size caps, allowed-scheme list, sandbox the scratch
   dir, and content-type/size validation before ingest.

## Suggested phased delivery

- **Phase 1 — Direct HTTP(S) by URL. ✅ DONE.** Job model + worker + "Add by URL"
  panel. Streamed download → existing ingest path, with an SSRF guard.
- **Phase 2 — FTP/SFTP + torrent/magnet. ✅ DONE.** `aria2c` (single binary,
  added to the Docker image) is driven over JSON-RPC by
  `services/aria2_downloader.py`: per-job process, progress polling, magnet/
  child-gid following, cancel via `aria2.remove`, scratch dir cleaned up after.
  Resulting files are ingested through `store_stream`. Gated by
  `REMOTE_DOWNLOAD_ALLOW_TORRENTS`; `seed-time=0` so nothing seeds after fetch.
- **Phase 3 — yt-dlp host/media sites.** Bundle `yt-dlp`; map the advertised
  host list to its extractors. Surface which hosts need credentials.
- **Phase 4 — Private cloud accounts.** Per-user OAuth for OneDrive/GDrive/
  Dropbox via `rclone`; Nextcloud via the existing WebDAV service. Token storage
  + refresh.

## Ingest hook

All downloaders converge on one internal call: given a finished file stream +
filename + mime, run the same validation/quota/store flow as
`FileService.upload_single_file`. Refactor that method to expose a
`store_stream(user, folder, stream, filename, mime, total_size, config)` core so
both browser uploads and remote downloads share it. With the new whole-file S3
path, large remote downloads land as a single S3 object automatically.

## Dependencies to add (when each phase starts)

- Phase 1: none beyond stdlib + `requests` (already present).
- Phase 2: `aria2` (system binary) in the Docker image.
- Phase 3: `yt-dlp` (pip) + `ffmpeg` (system binary, for muxing).
- Phase 4: `rclone` (system binary) + OAuth client apps per provider.

## Open decisions (need owner input)

- Worker model: lightweight DB-polling thread in the same container vs. a
  separate worker service (RQ/Celery + Redis)?
- Torrent seeding policy (ratio/time) and whether to allow magnet at all.
- Which providers in Phase 4 are actually wanted, and who registers the OAuth
  apps (each needs a developer app + redirect URI).
