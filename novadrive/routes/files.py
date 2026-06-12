from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from novadrive.forms import ShareLinkForm
from novadrive.models import ExternalUpload, ShareLink
from novadrive.services import external_upload
from novadrive.services.external_upload import ExternalUploadError
from novadrive.services.file_delivery import FileDeliveryService
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.share_service import ShareService
from novadrive.services.shared_drive_service import SharedDriveService
from novadrive.utils.validators import ValidationError

files_bp = Blueprint("files", __name__, url_prefix="/files")


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _workspace_redirect(folder_id: int | None = None, *, shared_drive_id: int | None = None):
    if shared_drive_id:
        return redirect(
            url_for("shared_drives.workspace", drive_id=shared_drive_id, folder_id=folder_id)
            if folder_id
            else url_for("shared_drives.workspace", drive_id=shared_drive_id)
        )
    return redirect(
        url_for("dashboard.index", folder_id=folder_id) if folder_id else url_for("dashboard.index")
    )


@files_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    folder_id = request.form.get("folder_id", type=int)
    uploads = request.files.getlist("files")
    shared_drive_id = None
    try:
        folder = (
            FileService.get_folder_or_404(current_user, folder_id)
            if folder_id
            else FileService.get_accessible_root_folder(current_user)
        )
        shared_drive_id = folder.shared_drive_id
        records = FileService.upload_files(current_user, folder, uploads, current_app.config)
        if not records:
            raise ValidationError("Choose at least one file to upload.")

        response_payload = {
            "success": True,
            "uploaded": [
                {
                    "id": file_record.id,
                    "filename": file_record.filename,
                    "size": file_record.total_size,
                    "chunks": file_record.total_chunks,
                }
                for file_record in records
            ],
        }
        if _wants_json():
            return jsonify(response_payload)
        flash(f"Uploaded {len(records)} file(s) to NovaDrive.", "success")
    except (LookupError, AccessError):
        if _wants_json():
            return jsonify({"success": False, "error": "Folder not found."}), 404
        flash("The target folder could not be found.", "error")
    except (ValidationError, ValueError) as exc:
        if _wants_json():
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
    except Exception:
        current_app.logger.exception("Upload failed.")
        if _wants_json():
            return jsonify({"success": False, "error": "Upload failed unexpectedly."}), 500
        flash("Upload failed unexpectedly.", "error")

    return _workspace_redirect(folder_id, shared_drive_id=shared_drive_id)


@files_bp.route("/<int:file_id>")
@login_required
def details(file_id: int):
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
    except LookupError:
        abort(404)
    except AccessError:
        abort(403)

    share_form = ShareLinkForm()
    share_links = (
        ShareLink.query.filter_by(file_id=file_record.id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )
    preview_kind = FileDeliveryService.preview_kind(file_record)
    text_preview = FileDeliveryService.get_text_preview(file_record, current_app.config)
    current_shared_drive = file_record.shared_drive
    can_write_file = (
        SharedDriveService.can_write(current_shared_drive, current_user)
        if current_shared_drive is not None
        else (current_user.is_admin or file_record.owner_id == current_user.id)
    )
    return render_template(
        "dashboard/file_details.html",
        file=file_record,
        share_form=share_form,
        share_links=share_links,
        breadcrumbs=FileService.build_breadcrumbs(file_record.folder),
        folder_options=FileService.folder_options(
            current_user,
            owner=None if current_shared_drive else file_record.owner,
            shared_drive=current_shared_drive,
        ),
        preview_kind=preview_kind,
        text_preview=text_preview,
        current_shared_drive=current_shared_drive,
        can_write_file=can_write_file,
        external_upload_enabled=current_app.config["EXTERNAL_UPLOAD_ENABLED"],
        external_upload_providers=external_upload.providers_for_file(
            current_app.config, file_record.total_size
        ),
        external_upload_all_providers=external_upload.available_providers(current_app.config),
        admin_target_user=file_record.owner
        if current_user.is_admin and current_shared_drive is None and file_record.owner_id != current_user.id
        else None,
    )


@files_bp.route("/<int:file_id>/download")
@login_required
def download(file_id: int):
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        return FileDeliveryService.build_response(
            file_record,
            current_app.config,
            as_attachment=True,
            download_name=file_record.filename,
        )
    except LookupError:
        abort(404)
    except AccessError:
        abort(403)
    except Exception as exc:
        flash(str(exc), "error")
        return _workspace_redirect(
            request.args.get("folder_id", type=int),
            shared_drive_id=request.args.get("shared_drive_id", type=int),
        )


@files_bp.route("/<int:file_id>/raw")
@login_required
def raw(file_id: int):
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        return FileDeliveryService.build_response(
            file_record,
            current_app.config,
            as_attachment=False,
            download_name=file_record.filename,
        )
    except LookupError:
        abort(404)
    except AccessError:
        abort(403)


@files_bp.route("/<int:file_id>/rename", methods=["POST"])
@login_required
def rename(file_id: int):
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        FileService.rename_file(current_user, file_record, request.form.get("filename", ""))
        flash("File renamed.", "success")
    except (LookupError, AccessError):
        flash("File not found.", "error")
    except (ValidationError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("files.details", file_id=file_id))


@files_bp.route("/<int:file_id>/move", methods=["POST"])
@login_required
def move(file_id: int):
    admin_user_id = request.form.get("admin_user_id", type=int)
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        destination_folder = FileService.get_folder_or_404(
            current_user,
            request.form.get("destination_folder_id", type=int),
        )
        FileService.move_file(current_user, file_record, destination_folder)
        flash("File moved.", "success")
        if admin_user_id:
            return redirect(
                url_for(
                    "admin.user_details",
                    user_id=admin_user_id,
                    folder_id=destination_folder.id,
                )
            )
        return _workspace_redirect(destination_folder.id, shared_drive_id=destination_folder.shared_drive_id)
    except (LookupError, AccessError):
        flash("Unable to move that file.", "error")
    except (ValidationError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("files.details", file_id=file_id))


@files_bp.route("/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id: int):
    redirect_folder_id = request.form.get("folder_id", type=int)
    admin_user_id = request.form.get("admin_user_id", type=int)
    # Delete frees the underlying storage object by default; pass
    # hard_delete=false explicitly for a soft (index-only) delete.
    hard_delete = request.form.get("hard_delete", "true").lower() != "false"
    shared_drive_id = request.form.get("shared_drive_id", type=int)
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        shared_drive_id = shared_drive_id or file_record.shared_drive_id
        FileService.delete_file(current_user, file_record, hard_delete=hard_delete)
        flash("File deleted.", "success")
    except (LookupError, AccessError):
        flash("File not found.", "error")
    if admin_user_id:
        return redirect(
            url_for(
                "admin.user_details",
                user_id=admin_user_id,
                folder_id=redirect_folder_id,
            )
        )
    return _workspace_redirect(redirect_folder_id, shared_drive_id=shared_drive_id)


def _serialize_external(job: ExternalUpload) -> dict:
    return {
        "id": job.id,
        "provider": job.provider,
        "status": job.status,
        "progress_bytes": job.progress_bytes,
        "total_bytes": job.total_bytes,
        "percent": job.percent,
        "result_url": job.result_url,
        "error": job.error,
        "is_active": job.is_active,
    }


@files_bp.route("/<int:file_id>/external-upload", methods=["POST"])
@login_required
def external_upload_create(file_id: int):
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        job = external_upload.enqueue(
            current_user, file_record, request.form.get("provider", ""), current_app.config
        )
        if _wants_json():
            return jsonify({"success": True, "job": _serialize_external(job)})
        flash("Upload to the third-party host has been queued.", "success")
    except (LookupError, AccessError):
        if _wants_json():
            return jsonify({"success": False, "error": "File not found."}), 404
        flash("File not found.", "error")
    except (ExternalUploadError, ValidationError, ValueError) as exc:
        if _wants_json():
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
    return redirect(url_for("files.details", file_id=file_id))


@files_bp.route("/<int:file_id>/external-uploads", methods=["GET"])
@login_required
def external_upload_list(file_id: int):
    try:
        FileService.get_file_or_404(current_user, file_id)
    except (LookupError, AccessError):
        return jsonify({"uploads": []}), 404
    jobs = external_upload.list_for_file(current_user, file_id)
    return jsonify({"uploads": [_serialize_external(job) for job in jobs]})


@files_bp.route("/external-uploads/<int:job_id>/cancel", methods=["POST"])
@login_required
def external_upload_cancel(job_id: int):
    job = ExternalUpload.query.filter_by(id=job_id, owner_id=current_user.id).first()
    if job is not None:
        external_upload.cancel(job)
    if _wants_json():
        return jsonify({"success": True})
    return redirect(url_for("files.details", file_id=job.file_id) if job else url_for("dashboard.index"))


@files_bp.route("/<int:file_id>/share", methods=["POST"])
@login_required
def create_share_link(file_id: int):
    form = ShareLinkForm()
    try:
        file_record = FileService.get_file_or_404(current_user, file_id)
        if file_record.shared_drive_id and not SharedDriveService.can_write(file_record.shared_drive, current_user):
            raise AccessError("You do not have permission to share files from this shared drive.")
        if not form.validate_on_submit():
            flash("The share form was invalid.", "error")
            return redirect(url_for("files.details", file_id=file_id))

        share_link = ShareService.create_link(
            file_record=file_record,
            expires_at=form.expires_at.data,
            user_id=current_user.id,
        )
        flash("Share link created.", "success")
        return redirect(url_for("files.details", file_id=file_id, created_share=share_link.token))
    except (LookupError, AccessError):
        flash("File not found.", "error")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("files.details", file_id=file_id))
