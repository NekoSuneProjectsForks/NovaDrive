from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask import current_app
from flask_login import current_user, login_required

from novadrive.services import shortener_providers
from novadrive.services.short_url_service import ShortUrlService
from novadrive.utils.urls import external_url
from novadrive.utils.validators import ValidationError

# Public redirect endpoint (kept separate so the short path stays at /u/<code>).
shorturl_bp = Blueprint("shorturl", __name__)
# Authenticated management UI.
links_bp = Blueprint("links", __name__, url_prefix="/links")


@shorturl_bp.route("/u/<code>", endpoint="redirect")
def redirect_code(code: str):
    short_url = ShortUrlService.resolve(code)
    if short_url is None:
        abort(404)
    ShortUrlService.register_click(short_url)
    return redirect(short_url.target_url, code=302)


@links_bp.route("", methods=["GET"])
@links_bp.route("/", methods=["GET"])
@login_required
def index():
    short_urls = ShortUrlService.list_for_user(current_user)
    return render_template(
        "links/index.html",
        short_urls=short_urls,
        short_link=ShortUrlService.short_link,
        provider_groups=shortener_providers.grouped_providers(current_app.config),
        sharex_config_url=external_url("api.sharex_shortener_config"),
    )


@links_bp.route("", methods=["POST"])
@links_bp.route("/", methods=["POST"])
@login_required
def create():
    try:
        short_url = ShortUrlService.create(
            current_user,
            request.form.get("target_url", ""),
            request.form.get("custom_code") or None,
            request.form.get("provider") or None,
        )
        flash(f"Short link created: {ShortUrlService.short_link(short_url)}", "success")
    except (ValidationError, ValueError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("links.index"))


@links_bp.route("/<int:short_url_id>/delete", methods=["POST"])
@login_required
def delete(short_url_id: int):
    short_url = ShortUrlService.get_owned(current_user, short_url_id)
    if short_url is None:
        flash("Short link not found.", "error")
    else:
        ShortUrlService.delete(short_url)
        flash("Short link deleted.", "success")
    return redirect(url_for("links.index"))
