"""One-time maintenance that merges legacy multi-chunk objects.

Files stored on a whole-file backend (S3) used to be split into many chunks.
:func:`consolidate_chunked_objects` finds those leftovers and rewrites each as a
single object, so downloads stop emitting a flood of ``storage.chunk_fetched``
lines. It is idempotent — once a file has a single chunk it is skipped — so it
is safe to run on every startup.
"""

from __future__ import annotations

import logging
import threading

from novadrive.extensions import db
from novadrive.models import File, FileChunk, FileManifest, utcnow
from novadrive.services.file_service import FileService
from novadrive.services.storage_base import StorageBackendError
from novadrive.services.storage_factory import get_storage_backend
from novadrive.utils.logging import structured_log

logger = logging.getLogger(__name__)

_run_lock = threading.Lock()


def _candidate_file_ids(backend_name: str) -> list[int]:
    rows = (
        db.session.query(File.id)
        .join(FileManifest, FileManifest.file_id == File.id)
        .filter(
            FileManifest.storage_backend == backend_name,
            File.total_chunks > 1,
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        )
        .order_by(File.id.asc())
        .all()
    )
    return [row[0] for row in rows]


def consolidate_chunked_objects(app, backend_name: str = "s3") -> dict[str, int]:
    """Rewrite every multi-chunk file on ``backend_name`` as a single object.

    Returns a summary ``{"converted": int, "failed": int}``. Runs at most once
    at a time across threads; concurrent calls return zero counts immediately.
    """
    if not _run_lock.acquire(blocking=False):
        return {"converted": 0, "failed": 0, "skipped": 1}

    converted = 0
    failed = 0
    try:
        with app.app_context():
            backend = get_storage_backend(app.config, backend_name=backend_name)
            if not getattr(backend, "whole_file", False):
                return {"converted": 0, "failed": 0}

            file_ids = _candidate_file_ids(backend_name)
            if not file_ids:
                return {"converted": 0, "failed": 0}

            structured_log(
                logger,
                "storage.consolidate_started",
                backend=backend_name,
                candidates=len(file_ids),
            )
            for file_id in file_ids:
                try:
                    if _consolidate_one(app.config, backend, file_id):
                        converted += 1
                except Exception:  # noqa: BLE001 - keep draining the queue
                    failed += 1
                    db.session.rollback()
                    logger.exception("Failed to consolidate file %s", file_id)

            structured_log(
                logger,
                "storage.consolidate_finished",
                backend=backend_name,
                converted=converted,
                failed=failed,
            )
        return {"converted": converted, "failed": failed}
    finally:
        _run_lock.release()


def _consolidate_one(config, backend, file_id: int) -> bool:
    file_record = db.session.get(File, file_id)
    if file_record is None or file_record.total_chunks <= 1 or file_record.is_deleted:
        return False

    # rebuild_file validates each chunk and the final hash, returning a spool of
    # the fully merged content (multi-chunk files use the per-chunk path).
    merged_stream, _ = FileService.rebuild_file(file_record, config)
    try:
        old_chunks = list(file_record.chunks)
        merged_stream.seek(0)
        channel_id = backend.choose_channel(file_record.id, 0)
        payload = backend.upload_whole(
            stream=merged_stream,
            filename=f"{file_record.id}-000000.part",
            sha256=file_record.sha256,
            channel_id=channel_id,
            metadata={**FileService.storage_metadata(file_record), "chunk_index": 0},
        )
        new_message_id = str(payload["message_id"])

        # Drop the old chunk objects (never the freshly written one).
        for chunk in old_chunks:
            if str(chunk.discord_message_id) == new_message_id:
                continue
            try:
                backend.delete_chunk(chunk.discord_channel_id, chunk.discord_message_id)
            except StorageBackendError:
                logger.warning(
                    "Could not delete legacy chunk %s of file %s during consolidation",
                    chunk.id,
                    file_record.id,
                )
            db.session.delete(chunk)
        db.session.flush()

        db.session.add(
            FileChunk(
                file_id=file_record.id,
                chunk_index=0,
                discord_channel_id=str(payload["channel_id"]),
                discord_message_id=new_message_id,
                discord_attachment_url=payload["attachment_url"],
                discord_attachment_filename=payload.get("attachment_filename"),
                chunk_size=file_record.total_size,
                sha256=file_record.sha256,
            )
        )
        file_record.total_chunks = 1
        file_record.updated_at = utcnow()
        if file_record.manifest:
            file_record.manifest.chunk_size = file_record.total_size
            file_record.manifest.last_verified_at = utcnow()
        db.session.commit()

        structured_log(
            logger,
            "storage.object_consolidated",
            backend="s3",
            file_id=file_record.id,
            object_size=file_record.total_size,
        )
        return True
    finally:
        merged_stream.close()
