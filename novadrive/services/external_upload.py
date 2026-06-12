"""Upload a stored file to a temporary third-party host (workupload-style).

The user picks a configured host for one of their files; an in-process worker
streams the file straight from storage to the host (no local re-buffering for
PUT, and streamed multipart for POST when requests-toolbelt is available) so
multi-GB uploads work without blocking a web worker.

Hosts are configured via ``EXTERNAL_UPLOAD_PROVIDERS`` (see config). Each entry:
``{"name","method","url","field","result","max_size","headers"}``.
"""

from __future__ import annotations

import logging
import re
import threading
from urllib.parse import quote

import requests

from novadrive.extensions import db
from novadrive.models import ExternalUpload, File, User, utcnow
from novadrive.services.file_service import FileService
from novadrive.utils.logging import structured_log
from novadrive.utils.validators import ValidationError

try:  # optional: enables streamed multipart POST without buffering to disk
    from requests_toolbelt import MultipartEncoder
except Exception:  # noqa: BLE001
    MultipartEncoder = None

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_PROGRESS_FLUSH_BYTES = 4 * 1024 * 1024

_claim_lock = threading.Lock()
_shutdown = threading.Event()
_workers: list[threading.Thread] = []
_workers_started = False
_start_lock = threading.Lock()


class ExternalUploadError(ValidationError):
    pass


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def available_providers(config) -> list[dict]:
    return config.get("EXTERNAL_UPLOAD_PROVIDERS") or []


def providers_for_file(config, total_size: int) -> list[dict]:
    """Providers whose size cap fits ``total_size`` (0 = unlimited)."""
    result = []
    for provider in available_providers(config):
        cap = int(provider.get("max_size") or 0)
        if cap == 0 or total_size <= cap:
            result.append({"id": provider["id"], "name": provider["name"], "max_size": cap})
    return result


def _find_provider(provider_id: str, config) -> dict | None:
    for provider in available_providers(config):
        if provider["id"] == provider_id:
            return provider
    return None


def enqueue(user: User, file_record: File, provider_id: str, config) -> ExternalUpload:
    if not config.get("EXTERNAL_UPLOAD_ENABLED", True):
        raise ExternalUploadError("Third-party uploads are disabled on this instance.")
    provider = _find_provider(provider_id, config)
    if provider is None:
        raise ExternalUploadError("That upload host is not available.")
    cap = int(provider.get("max_size") or 0)
    if cap and int(file_record.total_size or 0) > cap:
        raise ExternalUploadError(
            f"This file is larger than {provider['name']} allows "
            f"({FileService._format_bytes(cap)})."
        )

    job = ExternalUpload(
        owner_id=user.id,
        file_id=file_record.id,
        provider=provider_id,
        status=ExternalUpload.STATUS_QUEUED,
        total_bytes=int(file_record.total_size or 0),
    )
    db.session.add(job)
    db.session.commit()
    structured_log(logger, "external_upload.queued", job_id=job.id, provider=provider_id, file_id=file_record.id)
    return job


def list_for_file(user: User, file_id: int, limit: int = 25) -> list[ExternalUpload]:
    return (
        ExternalUpload.query.filter_by(owner_id=user.id, file_id=file_id)
        .order_by(ExternalUpload.created_at.desc())
        .limit(limit)
        .all()
    )


def cancel(job: ExternalUpload) -> bool:
    if not job.is_active:
        return False
    job.status = ExternalUpload.STATUS_CANCELED
    job.finished_at = utcnow()
    if not job.error:
        job.error = "Canceled by user."
    db.session.commit()
    return True


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def start_workers(app) -> None:
    global _workers_started
    if not app.config.get("EXTERNAL_UPLOAD_ENABLED", True):
        return
    with _start_lock:
        if _workers_started:
            return
        _workers_started = True
        _shutdown.clear()
        for index in range(int(app.config.get("EXTERNAL_UPLOAD_WORKERS", 1))):
            thread = threading.Thread(
                target=_worker_loop, args=(app,), name=f"external-upload-{index}", daemon=True
            )
            thread.start()
            _workers.append(thread)


def _worker_loop(app) -> None:
    with app.app_context():
        while not _shutdown.is_set():
            job_id = _claim_next_job_id()
            if job_id is None:
                _shutdown.wait(_POLL_INTERVAL_SECONDS)
                continue
            try:
                _process_job(app.config, job_id)
            except Exception:  # noqa: BLE001
                logger.exception("External upload job %s crashed", job_id)
                db.session.rollback()
                _set_status(job_id, ExternalUpload.STATUS_FAILED, "Upload failed unexpectedly.")


def _claim_next_job_id() -> int | None:
    with _claim_lock:
        job = (
            ExternalUpload.query.filter_by(status=ExternalUpload.STATUS_QUEUED)
            .order_by(ExternalUpload.id.asc())
            .first()
        )
        if job is None:
            return None
        job.status = ExternalUpload.STATUS_RUNNING
        job.started_at = utcnow()
        db.session.commit()
        return job.id


def _process_job(config, job_id: int) -> None:
    job = db.session.get(ExternalUpload, job_id)
    if job is None or job.status != ExternalUpload.STATUS_RUNNING:
        return
    file_record = db.session.get(File, job.file_id)
    provider = _find_provider(job.provider, config)
    if file_record is None or file_record.is_deleted:
        _set_status(job_id, ExternalUpload.STATUS_FAILED, "The file no longer exists.")
        return
    if provider is None:
        _set_status(job_id, ExternalUpload.STATUS_FAILED, "The upload host is no longer configured.")
        return

    total = int(file_record.total_size or 0)
    timeout = (15, int(config.get("EXTERNAL_UPLOAD_TIMEOUT_SECONDS", 1800)))
    reader = _StorageReader(file_record, config, total, job_id)
    try:
        response = _send(provider, file_record.filename, reader, timeout)
        response.raise_for_status()
        link = _parse_result(provider.get("result") or "text", response)
        if not link:
            raise ExternalUploadError(f"{provider['name']} did not return a download link.")
        _finish(job_id, ExternalUpload.STATUS_COMPLETED, result_url=link, progress_bytes=total)
        structured_log(logger, "external_upload.completed", job_id=job_id, provider=provider["id"])
    except _Canceled:
        db.session.rollback()
        _set_status(job_id, ExternalUpload.STATUS_CANCELED, "Canceled by user.")
    except (ExternalUploadError, ValidationError) as exc:
        db.session.rollback()
        _set_status(job_id, ExternalUpload.STATUS_FAILED, str(exc))
    except requests.RequestException as exc:
        db.session.rollback()
        _set_status(job_id, ExternalUpload.STATUS_FAILED, f"The upload host could not be reached: {exc}")


def _send(provider: dict, filename: str, reader, timeout):
    url = provider["url"].replace("{filename}", quote(filename, safe=""))
    headers = dict(provider.get("headers") or {})
    if provider.get("method") == "PUT":
        headers.setdefault("Content-Length", str(len(reader)))
        return requests.put(url, data=reader, headers=headers, timeout=timeout)

    field = provider.get("field") or "file"
    if MultipartEncoder is not None:
        encoder = MultipartEncoder(
            fields={field: (filename, reader, "application/octet-stream")}
        )
        headers["Content-Type"] = encoder.content_type
        return requests.post(url, data=encoder, headers=headers, timeout=timeout)
    # Fallback: requests streams a file-like multipart part from the reader.
    return requests.post(url, files={field: (filename, reader, "application/octet-stream")}, headers=headers, timeout=timeout)


def _parse_result(spec: str, response: requests.Response) -> str | None:
    text = (response.text or "").strip()
    if spec.startswith("json:"):
        try:
            data = response.json()
        except ValueError:
            return None
        for part in spec[5:].split("."):
            if isinstance(data, dict):
                data = data.get(part)
            else:
                return None
        return str(data) if data else None
    if spec.startswith("regex:"):
        match = re.search(spec[6:], text)
        if not match:
            return None
        return match.group(1) if match.groups() else match.group(0)
    return text if text.lower().startswith("http") else None


# --------------------------------------------------------------------------- #
# Streaming reader (storage -> host) with progress + cancel
# --------------------------------------------------------------------------- #
class _Canceled(Exception):
    pass


class _StorageReader:
    def __init__(self, file_record: File, config, total: int, job_id: int):
        self._gen = FileService.iter_file_content(file_record, config)
        self._buffer = b""
        self._eof = False
        self._total = total
        self._read = 0
        self._reported = 0
        self._job_id = job_id

    def __len__(self) -> int:
        return self._total

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buffer]
            self._buffer = b""
            parts.extend(self._gen)
            self._eof = True
            data = b"".join(parts)
        else:
            while len(self._buffer) < size and not self._eof:
                try:
                    self._buffer += next(self._gen)
                except StopIteration:
                    self._eof = True
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        self._read += len(data)
        if self._read - self._reported >= _PROGRESS_FLUSH_BYTES or not data:
            self._reported = self._read
            if self._is_canceled():
                raise _Canceled()
            db.session.query(ExternalUpload).filter_by(id=self._job_id).update(
                {"progress_bytes": self._read}
            )
            db.session.commit()
        return data

    def _is_canceled(self) -> bool:
        status = db.session.query(ExternalUpload.status).filter_by(id=self._job_id).scalar()
        return status == ExternalUpload.STATUS_CANCELED


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def _finish(job_id: int, status: str, **fields) -> None:
    job = db.session.get(ExternalUpload, job_id)
    if job is None:
        return
    job.status = status
    job.finished_at = utcnow()
    for key, value in fields.items():
        setattr(job, key, value)
    db.session.commit()


def _set_status(job_id: int, status: str, error: str | None) -> None:
    job = db.session.get(ExternalUpload, job_id)
    if job is None:
        return
    if status == ExternalUpload.STATUS_FAILED and job.status == ExternalUpload.STATUS_CANCELED:
        return
    job.status = status
    job.error = error
    job.finished_at = utcnow()
    db.session.commit()
