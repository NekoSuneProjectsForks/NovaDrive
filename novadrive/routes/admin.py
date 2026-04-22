from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from novadrive.extensions import db
from novadrive.models import ActivityLog, File, Folder, SharedDrive, User, UserSession
from novadrive.services.auth_service import AuthService
from novadrive.services.email_service import EmailService
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.storage_base import StorageBackendError
from novadrive.services.storage_factory import (
    configured_storage_backend_name,
    get_storage_backend,
    storage_backend_label,
)
from novadrive.utils.decorators import admin_required
from novadrive.utils.validators import ValidationError

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _parse_quota_bytes(raw_value: str | None, *, allow_blank: bool = False) -> int | None:
    normalized_value = (raw_value or "").strip()
    if not normalized_value:
        if allow_blank:
            return None
        raise ValueError("Storage quota is required.")

    quota_gb = Decimal(normalized_value)
    if quota_gb < 0:
        raise ValueError("Storage quota must be zero or greater.")
    return int(quota_gb * (1024 ** 3))


def _storage_config_rows() -> list[dict[str, str]]:
    backend_name = configured_storage_backend_name(current_app.config)
    if backend_name == "s3":
        return [
            {"label": "Endpoint", "value": current_app.config["S3_ENDPOINT_URL"] or "aws-default"},
            {"label": "Bucket", "value": current_app.config["S3_BUCKET_NAME"] or "Not configured"},
            {"label": "Prefix", "value": current_app.config["S3_PREFIX"] or "/"},
        ]

    return [
        {"label": "Bridge URL", "value": current_app.config["DISCORD_BOT_BRIDGE_URL"]},
        {
            "label": "Channels",
            "value": ", ".join(str(channel_id) for channel_id in current_app.config["DISCORD_STORAGE_CHANNEL_IDS"])
            or "None configured",
        },
    ]


def _redirect_to_user_workspace(user_id: int, folder_id: int | None = None):
    if folder_id:
        return redirect(url_for("admin.user_details", user_id=user_id, folder_id=folder_id))
    return redirect(url_for("admin.user_details", user_id=user_id))


@admin_bp.route("/")
@login_required
@admin_required
def index():
    users = User.query.order_by(User.created_at.asc()).all()
    shared_drives = SharedDrive.query.filter_by(is_active=True).order_by(SharedDrive.created_at.desc()).all()
    user_count = User.query.count()
    file_count = File.query.filter(File.deleted_at.is_(None), File.upload_status == "complete").count()
    folder_count = Folder.query.filter(Folder.deleted_at.is_(None)).count()
    shared_drive_count = SharedDrive.query.filter_by(is_active=True).count()
    total_storage = (
        db.session.query(func.coalesce(func.sum(File.total_size), 0))
        .filter(File.deleted_at.is_(None), File.upload_status == "complete")
        .scalar()
    )
    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(20).all()
    usage_rows = (
        db.session.query(File.owner_id, func.coalesce(func.sum(File.total_size), 0))
        .filter(File.deleted_at.is_(None), File.upload_status == "complete")
        .group_by(File.owner_id)
        .all()
    )
    user_usage_by_id = {int(owner_id): int(total or 0) for owner_id, total in usage_rows}

    backend_name = configured_storage_backend_name(current_app.config)
    try:
        backend = get_storage_backend(current_app.config, backend_name=backend_name)
        storage_health = backend.health_check()
    except StorageBackendError as exc:
        storage_health = {"ok": False, "error": str(exc), "channels": []}

    return render_template(
        "admin/index.html",
        users=users,
        shared_drives=shared_drives,
        user_usage_by_id=user_usage_by_id,
        stats={
            "user_count": user_count,
            "file_count": file_count,
            "folder_count": folder_count,
            "shared_drive_count": shared_drive_count,
            "total_storage": int(total_storage or 0),
        },
        recent_activity=recent_activity,
        storage_health=storage_health,
        storage_backend_name=backend_name,
        storage_backend_label=storage_backend_label(backend_name),
        storage_config_rows=_storage_config_rows(),
        config_snapshot={
            "allow_public_sharing": current_app.config["ALLOW_PUBLIC_SHARING"],
            "chunk_size": current_app.config["DISCORD_CHUNK_SIZE_BYTES"],
            "webdav_enabled": current_app.config["WEBDAV_ENABLED"],
            "email_verification_required": current_app.config["EMAIL_VERIFICATION_REQUIRED"],
            "smtp_enabled": EmailService.is_configured(current_app.config),
            "admin_count": AuthService.count_admins(),
            "default_user_storage_quota": current_app.config["DEFAULT_USER_STORAGE_QUOTA_BYTES"],
            "default_admin_storage_quota": current_app.config["DEFAULT_ADMIN_STORAGE_QUOTA_BYTES"],
        },
    )


@admin_bp.route("/users/create", methods=["POST"])
@login_required
@admin_required
def create_user():
    try:
        role = AuthService.normalize_role(request.form.get("role"))
        quota_bytes = _parse_quota_bytes(
            request.form.get("storage_quota_gb"),
            allow_blank=True,
        )
        user = AuthService.create_user(
            username=request.form.get("username", ""),
            email=request.form.get("email", ""),
            password=request.form.get("password", ""),
            force_role=role,
            email_verified=request.form.get("email_verified") == "on",
            storage_quota_bytes=quota_bytes,
        )
        flash(f"Created account for {user.username}.", "success")
        return redirect(url_for("admin.user_details", user_id=user.id))
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users/<int:user_id>")
@login_required
@admin_required
def user_details(user_id: int):
    target_user = db.session.get(User, user_id)
    if not target_user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    folder_id = request.args.get("folder_id", type=int)
    query = request.args.get("q", type=str, default="").strip()
    type_filter = request.args.get("type", "all")

    try:
        current_folder = (
            FileService.get_folder_or_404(current_user, folder_id)
            if folder_id
            else FileService.get_accessible_root_folder(current_user, owner=target_user)
        )
        if current_folder.owner_id != target_user.id:
            raise LookupError("Folder not found in that user's drive.")

        folders, files = FileService.list_folder_contents(
            user=current_user,
            folder=current_folder,
            query=query,
            scope="current",
            type_filter=type_filter,
        )
    except LookupError:
        flash("That folder could not be found in the selected user's drive.", "error")
        return redirect(url_for("admin.user_details", user_id=user_id))
    except AccessError:
        flash("You do not have access to that folder.", "error")
        return redirect(url_for("admin.index"))

    folders = [folder for folder in folders if folder.owner_id == target_user.id]
    files = [file_record for file_record in files if file_record.owner_id == target_user.id]
    recent_activity = (
        ActivityLog.query.filter_by(user_id=target_user.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(15)
        .all()
    )
    recent_sessions = (
        UserSession.query.filter_by(user_id=target_user.id)
        .order_by(UserSession.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "admin/user_details.html",
        target_user=target_user,
        current_folder=current_folder,
        folders=folders,
        files=files,
        breadcrumbs=FileService.build_breadcrumbs(current_folder),
        usage=FileService.usage_summary(target_user),
        recent_activity=recent_activity,
        recent_sessions=recent_sessions,
        folder_options=FileService.folder_options(current_user, owner=target_user),
        query=query,
        type_filter=type_filter,
    )


@admin_bp.route("/users/<int:user_id>/profile", methods=["POST"])
@login_required
@admin_required
def update_user_profile(user_id: int):
    target_user = db.session.get(User, user_id)
    if not target_user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    try:
        quota_bytes = _parse_quota_bytes(request.form.get("storage_quota_gb"))
        password = (request.form.get("password") or "").strip() or None
        AuthService.update_user_profile(
            target_user,
            username=request.form.get("username"),
            email=request.form.get("email"),
            password=password,
            role=request.form.get("role"),
            email_verified=request.form.get("email_verified") == "on",
            storage_quota_bytes=quota_bytes,
            actor_id=current_user.id,
        )
        flash("User profile updated.", "success")
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.user_details", user_id=user_id))


@admin_bp.route("/users/<int:user_id>/folders/create", methods=["POST"])
@login_required
@admin_required
def create_user_folder(user_id: int):
    target_user = db.session.get(User, user_id)
    if not target_user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    parent_id = request.form.get("parent_id", type=int)
    try:
        parent_folder = (
            FileService.get_folder_or_404(current_user, parent_id)
            if parent_id
            else FileService.get_accessible_root_folder(current_user, owner=target_user)
        )
        if parent_folder.owner_id != target_user.id:
            raise LookupError("Folder not found in that user's drive.")
        FileService.create_folder(current_user, parent_folder, request.form.get("name", ""))
        flash("Folder created.", "success")
        return _redirect_to_user_workspace(user_id, folder_id=parent_folder.id)
    except (LookupError, AccessError):
        flash("Unable to create a folder there.", "error")
    except (ValidationError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.user_details", user_id=user_id))


@admin_bp.route("/users/<int:user_id>/files/<int:file_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user_file(user_id: int, file_id: int):
    target_user = db.session.get(User, user_id)
    if not target_user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    redirect_folder_id = request.form.get("folder_id", type=int)
    hard_delete = request.form.get("hard_delete") == "true"
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        if file_record.owner_id != target_user.id:
            raise LookupError("File not found in that user's drive.")
        FileService.delete_file(current_user, file_record, hard_delete=hard_delete)
        flash("File deleted.", "success")
    except (LookupError, AccessError):
        flash("File not found.", "error")
    return _redirect_to_user_workspace(user_id, folder_id=redirect_folder_id)


@admin_bp.route("/users/<int:user_id>/folders/<int:folder_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user_folder(user_id: int, folder_id: int):
    target_user = db.session.get(User, user_id)
    if not target_user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    parent_id = request.form.get("parent_id", type=int)
    hard_delete = request.form.get("hard_delete") == "true"
    try:
        folder = FileService.get_folder_or_404(current_user, folder_id)
        if folder.owner_id != target_user.id:
            raise LookupError("Folder not found in that user's drive.")
        fallback_parent_id = parent_id or folder.parent_id
        FileService.delete_folder(current_user, folder, hard_delete=hard_delete)
        flash("Folder deleted.", "success")
        return _redirect_to_user_workspace(user_id, folder_id=fallback_parent_id)
    except (LookupError, AccessError):
        flash("Folder not found.", "error")
    except (ValidationError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.user_details", user_id=user_id))


@admin_bp.route("/users/<int:user_id>/role", methods=["POST"])
@login_required
@admin_required
def update_user_role(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    try:
        AuthService.update_role(
            user,
            request.form.get("role", ""),
            actor_id=current_user.id,
        )
        flash("User role updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users/<int:user_id>/quota", methods=["POST"])
@login_required
@admin_required
def update_user_quota(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    try:
        quota_bytes = _parse_quota_bytes(request.form.get("storage_quota_gb"))
        AuthService.update_storage_quota(
            user,
            quota_bytes or 0,
            actor_id=current_user.id,
        )
        flash("User storage quota updated.", "success")
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.index"))
