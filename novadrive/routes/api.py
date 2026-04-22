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
from werkzeug.datastructures import FileStorage

from novadrive.extensions import csrf
from novadrive.models import User, utcnow
from novadrive.services.auth_service import AuthService
from novadrive.services.file_delivery import FileDeliveryService
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.share_service import ShareService
from novadrive.utils.urls import external_url
from novadrive.utils.validators import ValidationError

api_bp = Blueprint("api", __name__, url_prefix="/api")


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


def _authenticate_api_request() -> User | None:
    header_value = request.headers.get("Authorization", "")
    bearer_prefix = "Bearer "
    if header_value.startswith(bearer_prefix):
        return AuthService.authenticate_api_key(header_value[len(bearer_prefix) :])

    return AuthService.authenticate_api_key(
        request.headers.get("X-NovaDrive-API-Key")
        or request.headers.get("api_key")
        or request.values.get("api_key")
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
