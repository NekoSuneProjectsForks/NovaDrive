from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import secrets
import tempfile
from typing import BinaryIO

from flask import current_app, has_request_context, request
from sqlalchemy import func

from novadrive.extensions import db
from novadrive.models import (
    ExternalUpload,
    File,
    FileChunk,
    FileManifest,
    Folder,
    ShareLink,
    SharedDrive,
    User,
    utcnow,
)
from novadrive.services.activity_service import ActivityService
from novadrive.services.auth_service import AuthService
from novadrive.services.shared_drive_service import SharedDriveService
from novadrive.services.storage_base import StorageBackendError
from novadrive.services.storage_factory import (
    configured_storage_backend_name,
    get_storage_backend,
)
from novadrive.utils.chunking import ChunkValidationError, iter_file_chunks, validate_chunk_indexes
from novadrive.utils.hashing import sha256_bytes
from novadrive.utils.validators import ValidationError, normalize_filename, validate_folder_name

logger = logging.getLogger(__name__)


class AccessError(PermissionError):
    pass


class FileService:
    DOWNLOADS_FOLDER_NAME = "Downloads"

    @staticmethod
    def get_or_create_downloads_folder(user: User) -> Folder:
        """Return the user's personal ``Downloads`` folder, creating it if absent.

        This is the default destination for remote/torrent downloads.
        """
        root = AuthService.get_root_folder(user)
        existing = (
            Folder.query.filter_by(
                parent_id=root.id,
                owner_id=user.id,
                shared_drive_id=None,
                deleted_at=None,
                name=FileService.DOWNLOADS_FOLDER_NAME,
            )
            .order_by(Folder.id.asc())
            .first()
        )
        if existing is not None:
            return existing
        folder = Folder(
            name=FileService.DOWNLOADS_FOLDER_NAME,
            parent_id=root.id,
            owner_id=user.id,
            shared_drive_id=None,
        )
        db.session.add(folder)
        db.session.commit()
        return folder

    @staticmethod
    def current_usage_bytes(user: User | None = None, shared_drive: SharedDrive | None = None) -> int:
        usage_query = db.session.query(func.coalesce(func.sum(File.total_size), 0)).filter(
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        )
        if shared_drive is not None:
            usage_query = usage_query.filter(File.shared_drive_id == shared_drive.id)
        elif user is not None:
            usage_query = usage_query.filter(File.owner_id == user.id, File.shared_drive_id.is_(None))
        else:
            return 0
        return int(usage_query.scalar() or 0)

    @staticmethod
    def get_accessible_root_folder(
        user: User,
        owner: User | None = None,
        shared_drive: SharedDrive | None = None,
    ) -> Folder:
        if shared_drive is not None:
            if not SharedDriveService.can_view(shared_drive, user):
                raise AccessError("You do not have access to that shared drive.")
            return SharedDriveService.get_root_folder(shared_drive)

        target_owner = owner or user
        if not user.is_admin and target_owner.id != user.id:
            raise AccessError("You do not have access to that drive.")
        return AuthService.get_root_folder(target_owner)

    @staticmethod
    def get_folder_or_404(user: User, folder_id: int) -> Folder:
        folder = db.session.get(Folder, folder_id)
        if not folder or folder.deleted_at is not None:
            raise LookupError("Folder not found.")
        if folder.shared_drive_id:
            if not SharedDriveService.can_view(folder.shared_drive, user):
                raise AccessError("You do not have access to that folder.")
        elif not user.is_admin and folder.owner_id != user.id:
            raise AccessError("You do not have access to that folder.")
        return folder

    @staticmethod
    def get_file_or_404(user: User, file_id: int, include_deleted: bool = False) -> File:
        file_record = db.session.get(File, file_id)
        if not file_record:
            raise LookupError("File not found.")
        if file_record.is_deleted and not include_deleted:
            raise LookupError("File not found.")
        if file_record.shared_drive_id:
            if not SharedDriveService.can_view(file_record.shared_drive, user):
                raise AccessError("You do not have access to that file.")
        elif not user.is_admin and file_record.owner_id != user.id:
            raise AccessError("You do not have access to that file.")
        return file_record

    @staticmethod
    def build_breadcrumbs(folder: Folder) -> list[Folder]:
        breadcrumbs: list[Folder] = []
        current = folder
        while current is not None:
            breadcrumbs.append(current)
            current = current.parent
        return list(reversed(breadcrumbs))

    @staticmethod
    def folder_options(
        user: User,
        exclude_folder_id: int | None = None,
        owner: User | None = None,
        shared_drive: SharedDrive | None = None,
    ) -> list[tuple[int, str]]:
        root = FileService.get_accessible_root_folder(user, owner=owner, shared_drive=shared_drive)
        options: list[tuple[int, str]] = []

        def walk(node: Folder, depth: int) -> None:
            if node.deleted_at is not None:
                return
            if node.id != exclude_folder_id:
                prefix = "  " * depth
                options.append((node.id, f"{prefix}{node.name}"))
            children = (
                Folder.query.filter_by(parent_id=node.id, deleted_at=None)
                .order_by(Folder.name.asc())
                .all()
            )
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return options

    @staticmethod
    def folder_tree(
        user: User,
        owner: User | None = None,
        shared_drive: SharedDrive | None = None,
    ) -> list[dict]:
        root = FileService.get_accessible_root_folder(user, owner=owner, shared_drive=shared_drive)

        def build(node: Folder) -> dict:
            children = (
                Folder.query.filter_by(parent_id=node.id, deleted_at=None)
                .order_by(Folder.name.asc())
                .all()
            )
            return {
                "folder": node,
                "children": [build(child) for child in children],
            }

        return [build(root)]

    @staticmethod
    def list_folder_contents(
        user: User,
        folder: Folder,
        query: str | None = None,
        scope: str = "current",
        type_filter: str | None = None,
    ) -> tuple[list[Folder], list[File]]:
        folders_query = Folder.query.filter_by(parent_id=folder.id, deleted_at=None).order_by(Folder.name.asc())
        files_query = File.query.filter(
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        ).order_by(File.created_at.desc())

        if folder.shared_drive_id:
            if not SharedDriveService.can_view(folder.shared_drive, user):
                raise AccessError("You do not have access to that shared drive.")
            folders_query = folders_query.filter(Folder.shared_drive_id == folder.shared_drive_id)
            files_query = files_query.filter(File.shared_drive_id == folder.shared_drive_id)
        elif not user.is_admin:
            files_query = files_query.filter(File.owner_id == user.id)
            folders_query = folders_query.filter(Folder.owner_id == user.id)

        if scope == "global":
            if folder.shared_drive_id:
                files_query = files_query.filter(File.shared_drive_id == folder.shared_drive_id)
            else:
                files_query = files_query.filter(File.owner_id == user.id)
        else:
            files_query = files_query.filter(File.folder_id == folder.id)

        if query:
            like_query = f"%{query.strip()}%"
            files_query = files_query.filter(File.filename.ilike(like_query))
            folders_query = folders_query.filter(Folder.name.ilike(like_query))

        if type_filter and type_filter != "all":
            files_query = FileService._apply_type_filter(files_query, type_filter)

        folders = folders_query.all()
        files = files_query.all()
        return folders, files

    @staticmethod
    def recent_files(user: User, limit: int = 6, shared_drive: SharedDrive | None = None) -> list[File]:
        query = File.query.filter(
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        )
        if shared_drive is not None:
            query = query.filter(File.shared_drive_id == shared_drive.id)
        else:
            query = query.filter(File.owner_id == user.id, File.shared_drive_id.is_(None))
        return query.order_by(File.created_at.desc()).limit(limit).all()

    @staticmethod
    def usage_summary(user: User | None = None, shared_drive: SharedDrive | None = None) -> dict[str, object]:
        file_count_query = db.session.query(func.count(File.id)).filter(
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        )
        if shared_drive is not None:
            file_count_query = file_count_query.filter(File.shared_drive_id == shared_drive.id)
            total_used = FileService.current_usage_bytes(shared_drive=shared_drive)
            quota_bytes = int(shared_drive.storage_quota_bytes or 0)
        else:
            if user is None:
                raise ValueError("A user or shared drive is required for usage summary.")
            file_count_query = file_count_query.filter(
                File.owner_id == user.id,
                File.shared_drive_id.is_(None),
            )
            total_used = FileService.current_usage_bytes(user=user)
            quota_bytes = AuthService.storage_quota_bytes_for_user(user, config=current_app.config)
        total_files = int(file_count_query.scalar() or 0)
        is_unlimited = quota_bytes <= 0
        remaining_bytes = None if is_unlimited else max(quota_bytes - total_used, 0)
        percent = min(100, int((total_used / quota_bytes) * 100)) if quota_bytes > 0 else 0
        return {
            "total_used": total_used,
            "total_files": total_files,
            "quota_bytes": quota_bytes,
            "remaining_bytes": remaining_bytes,
            "is_unlimited": is_unlimited,
            "can_upload": is_unlimited or (remaining_bytes or 0) > 0,
            "percent_used": percent,
        }

    @staticmethod
    def create_folder(user: User, parent_folder: Folder, name: str) -> Folder:
        FileService._ensure_can_write_folder(user, parent_folder)
        validated_name = validate_folder_name(name)
        folder = Folder(
            name=FileService._make_unique_folder_name(parent_folder, validated_name),
            parent_id=parent_folder.id,
            owner_id=user.id if parent_folder.shared_drive_id else parent_folder.owner_id,
            shared_drive_id=parent_folder.shared_drive_id,
        )
        db.session.add(folder)
        db.session.commit()
        ActivityService.log(
            action="folder.created",
            target_type="folder",
            target_id=folder.id,
            user_id=user.id,
            metadata={
                "parent_id": parent_folder.id,
                "shared_drive_id": parent_folder.shared_drive_id,
            },
        )
        return folder

    @staticmethod
    def rename_folder(user: User, folder: Folder, name: str) -> Folder:
        if folder.is_root:
            raise ValidationError("The root folder cannot be renamed.")
        FileService._ensure_can_write_folder(user, folder)
        parent_folder = folder.parent
        if parent_folder is None:
            raise ValidationError("Folder parent could not be resolved.")
        folder.name = FileService._make_unique_folder_name(
            parent_folder,
            validate_folder_name(name),
            existing_folder_id=folder.id,
        )
        folder.updated_at = utcnow()
        db.session.commit()
        ActivityService.log(
            action="folder.renamed",
            target_type="folder",
            target_id=folder.id,
            user_id=user.id,
            metadata={"name": folder.name},
        )
        return folder

    @staticmethod
    def move_folder(user: User, folder: Folder, destination_folder: Folder) -> Folder:
        if folder.is_root:
            raise ValidationError("The root folder cannot be moved.")
        if folder.id == destination_folder.id:
            raise ValidationError("Folder cannot be moved into itself.")
        if FileService._is_descendant(destination_folder, folder):
            raise ValidationError("Folder cannot be moved into one of its children.")
        FileService._ensure_can_write_folder(user, folder)
        FileService._ensure_can_write_folder(user, destination_folder)
        if folder.shared_drive_id != destination_folder.shared_drive_id:
            raise ValidationError("Folder cannot be moved between different drives.")
        if folder.shared_drive_id is None and folder.owner_id != destination_folder.owner_id and not user.is_admin:
            raise AccessError("Folder ownership mismatch.")

        folder.parent_id = destination_folder.id
        folder.name = FileService._make_unique_folder_name(
            destination_folder,
            folder.name,
            existing_folder_id=folder.id,
        )
        folder.updated_at = utcnow()
        db.session.commit()
        ActivityService.log(
            action="folder.moved",
            target_type="folder",
            target_id=folder.id,
            user_id=user.id,
            metadata={"destination_folder_id": destination_folder.id},
        )
        return folder

    @staticmethod
    def delete_folder(user: User, folder: Folder, hard_delete: bool = False) -> None:
        if folder.is_root:
            raise ValidationError("The root folder cannot be deleted.")
        FileService._ensure_can_write_folder(user, folder)

        child_folders = Folder.query.filter_by(parent_id=folder.id, deleted_at=None).all()
        child_files = File.query.filter_by(folder_id=folder.id, deleted_at=None).all()
        for child in child_folders:
            FileService.delete_folder(user, child, hard_delete=hard_delete)
        for child_file in child_files:
            FileService.delete_file(user, child_file, hard_delete=hard_delete)

        if hard_delete:
            db.session.delete(folder)
        else:
            folder.deleted_at = utcnow()
        db.session.commit()
        ActivityService.log(
            action="folder.deleted",
            target_type="folder",
            target_id=folder.id,
            user_id=user.id,
            metadata={"hard_delete": hard_delete},
        )

    @staticmethod
    def upload_files(user: User, folder: Folder, uploads: list, config) -> list[File]:
        uploaded_records: list[File] = []
        for upload in uploads:
            if not upload or not upload.filename:
                continue
            uploaded_records.append(FileService.upload_single_file(user, folder, upload, config))
        return uploaded_records

    @staticmethod
    def effective_max_upload_size(config) -> int:
        """Per-request upload ceiling.

        Direct requests (e.g. LAN hosts listed in APP_EXTERNAL_URLS) get the
        full MAX_UPLOAD_SIZE_BYTES. The lower Cloudflare cap only applies to
        requests that actually reach NovaDrive through Cloudflare.
        """
        full_limit = config["MAX_UPLOAD_SIZE_BYTES"]
        if not config.get("CLOUDFLARE_TUNNEL_COMPAT"):
            return full_limit

        direct_hosts = config.get("APP_DIRECT_UPLOAD_HOSTS") or frozenset()
        if direct_hosts and has_request_context() and request.host.lower() in direct_hosts:
            return full_limit
        return config.get("CLOUDFLARE_PROXIED_MAX_UPLOAD_SIZE_BYTES", full_limit)

    @staticmethod
    def upload_single_file(user: User, folder: Folder, upload, config, existing_file: File | None = None) -> File:
        FileService._ensure_can_write_folder(user, folder)
        safe_original_filename = normalize_filename(upload.filename)
        mime_type = upload.mimetype or mimetypes.guess_type(safe_original_filename)[0] or "application/octet-stream"
        max_memory = config["SPOOL_MAX_MEMORY_BYTES"]

        spool = tempfile.SpooledTemporaryFile(max_size=max_memory, mode="w+b")
        digest = hashlib.sha256()
        total_size = 0
        max_upload_size = FileService.effective_max_upload_size(config)

        while True:
            buffer = upload.stream.read(1024 * 1024)
            if not buffer:
                break
            total_size += len(buffer)
            if total_size > max_upload_size:
                spool.close()
                if max_upload_size < config["MAX_UPLOAD_SIZE_BYTES"] and config.get("CLOUDFLARE_TUNNEL_COMPAT"):
                    plan = str(config.get("CLOUDFLARE_TUNNEL_PLAN", "free")).capitalize()
                    raise ValidationError(
                        f"This file cannot be uploaded because this NovaDrive instance is running through "
                        f"Cloudflare {plan} tier compatibility mode. Maximum upload size: "
                        f"{FileService._format_bytes(max_upload_size)}."
                    )
                raise ValidationError(
                    f"This file cannot be uploaded because it exceeds the maximum upload size of "
                    f"{FileService._format_bytes(max_upload_size)}."
                )
            digest.update(buffer)
            spool.write(buffer)

        return FileService._persist_file(
            user,
            folder,
            spool,
            total_size=total_size,
            sha_hex=digest.hexdigest(),
            safe_filename=safe_original_filename,
            mime_type=mime_type,
            config=config,
            existing_file=existing_file,
        )

    @staticmethod
    def store_stream(
        user: User,
        folder: Folder,
        source_stream: BinaryIO,
        *,
        filename: str,
        config,
        mime_type: str | None = None,
    ) -> File:
        """Ingest an arbitrary binary stream as a new file.

        This is the shared entry point for non-browser sources such as remote
        URL downloads. The stream is buffered to a spool while its size and
        SHA-256 are computed, then handed to the same storage path used by
        browser uploads. The absolute ``MAX_UPLOAD_SIZE_BYTES`` ceiling is
        enforced (the Cloudflare-proxied cap does not apply to server-side
        downloads).
        """
        FileService._ensure_can_write_folder(user, folder)
        safe_filename = normalize_filename(filename)
        resolved_mime = (
            mime_type
            or mimetypes.guess_type(safe_filename)[0]
            or "application/octet-stream"
        )
        max_upload_size = config["MAX_UPLOAD_SIZE_BYTES"]

        spool = tempfile.SpooledTemporaryFile(max_size=config["SPOOL_MAX_MEMORY_BYTES"], mode="w+b")
        digest = hashlib.sha256()
        total_size = 0
        while True:
            buffer = source_stream.read(1024 * 1024)
            if not buffer:
                break
            total_size += len(buffer)
            if total_size > max_upload_size:
                spool.close()
                raise ValidationError(
                    "The remote file exceeds the maximum allowed size of "
                    f"{FileService._format_bytes(max_upload_size)}."
                )
            digest.update(buffer)
            spool.write(buffer)

        return FileService._persist_file(
            user,
            folder,
            spool,
            total_size=total_size,
            sha_hex=digest.hexdigest(),
            safe_filename=safe_filename,
            mime_type=resolved_mime,
            config=config,
        )

    @staticmethod
    def _persist_file(
        user: User,
        folder: Folder,
        spool,
        *,
        total_size: int,
        sha_hex: str,
        safe_filename: str,
        mime_type: str,
        config,
        existing_file: File | None = None,
    ) -> File:
        """Quota-check a buffered spool and store it via the active backend.

        Shared by browser uploads and remote downloads. ``spool`` must be a
        seekable binary file; it is always closed before returning.
        """
        chunk_size = config["DISCORD_CHUNK_SIZE_BYTES"]
        FileService._ensure_storage_quota(
            user,
            total_size,
            config,
            existing_file=existing_file,
            shared_drive=folder.shared_drive,
        )
        spool.seek(0)
        backend_name = (
            existing_file.manifest.storage_backend
            if existing_file and existing_file.manifest and existing_file.manifest.storage_backend
            else configured_storage_backend_name(config)
        )
        backend = get_storage_backend(config, backend_name=backend_name)
        whole_file = getattr(backend, "whole_file", False)
        manifest_chunk_size = total_size if whole_file else chunk_size
        file_record = existing_file or File(
            folder_id=folder.id,
            owner_id=user.id if folder.shared_drive_id else folder.owner_id,
            shared_drive_id=folder.shared_drive_id,
            filename=FileService._make_unique_filename(folder.id, safe_filename),
            original_filename=safe_filename,
            mime_type=mime_type,
            total_size=total_size,
            total_chunks=0,
            sha256=sha_hex,
            upload_status="uploading",
        )
        db.session.add(file_record)
        db.session.flush()

        if not file_record.manifest:
            file_record.manifest = FileManifest(
                storage_backend=backend_name,
                chunk_size=manifest_chunk_size,
                upload_session_token=secrets.token_urlsafe(18),
                metadata_json=json.dumps(
                    {
                        "original_filename": safe_filename,
                        "mime_type": mime_type,
                    }
                ),
            )
            db.session.flush()

        existing_chunks = {chunk.chunk_index: chunk for chunk in file_record.chunks}
        uploaded_chunk_count = 0

        try:
            if whole_file:
                uploaded_chunk_count = FileService._store_whole_file(
                    backend,
                    file_record,
                    spool,
                    total_size,
                    existing_chunks,
                )
            else:
                for chunk_index, chunk_bytes in iter_file_chunks(spool, chunk_size):
                    if chunk_index in existing_chunks:
                        uploaded_chunk_count += 1
                        continue

                    channel_id = backend.choose_channel(file_record.id, chunk_index)
                    chunk_sha = sha256_bytes(chunk_bytes)
                    storage_payload = backend.upload_chunk(
                        chunk_bytes=chunk_bytes,
                        filename=f"{file_record.id}-{chunk_index:06d}.part",
                        sha256=chunk_sha,
                        channel_id=channel_id,
                        metadata={**FileService.storage_metadata(file_record), "chunk_index": chunk_index},
                    )
                    file_chunk = FileChunk(
                        file_id=file_record.id,
                        chunk_index=chunk_index,
                        discord_channel_id=str(storage_payload["channel_id"]),
                        discord_message_id=str(storage_payload["message_id"]),
                        discord_attachment_url=storage_payload["attachment_url"],
                        discord_attachment_filename=storage_payload.get("attachment_filename"),
                        chunk_size=len(chunk_bytes),
                        sha256=chunk_sha,
                    )
                    db.session.add(file_chunk)
                    db.session.commit()
                    uploaded_chunk_count += 1

            file_record.total_chunks = uploaded_chunk_count
            file_record.upload_status = "complete"
            file_record.updated_at = utcnow()
            if file_record.manifest:
                file_record.manifest.last_verified_at = utcnow()
            db.session.commit()

            ActivityService.log(
                action="file.uploaded",
                target_type="file",
                target_id=file_record.id,
                user_id=user.id,
                metadata={
                    "folder_id": folder.id,
                    "size": total_size,
                    "chunks": uploaded_chunk_count,
                },
            )
            return file_record
        except Exception:
            logger.exception("File store failed for %s", safe_filename)
            file_record.upload_status = "failed"
            db.session.commit()
            raise
        finally:
            spool.close()

    @staticmethod
    def _folder_relative_path(folder: Folder | None) -> str:
        """Path of ``folder`` relative to its drive root, e.g. ``Photos/2024``."""
        parts: list[str] = []
        node = folder
        while node is not None and not node.is_root:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    @staticmethod
    def storage_metadata(file_record: File) -> dict[str, object]:
        """Locator hints passed to the storage backend (used for the S3 key)."""
        owner = file_record.owner
        return {
            "file_id": file_record.id,
            "filename": file_record.filename,
            "username": owner.username if owner else "unknown",
            "folder_path": FileService._folder_relative_path(file_record.folder),
        }

    @staticmethod
    def _store_whole_file(
        backend,
        file_record: File,
        spool,
        total_size: int,
        existing_chunks: dict,
    ) -> int:
        """Upload a file as a single object (used by backends like S3).

        Returns the number of stored chunks (always 1). Any previously stored
        chunk records are removed first so re-uploads overwrite cleanly.
        """
        for chunk in list(existing_chunks.values()):
            try:
                backend.delete_chunk(chunk.discord_channel_id, chunk.discord_message_id)
            except StorageBackendError:
                logger.warning(
                    "Failed to delete previous object for file %s before re-upload",
                    file_record.id,
                )
            db.session.delete(chunk)
        db.session.flush()

        channel_id = backend.choose_channel(file_record.id, 0)
        spool.seek(0)
        storage_payload = backend.upload_whole(
            stream=spool,
            filename=f"{file_record.id}-000000.part",
            sha256=file_record.sha256,
            channel_id=channel_id,
            metadata={**FileService.storage_metadata(file_record), "chunk_index": 0},
        )
        file_chunk = FileChunk(
            file_id=file_record.id,
            chunk_index=0,
            discord_channel_id=str(storage_payload["channel_id"]),
            discord_message_id=str(storage_payload["message_id"]),
            discord_attachment_url=storage_payload["attachment_url"],
            discord_attachment_filename=storage_payload.get("attachment_filename"),
            chunk_size=total_size,
            sha256=file_record.sha256,
        )
        db.session.add(file_chunk)
        db.session.commit()
        return 1

    @staticmethod
    def open_download(file_record: File, config, start: int | None = None, end: int | None = None):
        """Open a file for streaming delivery.

        Returns ``(iterator, content_length)``. The first storage access happens
        eagerly here so a missing object / backend error surfaces to the request
        handler (graceful 500) instead of crashing mid-stream in the WSGI
        server. ``start``/``end`` are ``None`` for a full download, or the
        inclusive byte range for a partial request.
        """
        if file_record.upload_status != "complete" or file_record.is_deleted:
            raise ValidationError("This file is not available for download.")

        total_size = int(file_record.total_size or 0)
        is_range = start is not None
        if not is_range:
            start, end = 0, max(total_size - 1, 0)

        chunk_records = (
            FileChunk.query.filter_by(file_id=file_record.id)
            .order_by(FileChunk.chunk_index.asc())
            .all()
        )
        backend_name = (
            file_record.manifest.storage_backend
            if file_record.manifest and file_record.manifest.storage_backend
            else configured_storage_backend_name(config)
        )
        backend = get_storage_backend(config, backend_name=backend_name)

        if total_size <= 0:
            return iter(()), 0

        whole_file = getattr(backend, "whole_file", False) and len(chunk_records) == 1
        if whole_file and hasattr(backend, "open_object"):
            chunk = chunk_records[0]
            body, length = backend.open_object(
                chunk.discord_channel_id,
                chunk.discord_message_id,
                start if is_range else None,
                end if is_range else None,
            )

            def _stream_body():
                for piece in body.iter_chunks(chunk_size=1024 * 1024):
                    if piece:
                        yield piece

            return _stream_body(), (length or (end - start + 1))

        # Per-chunk path (Discord / legacy multi-chunk S3). Prime the first chunk
        # eagerly so a fetch error surfaces before we start the response.
        generator = FileService._iter_chunks_range(backend, chunk_records, start, end)
        try:
            first_piece = next(generator)
        except StopIteration:
            return iter(()), 0

        def _primed():
            yield first_piece
            yield from generator

        return _primed(), (end - start + 1)

    @staticmethod
    def _iter_chunks_range(backend, chunk_records, start: int, end: int):
        offset = 0
        for chunk in chunk_records:
            chunk_len = int(chunk.chunk_size or 0)
            chunk_start = offset
            chunk_end = offset + chunk_len - 1
            offset += chunk_len
            if chunk_end < start:
                continue
            if chunk_start > end:
                break
            data = backend.fetch_chunk(
                channel_id=chunk.discord_channel_id,
                message_id=chunk.discord_message_id,
            )
            slice_start = start - chunk_start if start > chunk_start else 0
            slice_end = (end - chunk_start + 1) if end < chunk_end else chunk_len
            yield data[slice_start:slice_end]

    @staticmethod
    def rebuild_file(file_record: File, config) -> tuple[BinaryIO, str]:
        if file_record.upload_status != "complete" or file_record.is_deleted:
            raise ValidationError("This file is not available for download.")

        chunk_records = (
            FileChunk.query.filter_by(file_id=file_record.id)
            .order_by(FileChunk.chunk_index.asc())
            .all()
        )
        validate_chunk_indexes(
            [chunk.chunk_index for chunk in chunk_records],
            file_record.total_chunks,
        )

        backend_name = (
            file_record.manifest.storage_backend
            if file_record.manifest and file_record.manifest.storage_backend
            else configured_storage_backend_name(config)
        )
        backend = get_storage_backend(config, backend_name=backend_name)
        output = tempfile.SpooledTemporaryFile(max_size=config["SPOOL_MAX_MEMORY_BYTES"], mode="w+b")
        digest = hashlib.sha256()

        # Files stored as a single object (e.g. S3) are streamed straight into
        # the output so multi-GiB downloads never buffer the whole body in
        # memory. Legacy multi-chunk files keep using the per-chunk path.
        whole_file = getattr(backend, "whole_file", False) and len(chunk_records) == 1

        try:
            if whole_file:
                chunk = chunk_records[0]
                backend.fetch_whole(
                    channel_id=chunk.discord_channel_id,
                    message_id=chunk.discord_message_id,
                    dest_stream=output,
                    hasher=digest,
                )
            else:
                for chunk in chunk_records:
                    chunk_bytes = backend.fetch_chunk(
                        channel_id=chunk.discord_channel_id,
                        message_id=chunk.discord_message_id,
                    )
                    if sha256_bytes(chunk_bytes) != chunk.sha256:
                        raise ChunkValidationError(
                            f"Checksum mismatch while rebuilding chunk {chunk.chunk_index}."
                        )
                    digest.update(chunk_bytes)
                    output.write(chunk_bytes)

            final_hash = digest.hexdigest()
            if final_hash != file_record.sha256:
                raise ChunkValidationError("Final rebuilt file hash does not match stored SHA256.")

            output.seek(0)
            if file_record.manifest:
                file_record.manifest.last_verified_at = utcnow()
                db.session.commit()
            return output, final_hash
        except Exception:
            output.close()
            raise

    @staticmethod
    def rename_file(user: User, file_record: File, new_name: str) -> File:
        FileService._ensure_can_write_file(user, file_record)
        file_record.filename = FileService._make_unique_filename(
            file_record.folder_id,
            normalize_filename(new_name),
            existing_file_id=file_record.id,
        )
        file_record.updated_at = utcnow()
        db.session.commit()
        ActivityService.log(
            action="file.renamed",
            target_type="file",
            target_id=file_record.id,
            user_id=user.id,
            metadata={"filename": file_record.filename},
        )
        return file_record

    @staticmethod
    def move_file(user: User, file_record: File, destination_folder: Folder) -> File:
        FileService._ensure_can_write_file(user, file_record)
        FileService._ensure_can_write_folder(user, destination_folder)
        if file_record.shared_drive_id != destination_folder.shared_drive_id:
            raise ValidationError("File cannot be moved between different drives.")
        if file_record.shared_drive_id is None and file_record.owner_id != destination_folder.owner_id and not user.is_admin:
            raise AccessError("File ownership mismatch.")
        file_record.folder_id = destination_folder.id
        file_record.filename = FileService._make_unique_filename(
            destination_folder.id,
            file_record.filename,
            existing_file_id=file_record.id,
        )
        file_record.updated_at = utcnow()
        db.session.commit()
        ActivityService.log(
            action="file.moved",
            target_type="file",
            target_id=file_record.id,
            user_id=user.id,
            metadata={"destination_folder_id": destination_folder.id},
        )
        return file_record

    @staticmethod
    def delete_file(user: User, file_record: File, hard_delete: bool = False) -> None:
        FileService._ensure_can_write_file(user, file_record)
        file_id = file_record.id
        if hard_delete:
            backend_name = (
                file_record.manifest.storage_backend
                if file_record.manifest and file_record.manifest.storage_backend
                else configured_storage_backend_name(current_app.config)
            )
            backend = get_storage_backend(current_app.config, backend_name=backend_name)
            for chunk in file_record.chunks:
                try:
                    backend.delete_chunk(chunk.discord_channel_id, chunk.discord_message_id)
                except StorageBackendError:
                    logger.warning(
                        "Failed to delete stored chunk %s for file %s",
                        chunk.id,
                        file_record.id,
                    )
            # Rows that reference the file via a NOT NULL FK must go first.
            ShareLink.query.filter_by(file_id=file_id).delete(synchronize_session=False)
            ExternalUpload.query.filter_by(file_id=file_id).delete(synchronize_session=False)
            db.session.delete(file_record)
        else:
            file_record.deleted_at = utcnow()
            file_record.upload_status = "deleted"
        db.session.commit()
        ActivityService.log(
            action="file.deleted",
            target_type="file",
            target_id=file_id,
            user_id=user.id,
            metadata={"hard_delete": hard_delete},
        )

    @staticmethod
    def _make_unique_filename(
        folder_id: int,
        desired_filename: str,
        existing_file_id: int | None = None,
    ) -> str:
        stem, dot, extension = desired_filename.partition(".")
        candidate = desired_filename
        counter = 1

        while True:
            query = File.query.filter(
                File.folder_id == folder_id,
                File.deleted_at.is_(None),
                File.filename == candidate,
            )
            if existing_file_id is not None:
                query = query.filter(File.id != existing_file_id)
            if not query.first():
                return candidate
            suffix = f" ({counter})"
            candidate = f"{stem}{suffix}{dot}{extension}" if dot else f"{stem}{suffix}"
            counter += 1

    @staticmethod
    def _make_unique_folder_name(
        parent_folder: Folder,
        desired_name: str,
        existing_folder_id: int | None = None,
    ) -> str:
        candidate = desired_name
        counter = 1
        while True:
            query = Folder.query.filter(
                Folder.parent_id == parent_folder.id,
                Folder.deleted_at.is_(None),
                Folder.name == candidate,
            )
            if parent_folder.shared_drive_id is not None:
                query = query.filter(Folder.shared_drive_id == parent_folder.shared_drive_id)
            else:
                query = query.filter(Folder.owner_id == parent_folder.owner_id)
            if existing_folder_id is not None:
                query = query.filter(Folder.id != existing_folder_id)
            if not query.first():
                return candidate
            candidate = f"{desired_name} ({counter})"
            counter += 1

    @staticmethod
    def _is_descendant(candidate: Folder, ancestor: Folder) -> bool:
        current = candidate
        while current is not None:
            if current.id == ancestor.id:
                return True
            current = current.parent
        return False

    @staticmethod
    def _apply_type_filter(query, type_filter: str):
        if type_filter == "image":
            return query.filter(File.mime_type.like("image/%"))
        if type_filter == "video":
            return query.filter(File.mime_type.like("video/%"))
        if type_filter == "audio":
            return query.filter(File.mime_type.like("audio/%"))
        if type_filter == "document":
            return query.filter(
                File.mime_type.like("application/%") | File.mime_type.like("text/%")
            )
        return query

    @staticmethod
    def _ensure_storage_quota(
        user: User,
        incoming_bytes: int,
        config,
        *,
        existing_file: File | None = None,
        shared_drive: SharedDrive | None = None,
    ) -> None:
        if shared_drive is not None:
            quota_bytes = int(shared_drive.storage_quota_bytes or 0)
        else:
            quota_bytes = AuthService.storage_quota_bytes_for_user(user, config=config)
        if quota_bytes <= 0:
            return

        current_usage = (
            FileService.current_usage_bytes(shared_drive=shared_drive)
            if shared_drive is not None
            else FileService.current_usage_bytes(user=user)
        )
        if existing_file and existing_file.upload_status == "complete" and not existing_file.is_deleted:
            current_usage = max(0, current_usage - existing_file.total_size)

        projected_usage = current_usage + incoming_bytes
        if projected_usage <= quota_bytes:
            return

        remaining_bytes = max(quota_bytes - current_usage, 0)
        if shared_drive is not None:
            if remaining_bytes <= 0:
                raise ValidationError(
                    "This shared drive is full. "
                    f"It has used all available storage ({FileService._format_bytes(quota_bytes)}). "
                    "Ask an admin to increase the shared drive storage cap or delete files."
                )
            raise ValidationError(
                "This upload cannot be completed because it would exceed the shared drive storage quota. "
                f"Remaining space: {FileService._format_bytes(remaining_bytes)} of "
                f"{FileService._format_bytes(quota_bytes)}."
            )

        if remaining_bytes <= 0:
            raise ValidationError(
                "Your storage is full. "
                f"You have used all available storage ({FileService._format_bytes(quota_bytes)}). "
                "Delete files or ask an admin to increase your storage quota."
            )

        raise ValidationError(
            "This upload cannot be completed because it would exceed your storage quota. "
            f"Remaining space: {FileService._format_bytes(remaining_bytes)} of "
            f"{FileService._format_bytes(quota_bytes)}."
        )

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    @staticmethod
    def _ensure_can_write_folder(user: User, folder: Folder) -> None:
        if folder.shared_drive_id:
            if not SharedDriveService.can_write(folder.shared_drive, user):
                raise AccessError("You do not have write access to that shared drive.")
            return
        if not user.is_admin and folder.owner_id != user.id:
            raise AccessError("You do not have write access to that folder.")

    @staticmethod
    def _ensure_can_write_file(user: User, file_record: File) -> None:
        if file_record.shared_drive_id:
            if not SharedDriveService.can_write(file_record.shared_drive, user):
                raise AccessError("You do not have write access to that shared drive.")
            return
        if not user.is_admin and file_record.owner_id != user.id:
            raise AccessError("You do not have write access to that file.")
