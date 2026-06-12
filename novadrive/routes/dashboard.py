from __future__ import annotations

from flask import Blueprint, abort, current_app, render_template, request, session
from flask_login import current_user, login_required

from novadrive.forms import TwoFactorChallengeForm, TwoFactorDisableForm
from novadrive.models import ActivityLog
from novadrive.services.auth_service import AuthService
from novadrive.services.file_service import AccessError, FileService

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    folder_id = request.args.get("folder_id", type=int)
    query = request.args.get("q", type=str, default="").strip()
    scope = request.args.get("scope", "current")
    type_filter = request.args.get("type", "all")
    view_mode = request.args.get("view", "list")

    try:
        current_folder = (
            FileService.get_folder_or_404(current_user, folder_id)
            if folder_id
            else FileService.get_accessible_root_folder(current_user)
        )
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

    downloads_folder = FileService.get_or_create_downloads_folder(current_user)

    recent_activity = (
        ActivityLog.query.filter_by(user_id=current_user.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(8)
        .all()
    )
    two_factor_setup_secret = current_user.two_factor_pending_secret
    two_factor_setup_uri = (
        AuthService.build_two_factor_uri(
            current_user,
            two_factor_setup_secret,
            current_app.config["TWO_FACTOR_ISSUER_NAME"],
        )
        if two_factor_setup_secret
        else None
    )

    return render_template(
        "dashboard/index.html",
        current_folder=current_folder,
        folders=folders,
        files=files,
        breadcrumbs=FileService.build_breadcrumbs(current_folder),
        folder_tree=FileService.folder_tree(current_user),
        recent_files=FileService.recent_files(current_user),
        usage=FileService.usage_summary(current_user),
        recent_activity=recent_activity,
        folder_options=FileService.folder_options(current_user),
        default_remote_folder_id=downloads_folder.id,
        query=query,
        scope=scope,
        type_filter=type_filter,
        view_mode=view_mode,
        generated_api_key=session.pop("nova_generated_api_key", None),
        generated_webdav_password=session.pop("nova_generated_webdav_password", None),
        two_factor_setup_form=TwoFactorChallengeForm(prefix="two_factor_setup"),
        two_factor_disable_form=TwoFactorDisableForm(prefix="two_factor_disable"),
        two_factor_setup_secret=two_factor_setup_secret,
        two_factor_setup_uri=two_factor_setup_uri,
    )
