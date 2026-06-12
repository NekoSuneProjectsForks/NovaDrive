"""Remote download: fetch a file from a URL/torrent into the drive.

A queued :class:`RemoteDownload` job is picked up by an in-process worker thread
and ingested through :meth:`FileService.store_stream`, so it lands in whatever
storage backend is configured (single S3 object, or Discord chunks).

Source types:
- ``http`` — streamed directly with SSRF protection + progress/cancel (Phase 1).
- ``ftp`` / ``sftp`` / ``magnet`` / ``torrent`` — handed to ``aria2c`` via the
  :mod:`novadrive.services.aria2_downloader` driver (Phase 2).

Future phases (cloud providers) plug in by adding new ``source_type`` handlers
that converge on the same ``store_stream`` ingest. See
``docs/REMOTE_DOWNLOAD_ROADMAP.md``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import tempfile
import threading
from email.message import Message
from urllib.parse import unquote, urljoin, urlsplit

import requests

from novadrive.extensions import db
from novadrive.models import File, Folder, RemoteDownload, User, utcnow
from novadrive.services import aria2_downloader
from novadrive.services.aria2_downloader import Aria2Canceled, Aria2Error
from novadrive.services.auth_service import AuthService
from novadrive.services.file_service import AccessError, FileService
from novadrive.utils.logging import structured_log
from novadrive.utils.validators import ValidationError

logger = logging.getLogger(__name__)

_ARIA2_SOURCE_TYPES = ("ftp", "sftp", "magnet", "torrent")

_MAX_REDIRECTS = 5
_POLL_INTERVAL_SECONDS = 2.0
_PROGRESS_FLUSH_BYTES = 4 * 1024 * 1024

_claim_lock = threading.Lock()
_shutdown = threading.Event()
_workers: list[threading.Thread] = []
_workers_started = False
_start_lock = threading.Lock()


class RemoteDownloadError(ValidationError):
    """User-facing problem with a remote download request."""


class _Canceled(Exception):
    """Raised internally when a job is canceled mid-download."""


# --------------------------------------------------------------------------- #
# SSRF protection
# --------------------------------------------------------------------------- #
def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_host(url: str, allow_private: bool) -> None:
    """Resolve a URL's host and reject private/internal addresses (SSRF guard)."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise RemoteDownloadError("That URL is missing a host.")
    if allow_private:
        return

    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RemoteDownloadError(f"Could not resolve host '{host}'.") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise RemoteDownloadError(
                "Refusing to download from a private or internal address."
            )


def _validate_public_url(url: str, allow_private: bool) -> None:
    """Validate an http(s) URL (used per redirect hop during the HTTP download)."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise RemoteDownloadError("Only http:// and https:// URLs are supported.")
    _validate_host(url, allow_private)


def detect_source_type(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "magnet" or url.lower().startswith("magnet:"):
        return "magnet"
    if scheme in ("ftp", "ftps"):
        return "ftp"
    if scheme == "sftp":
        return "sftp"
    if scheme in ("http", "https"):
        if parts.path.lower().endswith(".torrent"):
            return "torrent"
        return "http"
    raise RemoteDownloadError("Unsupported URL scheme.")


def _validate_source(url: str, source_type: str, *, allow_private: bool, allow_torrents: bool) -> None:
    if source_type in ("magnet", "torrent") and not allow_torrents:
        raise RemoteDownloadError("Your account is not allowed to download torrents or magnet links.")
    if source_type == "magnet":
        return  # No single host to validate for a peer swarm.
    # http/https/ftp/sftp and .torrent-over-http all have a resolvable host.
    _validate_host(url, allow_private)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def enqueue(user: User, folder: Folder, url: str, config) -> RemoteDownload:
    """Validate a URL and queue a download into ``folder``."""
    if not config.get("REMOTE_DOWNLOAD_ENABLED", True):
        raise RemoteDownloadError("Remote downloads are disabled on this instance.")

    url = (url or "").strip()
    if not url:
        raise RemoteDownloadError("Enter a URL to download.")
    if len(url) > 4096:
        raise RemoteDownloadError("That URL is too long.")

    FileService._ensure_can_write_folder(user, folder)
    source_type = detect_source_type(url)
    if source_type in _ARIA2_SOURCE_TYPES and not aria2_downloader.is_available():
        raise RemoteDownloadError(
            "This download type needs aria2c, which isn't installed on the server."
        )
    _validate_source(
        url,
        source_type,
        allow_private=bool(config.get("REMOTE_DOWNLOAD_ALLOW_PRIVATE_HOSTS")),
        allow_torrents=AuthService.user_can_download_torrents(user, config),
    )

    job = RemoteDownload(
        owner_id=user.id,
        folder_id=folder.id,
        shared_drive_id=folder.shared_drive_id,
        source_url=url,
        source_type=source_type,
        status=RemoteDownload.STATUS_QUEUED,
    )
    db.session.add(job)
    db.session.commit()
    structured_log(logger, "remote_download.queued", job_id=job.id, owner_id=user.id)
    return job


def cancel(job: RemoteDownload) -> bool:
    """Cancel a queued or running job. Returns True if a transition happened."""
    if not job.is_active:
        return False
    job.status = RemoteDownload.STATUS_CANCELED
    job.finished_at = utcnow()
    if not job.error:
        job.error = "Canceled by user."
    db.session.commit()
    structured_log(logger, "remote_download.canceled", job_id=job.id)
    return True


def list_for_user(user: User, limit: int = 25) -> list[RemoteDownload]:
    return (
        RemoteDownload.query.filter_by(owner_id=user.id)
        .order_by(RemoteDownload.created_at.desc())
        .limit(limit)
        .all()
    )


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def start_workers(app) -> None:
    """Spawn the configured number of daemon worker threads (idempotent)."""
    global _workers_started
    if not app.config.get("REMOTE_DOWNLOAD_ENABLED", True):
        return
    with _start_lock:
        if _workers_started:
            return
        _workers_started = True
        _shutdown.clear()
        count = int(app.config.get("REMOTE_DOWNLOAD_WORKERS", 2))
        for index in range(count):
            thread = threading.Thread(
                target=_worker_loop,
                args=(app,),
                name=f"remote-download-{index}",
                daemon=True,
            )
            thread.start()
            _workers.append(thread)
        structured_log(logger, "remote_download.workers_started", workers=count)


def stop_workers() -> None:
    _shutdown.set()


def _worker_loop(app) -> None:
    with app.app_context():
        while not _shutdown.is_set():
            job_id = _claim_next_job_id()
            if job_id is None:
                _shutdown.wait(_POLL_INTERVAL_SECONDS)
                continue
            try:
                _process_job(app.config, job_id)
            except Exception:  # noqa: BLE001 - never let the worker die
                logger.exception("Remote download job %s crashed", job_id)
                db.session.rollback()
                _mark_failed(job_id, "Download failed unexpectedly.")


def _claim_next_job_id() -> int | None:
    with _claim_lock:
        job = (
            RemoteDownload.query.filter_by(status=RemoteDownload.STATUS_QUEUED)
            .order_by(RemoteDownload.id.asc())
            .first()
        )
        if job is None:
            return None
        job.status = RemoteDownload.STATUS_RUNNING
        job.started_at = utcnow()
        db.session.commit()
        return job.id


def _process_job(config, job_id: int) -> None:
    job = db.session.get(RemoteDownload, job_id)
    if job is None or job.status != RemoteDownload.STATUS_RUNNING:
        return

    user = db.session.get(User, job.owner_id)
    folder = db.session.get(Folder, job.folder_id)
    if user is None or folder is None or folder.deleted_at is not None:
        _finish(job, RemoteDownload.STATUS_FAILED, error="The target folder no longer exists.")
        return

    try:
        if job.source_type in _ARIA2_SOURCE_TYPES:
            _run_aria2(config, job, user, folder)
        else:
            _run_http(config, job, user, folder)
    except (_Canceled, Aria2Canceled):
        db.session.rollback()
        _refresh_status(job_id, RemoteDownload.STATUS_CANCELED, "Canceled by user.")
    except (RemoteDownloadError, ValidationError, AccessError, Aria2Error) as exc:
        db.session.rollback()
        _refresh_status(job_id, RemoteDownload.STATUS_FAILED, str(exc))
    except requests.RequestException as exc:
        db.session.rollback()
        _refresh_status(job_id, RemoteDownload.STATUS_FAILED, f"Could not fetch the URL: {exc}")


def _run_http(config, job: RemoteDownload, user: User, folder: Folder) -> None:
    allow_private = bool(config.get("REMOTE_DOWNLOAD_ALLOW_PRIVATE_HOSTS"))
    timeout = (
        int(config["REMOTE_DOWNLOAD_CONNECT_TIMEOUT_SECONDS"]),
        int(config["REMOTE_DOWNLOAD_READ_TIMEOUT_SECONDS"]),
    )
    response = None
    try:
        response = _open_source(job.source_url, allow_private, timeout)
        filename = _resolve_filename(job.source_url, response)
        content_length = response.headers.get("Content-Length")
        job.filename = filename[:255]
        job.total_bytes = int(content_length) if content_length and content_length.isdigit() else None
        db.session.commit()

        reader = _ProgressReader(response, job.id)
        file_record = FileService.store_stream(
            user,
            folder,
            reader,
            filename=filename,
            config=config,
            mime_type=(response.headers.get("Content-Type") or "").split(";")[0].strip() or None,
        )
        _finish(
            job,
            RemoteDownload.STATUS_COMPLETED,
            file_id=file_record.id,
            progress_bytes=file_record.total_size,
            total_bytes=file_record.total_size,
        )
        structured_log(
            logger,
            "remote_download.completed",
            job_id=job.id,
            file_id=file_record.id,
            size=file_record.total_size,
        )
    finally:
        if response is not None:
            response.close()


def _run_aria2(config, job: RemoteDownload, user: User, folder: Folder) -> None:
    job_id = job.id
    scratch_dir = tempfile.mkdtemp(prefix="nd-rdl-")

    def on_progress(completed: int, total: int) -> None:
        values: dict = {"progress_bytes": completed}
        if total:
            values["total_bytes"] = total
        db.session.query(RemoteDownload).filter_by(id=job_id).update(values)
        db.session.commit()

    def should_cancel() -> bool:
        status = db.session.query(RemoteDownload.status).filter_by(id=job_id).scalar()
        return status == RemoteDownload.STATUS_CANCELED

    try:
        paths = aria2_downloader.download_to_dir(
            job.source_url,
            scratch_dir,
            config=config,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
        if not paths:
            raise RemoteDownloadError("The download produced no files.")

        created: list[File] = []
        for path in paths:
            with open(path, "rb") as handle:
                created.append(
                    FileService.store_stream(user, folder, handle, filename=path.name, config=config)
                )

        total_size = sum(record.total_size for record in created)
        _finish(
            job,
            RemoteDownload.STATUS_COMPLETED,
            file_id=created[0].id,
            filename=created[0].filename[:255],
            progress_bytes=total_size,
            total_bytes=total_size,
        )
        structured_log(
            logger,
            "remote_download.completed",
            job_id=job_id,
            source_type=job.source_type,
            files=len(created),
            size=total_size,
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _open_source(url: str, allow_private: bool, timeout: tuple[int, int]) -> requests.Response:
    """GET ``url`` following redirects, validating every hop against SSRF rules."""
    session = requests.Session()
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _validate_public_url(current, allow_private)
        response = session.get(
            current,
            stream=True,
            allow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": "NovaDrive/remote-download"},
        )
        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise RemoteDownloadError("The server returned a redirect without a location.")
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        return response
    raise RemoteDownloadError("Too many redirects while fetching the URL.")


def _resolve_filename(url: str, response: requests.Response) -> str:
    disposition = response.headers.get("Content-Disposition")
    if disposition:
        parsed = Message()
        parsed["content-disposition"] = disposition
        name = parsed.get_filename()
        if name:
            return os.path.basename(unquote(name)).strip() or "download"

    path = urlsplit(url).path
    candidate = os.path.basename(unquote(path)).strip()
    return candidate or "download"


class _ProgressReader:
    """Wraps a streaming response so ``store_stream`` reads drive progress + cancel."""

    def __init__(self, response: requests.Response, job_id: int):
        self._iter = response.iter_content(chunk_size=1024 * 1024)
        self._buffer = b""
        self._exhausted = False
        self._job_id = job_id
        self._read_total = 0
        self._reported = 0

    def read(self, size: int = -1) -> bytes:
        if self._is_canceled():
            raise _Canceled()

        if size is None or size < 0:
            parts = [self._buffer]
            self._buffer = b""
            parts.extend(self._iter)
            self._exhausted = True
            data = b"".join(parts)
        else:
            while len(self._buffer) < size and not self._exhausted:
                try:
                    self._buffer += next(self._iter)
                except StopIteration:
                    self._exhausted = True
            data, self._buffer = self._buffer[:size], self._buffer[size:]

        self._read_total += len(data)
        if self._read_total - self._reported >= _PROGRESS_FLUSH_BYTES or not data:
            self._flush_progress()
        return data

    def _flush_progress(self) -> None:
        self._reported = self._read_total
        db.session.query(RemoteDownload).filter_by(id=self._job_id).update(
            {"progress_bytes": self._read_total}
        )
        db.session.commit()

    def _is_canceled(self) -> bool:
        if self._read_total - self._reported < _PROGRESS_FLUSH_BYTES and self._read_total:
            return False
        status = (
            db.session.query(RemoteDownload.status)
            .filter_by(id=self._job_id)
            .scalar()
        )
        return status == RemoteDownload.STATUS_CANCELED


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def _finish(job: RemoteDownload, status: str, **fields) -> None:
    job.status = status
    job.finished_at = utcnow()
    for key, value in fields.items():
        setattr(job, key, value)
    db.session.commit()


def _refresh_status(job_id: int, status: str, error: str | None) -> None:
    job = db.session.get(RemoteDownload, job_id)
    if job is None:
        return
    # A user cancel during the run wins over a late failure.
    if status == RemoteDownload.STATUS_FAILED and job.status == RemoteDownload.STATUS_CANCELED:
        return
    job.status = status
    job.error = error
    job.finished_at = utcnow()
    db.session.commit()


def _mark_failed(job_id: int, error: str) -> None:
    try:
        _refresh_status(job_id, RemoteDownload.STATUS_FAILED, error)
    except Exception:  # noqa: BLE001
        db.session.rollback()
