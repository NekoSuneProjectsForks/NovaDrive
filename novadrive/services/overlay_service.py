from __future__ import annotations

import json
import random

from flask import url_for
from sqlalchemy import or_

from novadrive.extensions import db
from novadrive.models import File, ObsOverlaySettings, User, utcnow
from novadrive.services.file_delivery import FileDeliveryService
from novadrive.services.file_service import AccessError, FileService


BACKGROUND_MEDIA_KINDS = {"image", "video"}
MEDIA_FILTERS = {"image", "video", "media"}
FIT_MODES = {"cover", "contain", "fill"}


class OverlayService:
    @staticmethod
    def get_or_create_settings(user: User) -> ObsOverlaySettings:
        settings = ObsOverlaySettings.query.filter_by(user_id=user.id).first()
        if settings:
            return settings

        settings = ObsOverlaySettings(user_id=user.id)
        db.session.add(settings)
        db.session.commit()
        return settings

    @staticmethod
    def update_background_settings(user: User, form) -> ObsOverlaySettings:
        settings = OverlayService.get_or_create_settings(user)
        folder_id = form.get("folder_id", type=int)
        if folder_id:
            folder = FileService.get_folder_or_404(user, folder_id)
            settings.folder_id = folder.id
        else:
            settings.folder_id = None

        media_filter = (form.get("media_filter") or "image").strip().lower()
        if media_filter not in MEDIA_FILTERS:
            raise ValueError("Invalid media filter.")
        settings.media_filter = media_filter

        fit_mode = (form.get("fit_mode") or "cover").strip().lower()
        if fit_mode not in FIT_MODES:
            raise ValueError("Invalid fit mode.")
        settings.fit_mode = fit_mode

        settings.slide_interval_seconds = OverlayService._bounded_int(
            form.get("slide_interval_seconds", type=int),
            minimum=2,
            maximum=3600,
            default=30,
        )
        settings.fade_duration_ms = OverlayService._bounded_int(
            form.get("fade_duration_ms", type=int),
            minimum=0,
            maximum=10000,
            default=1000,
        )
        settings.shuffle = form.get("shuffle") == "on"
        settings.selected_file_ids_json = json.dumps(
            OverlayService._validated_selected_file_ids(user, form.getlist("selected_file_ids"))
        )
        settings.updated_at = utcnow()
        db.session.commit()
        return settings

    @staticmethod
    def selectable_background_files(user: User) -> list[File]:
        return (
            File.query.filter(
                File.owner_id == user.id,
                File.shared_drive_id.is_(None),
                File.upload_status == "complete",
                File.deleted_at.is_(None),
                or_(
                    File.mime_type.like("image/%"),
                    File.mime_type.like("video/%"),
                ),
            )
            .order_by(File.created_at.desc())
            .all()
        )

    @staticmethod
    def background_files(user: User, settings: ObsOverlaySettings) -> list[File]:
        selected_ids = OverlayService.selected_file_ids(settings)
        if selected_ids:
            files_by_id: dict[int, File] = {}
            for file_id in selected_ids:
                try:
                    file_record = FileService.get_file_or_404(user, file_id)
                except (LookupError, AccessError):
                    continue
                if OverlayService.media_kind(file_record) in BACKGROUND_MEDIA_KINDS:
                    files_by_id[file_record.id] = file_record
            files = [files_by_id[file_id] for file_id in selected_ids if file_id in files_by_id]
            if settings.shuffle:
                random.shuffle(files)
            return files

        query = File.query.filter(
            File.owner_id == user.id,
            File.shared_drive_id.is_(None),
            File.upload_status == "complete",
            File.deleted_at.is_(None),
        )
        if settings.folder_id:
            try:
                folder = FileService.get_folder_or_404(user, settings.folder_id)
            except (LookupError, AccessError):
                folder = None
            if folder is not None:
                query = query.filter(File.folder_id == folder.id)

        query = OverlayService._apply_media_filter(query, settings.media_filter)
        files = query.order_by(File.created_at.desc()).limit(1000).all()
        if settings.shuffle:
            random.shuffle(files)
        return files

    @staticmethod
    def selected_file_ids(settings: ObsOverlaySettings) -> list[int]:
        try:
            parsed = json.loads(settings.selected_file_ids_json or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        selected_ids = []
        for value in parsed:
            try:
                selected_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return selected_ids

    @staticmethod
    def serialize_background_payload(
        user: User,
        settings: ObsOverlaySettings,
        files: list[File],
        api_key: str | None,
    ) -> dict[str, object]:
        assets = [
            OverlayService.serialize_asset(file_record, api_key)
            for file_record in files
            if OverlayService.media_kind(file_record) in BACKGROUND_MEDIA_KINDS
        ]
        photos = [asset["url"] for asset in assets if asset["kind"] == "image"]
        videos = [asset["url"] for asset in assets if asset["kind"] == "video"]
        return {
            "success": True,
            "username": user.username,
            "count": len(assets),
            "images": [asset["url"] for asset in assets],
            "photos": photos,
            "videos": videos,
            "assets": assets,
            "options": {
                "media_filter": settings.media_filter,
                "fit_mode": settings.fit_mode,
                "slide_interval_seconds": settings.slide_interval_seconds,
                "fade_duration_ms": settings.fade_duration_ms,
                "shuffle": settings.shuffle,
            },
        }

    @staticmethod
    def serialize_asset(file_record: File, api_key: str | None) -> dict[str, object]:
        kind = OverlayService.media_kind(file_record) or "file"
        media_url_values: dict[str, object] = {
            "file_id": file_record.id,
            "filename": file_record.filename,
        }
        if api_key:
            media_url_values["api_key"] = api_key
        raw_url = url_for("api.media_raw", **media_url_values)
        return {
            "id": file_record.id,
            "filename": file_record.filename,
            "size": file_record.total_size,
            "mime_type": file_record.mime_type,
            "kind": kind,
            "url": raw_url,
            "raw_url": raw_url,
            "thumbnail_url": raw_url if kind == "image" else None,
            "created_at": file_record.created_at.isoformat() if file_record.created_at else None,
        }

    @staticmethod
    def media_kind(file_record: File) -> str | None:
        kind = FileDeliveryService.preview_kind(file_record)
        return kind if kind in BACKGROUND_MEDIA_KINDS else None

    @staticmethod
    def _apply_media_filter(query, media_filter: str):
        if media_filter == "image":
            return query.filter(File.mime_type.like("image/%"))
        if media_filter == "video":
            return query.filter(File.mime_type.like("video/%"))
        return query.filter(
            or_(
                File.mime_type.like("image/%"),
                File.mime_type.like("video/%"),
            )
        )

    @staticmethod
    def _validated_selected_file_ids(user: User, raw_values: list[str]) -> list[int]:
        selected_ids: list[int] = []
        seen: set[int] = set()
        for raw_value in raw_values:
            try:
                file_id = int(raw_value)
            except (TypeError, ValueError):
                continue
            if file_id in seen:
                continue
            try:
                file_record = FileService.get_file_or_404(user, file_id)
            except (LookupError, AccessError):
                continue
            if OverlayService.media_kind(file_record) not in BACKGROUND_MEDIA_KINDS:
                continue
            selected_ids.append(file_id)
            seen.add(file_id)
        return selected_ids

    @staticmethod
    def _bounded_int(value: int | None, *, minimum: int, maximum: int, default: int) -> int:
        if value is None:
            return default
        return min(max(int(value), minimum), maximum)
