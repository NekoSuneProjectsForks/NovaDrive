from __future__ import annotations

import io
import json

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_
from werkzeug.datastructures import FileStorage

from novadrive.extensions import csrf
from novadrive.models import File, User, utcnow
from novadrive.services.auth_service import AuthService
from novadrive.services.file_delivery import FileDeliveryService
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.overlay_service import OverlayService
from novadrive.services.share_service import ShareService
from novadrive.utils.urls import external_url
from novadrive.utils.validators import ValidationError

api_bp = Blueprint("api", __name__, url_prefix="/api")

MEDIA_KINDS = {"image", "video", "audio"}
MEDIA_RESPONSE_KEYS = {
    "image": "photos",
    "video": "videos",
    "audio": "audios",
}
MAX_GALLERY_LIMIT = 1000


@api_bp.post("/sharex/upload")
@csrf.exempt
def sharex_upload():
    user = _authenticate_api_request()
    if not user:
        return jsonify({"success": False, "error": "Invalid or missing API key."}), 401

    try:
        if not current_app.config["ALLOW_PUBLIC_SHARING"]:
            raise ValidationError("Public sharing must be enabled for ShareX uploads.")

        folder = _resolve_target_folder(user)
        uploads = _collect_request_uploads()
        if not uploads:
            text_upload = _build_text_upload()
            if text_upload is not None:
                uploads = [text_upload]

        if not uploads:
            raise ValidationError("No file or text payload was provided.")

        uploaded_records = FileService.upload_files(user, folder, uploads, current_app.config)
        if not uploaded_records:
            raise ValidationError("No valid uploads were found in the request.")

        uploads_payload = [_build_share_payload(record, user) for record in uploaded_records]
        primary = uploads_payload[0]
        return (
            jsonify(
                {
                    "success": True,
                    "url": primary["url"],
                    "download_url": primary["download_url"],
                    "raw_url": primary["raw_url"],
                    "thumbnail_url": primary["thumbnail_url"],
                    "kind": primary["kind"],
                    "uploads": uploads_payload,
                }
            ),
            201,
        )
    except (LookupError, AccessError):
        return jsonify({"success": False, "error": "Folder not found."}), 404
    except (ValidationError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("ShareX upload failed.")
        return jsonify({"success": False, "error": "Upload failed unexpectedly."}), 500


@api_bp.get("/sharex/config.sxcu")
@login_required
def sharex_config():
    folder_id = request.args.get("folder_id", type=int)
    if folder_id:
        FileService.get_folder_or_404(current_user, folder_id)

    api_key = session.get("nova_generated_api_key")
    if not api_key:
        api_key = AuthService.generate_api_key(current_user)
        session["nova_generated_api_key"] = api_key

    request_url = external_url(
        "api.sharex_upload",
        folder_id=folder_id,
    )
    payload = {
        "Version": "17.0.0",
        "Name": f"NovaDrive ({current_user.username})",
        "DestinationType": "ImageUploader, TextUploader, FileUploader",
        "RequestMethod": "POST",
        "RequestURL": request_url,
        "Headers": {
            "X-NovaDrive-API-Key": api_key,
        },
        "Body": "MultipartFormData",
        "Arguments": {
            "text": "{input}",
            "filename": "{filename}",
        },
        "FileFormName": "file",
        "URL": "{json:url}",
        "ThumbnailURL": "{json:thumbnail_url}",
        "ErrorMessage": "{json:error}",
    }
    filename = f"novadrive-{current_user.username}.sxcu"
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@api_bp.get("/gallery")
def gallery():
    user = _authenticate_api_request()
    if not user:
        return _api_error("Invalid or missing API key.", 401)

    try:
        files = _query_gallery_files(user)
    except LookupError:
        return _api_error("Folder not found.", 404)
    except AccessError:
        return _api_error("You do not have access to that folder.", 403)
    except ValueError as exc:
        return _api_error(str(exc), 400)

    response = jsonify(_build_gallery_payload(user, files))
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@api_bp.get("/media/<int:file_id>/raw")
@api_bp.get("/media/<int:file_id>/<path:filename>")
def media_raw(file_id: int, filename: str | None = None):
    user = _authenticate_api_request()
    if not user:
        return _api_error("Invalid or missing API key.", 401)

    try:
        file_record = FileService.get_file_or_404(user, file_id)
    except LookupError:
        return _api_error("File not found.", 404)
    except AccessError:
        return _api_error("You do not have access to that file.", 403)

    if _media_kind(file_record) not in MEDIA_KINDS:
        return _api_error("Only image, video, and audio files can be served by this media endpoint.", 415)

    response = FileDeliveryService.build_response(
        file_record,
        current_app.config,
        as_attachment=False,
        download_name=file_record.filename,
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@api_bp.get("/overlay/background")
def overlay_background():
    user = _authenticate_api_request()
    if not user:
        return _api_error("Invalid or missing API key.", 401)

    settings = OverlayService.get_or_create_settings(user)
    files = OverlayService.background_files(user, settings)
    response = jsonify(
        OverlayService.serialize_background_payload(
            user,
            settings,
            files,
            _api_key_from_request(),
        )
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _authenticate_api_request() -> User | None:
    return AuthService.authenticate_api_key(_api_key_from_request())


def _api_key_from_request() -> str | None:
    header_value = request.headers.get("Authorization", "")
    bearer_prefix = "Bearer "
    if header_value.startswith(bearer_prefix):
        return header_value[len(bearer_prefix) :]

    return (
        request.headers.get("X-NovaDrive-API-Key")
        or request.headers.get("X-API-Key")
        or request.headers.get("api_key")
        or request.headers.get("apikey")
        or request.values.get("api_key")
        or request.values.get("apikey")
        or request.values.get("key")
    )


def _resolve_target_folder(user: User):
    folder_id = request.args.get("folder_id", type=int) or request.form.get("folder_id", type=int)
    if folder_id:
        return FileService.get_folder_or_404(user, folder_id)
    return FileService.get_accessible_root_folder(user)


def _collect_request_uploads() -> list[FileStorage]:
    uploads: list[FileStorage] = []
    for field_name in request.files:
        uploads.extend(
            uploaded_file
            for uploaded_file in request.files.getlist(field_name)
            if uploaded_file and uploaded_file.filename
        )
    return uploads


def _build_text_upload() -> FileStorage | None:
    text_value = request.form.get("text") or request.form.get("content") or request.form.get("input")
    filename = request.form.get("filename") or request.form.get("title")
    content_type = request.form.get("content_type") or "text/plain; charset=utf-8"

    if text_value is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        text_value = payload.get("text") or payload.get("content") or payload.get("input")
        filename = filename or payload.get("filename") or payload.get("title")
        content_type = payload.get("content_type", content_type)

    if text_value is None:
        raw_body = request.get_data(cache=True, as_text=True)
        if raw_body and request.mimetype in {
            "text/plain",
            "application/json",
            "application/x-www-form-urlencoded",
        }:
            text_value = raw_body
            content_type = request.content_type or content_type

    if text_value is None:
        return None

    resolved_filename = (filename or "").strip() or _default_text_filename(content_type)
    if "." not in resolved_filename:
        resolved_filename = f"{resolved_filename}.txt"

    return FileStorage(
        stream=io.BytesIO(text_value.encode("utf-8")),
        filename=resolved_filename,
        name="file",
        content_type=content_type,
    )


def _default_text_filename(content_type: str) -> str:
    extension = "txt"
    if content_type.startswith("application/json"):
        extension = "json"
    return f"sharex-{utcnow().strftime('%Y%m%d-%H%M%S')}.{extension}"


def _build_share_payload(file_record, user: User) -> dict[str, object]:
    share_link = ShareService.create_link(file_record=file_record, user_id=user.id)
    preview_kind = FileDeliveryService.preview_kind(file_record) or "file"
    share_url = external_url("share.view", token=share_link.token)
    raw_url = external_url("share.raw", token=share_link.token)
    download_url = external_url("share.download", token=share_link.token)
    return {
        "id": file_record.id,
        "filename": file_record.filename,
        "size": file_record.total_size,
        "mime_type": file_record.mime_type,
        "kind": preview_kind,
        "url": share_url,
        "download_url": download_url,
        "raw_url": raw_url,
        "thumbnail_url": raw_url if preview_kind == "image" else None,
    }


def _query_gallery_files(user: User) -> list[File]:
    folder_id = request.args.get("folder_id", type=int)
    scope = (request.args.get("scope") or "global").strip().lower()
    type_filter = (request.args.get("type") or "media").strip().lower()
    search_query = (request.args.get("q") or request.args.get("query") or "").strip()
    limit = _bounded_gallery_limit(request.args.get("limit", type=int))

    if scope not in {"current", "folder", "global", "all"}:
        raise ValueError("Invalid gallery scope.")

    files_query = File.query.filter(
        File.upload_status == "complete",
        File.deleted_at.is_(None),
    )

    if folder_id:
        folder = FileService.get_folder_or_404(user, folder_id)
        if scope in {"current", "folder"}:
            files_query = files_query.filter(File.folder_id == folder.id)
        elif folder.shared_drive_id:
            files_query = files_query.filter(File.shared_drive_id == folder.shared_drive_id)
        else:
            files_query = files_query.filter(
                File.owner_id == folder.owner_id,
                File.shared_drive_id.is_(None),
            )
    else:
        files_query = files_query.filter(
            File.owner_id == user.id,
            File.shared_drive_id.is_(None),
        )

    if search_query:
        files_query = files_query.filter(File.filename.ilike(f"%{search_query}%"))

    files_query = _apply_gallery_type_filter(files_query, type_filter)
    return files_query.order_by(File.created_at.desc()).limit(limit).all()


def _bounded_gallery_limit(value: int | None) -> int:
    if value is None:
        return 500
    return min(max(int(value), 1), MAX_GALLERY_LIMIT)


def _apply_gallery_type_filter(query, type_filter: str):
    if type_filter in {"image", "images", "photo", "photos"}:
        return query.filter(File.mime_type.like("image/%"))
    if type_filter in {"video", "videos"}:
        return query.filter(File.mime_type.like("video/%"))
    if type_filter in {"audio", "audios"}:
        return query.filter(File.mime_type.like("audio/%"))
    if type_filter in {"media", "background", "backgrounds", "overlay", "all"}:
        return query.filter(
            or_(
                File.mime_type.like("image/%"),
                File.mime_type.like("video/%"),
                File.mime_type.like("audio/%"),
            )
        )
    raise ValueError("Invalid gallery type filter.")


def _build_gallery_payload(user: User, files: list[File]) -> dict[str, object]:
    grouped_urls: dict[str, list[str]] = {
        "photos": [],
        "videos": [],
        "audios": [],
    }
    assets: list[dict[str, object]] = []

    for file_record in files:
        kind = _media_kind(file_record)
        if kind not in MEDIA_KINDS:
            continue

        raw_url = _media_url(file_record)
        asset = {
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
        assets.append(asset)
        grouped_urls[MEDIA_RESPONSE_KEYS[kind]].append(raw_url)

    return {
        "success": True,
        "username": user.username,
        "count": len(assets),
        "photos": grouped_urls["photos"],
        "videos": grouped_urls["videos"],
        "audios": grouped_urls["audios"],
        "assets": assets,
    }


def _media_url(file_record: File) -> str:
    values: dict[str, object] = {
        "file_id": file_record.id,
        "filename": file_record.filename,
    }
    api_key = _api_key_from_request()
    if api_key:
        values["api_key"] = api_key
    return url_for("api.media_raw", **values)


def _media_kind(file_record: File) -> str | None:
    kind = FileDeliveryService.preview_kind(file_record)
    return kind if kind in MEDIA_KINDS else None


def _api_error(message: str, status_code: int):
    response = jsonify({"success": False, "error": message})
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response, status_code
