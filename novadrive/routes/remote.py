from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    request,
    url_for,
)
from flask_login import current_user, login_required

from novadrive.models import RemoteDownload
from novadrive.services import remote_download
from novadrive.services.file_service import AccessError, FileService
from novadrive.services.remote_download import RemoteDownloadError
from novadrive.utils.validators import ValidationError

remote_bp = Blueprint("remote", __name__, url_prefix="/remote")


def _wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _serialize(job: RemoteDownload) -> dict:
    return {
        "id": job.id,
        "url": job.source_url,
        "filename": job.filename,
        "status": job.status,
        "progress_bytes": job.progress_bytes,
        "total_bytes": job.total_bytes,
        "percent": job.percent,
        "error": job.error,
        "file_id": job.file_id,
        "is_active": job.is_active,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@remote_bp.route("/downloads", methods=["POST"])
@login_required
def create():
    folder_id = request.form.get("folder_id", type=int)
    new_folder_name = (request.form.get("new_folder") or "").strip()
    url = request.form.get("url", "")
    shared_drive_id = None
    try:
        folder = (
            FileService.get_folder_or_404(current_user, folder_id)
            if folder_id
            else FileService.get_or_create_downloads_folder(current_user)
        )
        if new_folder_name:
            folder = FileService.create_folder(current_user, folder, new_folder_name)
        shared_drive_id = folder.shared_drive_id
        job = remote_download.enqueue(current_user, folder, url, current_app.config)
        if _wants_json():
            return jsonify({"success": True, "job": _serialize(job)})
        flash("Download queued. It will appear in your files when finished.", "success")
    except (LookupError, AccessError):
        if _wants_json():
            return jsonify({"success": False, "error": "Folder not found."}), 404
        flash("The target folder could not be found.", "error")
    except (RemoteDownloadError, ValidationError, ValueError) as exc:
        if _wants_json():
            return jsonify({"success": False, "error": str(exc)}), 400
        flash(str(exc), "error")
    except Exception:
        current_app.logger.exception("Failed to queue remote download.")
        if _wants_json():
            return jsonify({"success": False, "error": "Could not queue that download."}), 500
        flash("Could not queue that download.", "error")

    if shared_drive_id:
        return redirect(url_for("shared_drives.workspace", drive_id=shared_drive_id, folder_id=folder_id))
    return redirect(url_for("dashboard.index", folder_id=folder_id) if folder_id else url_for("dashboard.index"))


@remote_bp.route("/downloads", methods=["GET"])
@login_required
def index():
    jobs = remote_download.list_for_user(current_user)
    return jsonify({"downloads": [_serialize(job) for job in jobs]})


@remote_bp.route("/downloads/<int:job_id>/cancel", methods=["POST"])
@login_required
def cancel(job_id: int):
    job = RemoteDownload.query.filter_by(id=job_id, owner_id=current_user.id).first()
    if job is None:
        if _wants_json():
            return jsonify({"success": False, "error": "Download not found."}), 404
        flash("Download not found.", "error")
        return redirect(url_for("dashboard.index"))

    remote_download.cancel(job)
    if _wants_json():
        return jsonify({"success": True, "job": _serialize(job)})
    flash("Download canceled.", "success")
    return redirect(url_for("dashboard.index"))
