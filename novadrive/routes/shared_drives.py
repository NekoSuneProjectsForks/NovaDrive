from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from novadrive.extensions import db
from novadrive.models import SharedDriveJoinRequest, User
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.shared_drive_service import SharedDriveService
from novadrive.utils.decorators import admin_required
from novadrive.utils.validators import ValidationError

shared_drives_bp = Blueprint("shared_drives", __name__, url_prefix="/shared-drives")


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


def _get_drive_or_abort(drive_id: int):
    shared_drive = SharedDriveService.get_drive(drive_id)
    if not shared_drive:
        abort(404)
    return shared_drive


@shared_drives_bp.get("/")
@login_required
def index():
    member_drives = SharedDriveService.list_member_drives(current_user)
    discoverable_drives = SharedDriveService.list_discoverable_drives(current_user)
    pending_requests_by_drive_id = {
        request_record.shared_drive_id: request_record
        for request_record in current_user.shared_drive_requests
        if request_record.status == "pending"
    }
    return render_template(
        "shared_drives/index.html",
        member_drives=member_drives,
        discoverable_drives=discoverable_drives,
        pending_requests_by_drive_id=pending_requests_by_drive_id,
    )


@shared_drives_bp.post("/create")
@login_required
@admin_required
def create():
    owner_email = (request.form.get("owner_email") or "").strip().lower()
    owner = User.query.filter(db.func.lower(User.email) == owner_email).first()
    if not owner:
        flash("Owner account not found for that email.", "error")
        return redirect(url_for("admin.index"))

    try:
        quota_bytes = _parse_quota_bytes(request.form.get("storage_quota_gb"))
        shared_drive = SharedDriveService.create_shared_drive(
            name=request.form.get("name", ""),
            description=request.form.get("description"),
            owner=owner,
            actor=current_user,
            storage_quota_bytes=quota_bytes or 0,
            visibility=request.form.get("visibility"),
        )
        flash("Shared drive created.", "success")
        return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.index"))


@shared_drives_bp.get("/<int:drive_id>")
@login_required
def workspace(drive_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    can_view = SharedDriveService.can_view(shared_drive, current_user)
    pending_request = SharedDriveService.pending_request_for_user(shared_drive, current_user)
    if not can_view:
        if shared_drive.visibility == "request_access":
            return render_template(
                "shared_drives/request_access.html",
                shared_drive=shared_drive,
                pending_request=pending_request,
            )
        abort(403)

    folder_id = request.args.get("folder_id", type=int)
    query = request.args.get("q", type=str, default="").strip()
    scope = request.args.get("scope", "current")
    type_filter = request.args.get("type", "all")
    view_mode = request.args.get("view", "list")

    try:
        current_folder = (
            FileService.get_folder_or_404(current_user, folder_id)
            if folder_id
            else FileService.get_accessible_root_folder(current_user, shared_drive=shared_drive)
        )
        if current_folder.shared_drive_id != shared_drive.id:
            raise LookupError("Folder not found in that shared drive.")
    except LookupError:
        abort(404)
    except AccessError:
        abort(403)

    folders, files = FileService.list_folder_contents(
        user=current_user,
        folder=current_folder,
        query=query,
        scope=scope,
        type_filter=type_filter,
    )

    can_manage_shared_drive = SharedDriveService.can_manage(shared_drive, current_user)
    can_write_to_workspace = SharedDriveService.can_write(shared_drive, current_user)

    pending_requests = (
        SharedDriveJoinRequest.query.filter_by(
            shared_drive_id=shared_drive.id,
            status="pending",
        )
        .order_by(SharedDriveJoinRequest.created_at.desc())
        .all()
        if can_manage_shared_drive
        else []
    )

    return render_template(
        "shared_drives/workspace.html",
        current_shared_drive=shared_drive,
        current_folder=current_folder,
        folders=folders,
        files=files,
        breadcrumbs=FileService.build_breadcrumbs(current_folder),
        folder_tree=FileService.folder_tree(current_user, shared_drive=shared_drive),
        sidebar_tree=FileService.folder_tree(current_user, shared_drive=shared_drive),
        sidebar_usage=FileService.usage_summary(shared_drive=shared_drive),
        recent_files=FileService.recent_files(current_user, shared_drive=shared_drive),
        usage=FileService.usage_summary(shared_drive=shared_drive),
        folder_options=FileService.folder_options(current_user, shared_drive=shared_drive),
        query=query,
        scope=scope,
        type_filter=type_filter,
        view_mode=view_mode,
        shared_drive_member=SharedDriveService.membership_for_user(shared_drive, current_user),
        can_manage_shared_drive=can_manage_shared_drive,
        can_write_to_workspace=can_write_to_workspace,
        pending_requests=pending_requests,
    )


@shared_drives_bp.post("/<int:drive_id>/invite")
@login_required
def invite_member(drive_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    if not SharedDriveService.can_manage(shared_drive, current_user):
        abort(403)

    try:
        SharedDriveService.add_member_by_email(
            shared_drive,
            email=request.form.get("email"),
            role=request.form.get("role"),
            actor=current_user,
        )
        flash("Shared drive member added.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))


@shared_drives_bp.post("/<int:drive_id>/settings")
@login_required
def update_settings(drive_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    if not SharedDriveService.can_manage(shared_drive, current_user):
        abort(403)

    try:
        quota_bytes = (
            _parse_quota_bytes(request.form.get("storage_quota_gb"), allow_blank=True)
            if current_user.is_admin
            else None
        )
        SharedDriveService.update_shared_drive(
            shared_drive,
            actor=current_user,
            name=request.form.get("name"),
            description=request.form.get("description"),
            visibility=request.form.get("visibility"),
            storage_quota_bytes=quota_bytes,
        )
        flash("Shared drive settings updated.", "success")
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))


@shared_drives_bp.post("/<int:drive_id>/request-access")
@login_required
def request_access(drive_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    try:
        SharedDriveService.create_join_request(shared_drive, user=current_user)
        flash("Access request sent.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.index"))


@shared_drives_bp.post("/<int:drive_id>/requests/<int:request_id>/approve")
@login_required
def approve_request(drive_id: int, request_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    if not SharedDriveService.can_manage(shared_drive, current_user):
        abort(403)

    join_request = db.session.get(SharedDriveJoinRequest, request_id)
    if not join_request or join_request.shared_drive_id != shared_drive.id:
        abort(404)

    try:
        SharedDriveService.resolve_join_request(join_request, actor=current_user, approve=True)
        flash("Join request approved.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))


@shared_drives_bp.post("/<int:drive_id>/requests/<int:request_id>/deny")
@login_required
def deny_request(drive_id: int, request_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    if not SharedDriveService.can_manage(shared_drive, current_user):
        abort(403)

    join_request = db.session.get(SharedDriveJoinRequest, request_id)
    if not join_request or join_request.shared_drive_id != shared_drive.id:
        abort(404)

    try:
        SharedDriveService.resolve_join_request(join_request, actor=current_user, approve=False)
        flash("Join request denied.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))


@shared_drives_bp.post("/<int:drive_id>/members/<int:member_id>/remove")
@login_required
def remove_member(drive_id: int, member_id: int):
    shared_drive = _get_drive_or_abort(drive_id)
    if not SharedDriveService.can_manage(shared_drive, current_user):
        abort(403)

    membership = SharedDriveService.membership_for_user(shared_drive, db.session.get(User, member_id))
    if not membership:
        abort(404)

    try:
        SharedDriveService.remove_member(shared_drive, membership, actor=current_user)
        flash("Shared drive member removed.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("shared_drives.workspace", drive_id=shared_drive.id))
