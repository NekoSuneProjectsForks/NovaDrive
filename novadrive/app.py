from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix

from novadrive.config import Config
from novadrive.extensions import csrf, db, login_manager, migrate
from novadrive.models import User
from novadrive.services.auth_service import AuthService
from novadrive.services.storage_factory import (
    configured_storage_backend_name,
    get_storage_backend,
    storage_backend_label,
)
from novadrive.utils.urls import external_url
from novadrive.utils.logging import configure_logging

load_dotenv()


def create_app(config_object: type[Config] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object or Config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    Path(app.config["INSTANCE_DIR"]).mkdir(parents=True, exist_ok=True)
    _ensure_database_storage_path(app)
    app.permanent_session_lifetime = timedelta(
        hours=app.config["PERMANENT_SESSION_LIFETIME_HOURS"]
    )

    configure_logging(app.config["LOG_LEVEL"])
    _init_extensions(app)
    _ensure_runtime_schema(app)
    _ensure_default_admin(app)
    _register_blueprints(app)
    _register_routes(app)
    _register_request_guards(app)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)
    return app


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return db.session.get(User, int(user_id))


def _register_blueprints(app: Flask) -> None:
    from novadrive.routes.api import api_bp
    from novadrive.routes.admin import admin_bp
    from novadrive.routes.auth import auth_bp
    from novadrive.routes.dashboard import dashboard_bp
    from novadrive.routes.files import files_bp
    from novadrive.routes.folders import folders_bp
    from novadrive.routes.share import share_bp
    from novadrive.routes.shared_drives import shared_drives_bp
    from novadrive.routes.webdav import webdav_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(folders_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(share_bp)
    app.register_blueprint(shared_drives_bp)
    app.register_blueprint(webdav_bp)


def _register_routes(app: Flask) -> None:
    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "app": app.config["APP_NAME"],
        }


def _ensure_database_storage_path(app: Flask) -> None:
    database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip()
    sqlite_prefixes = ("sqlite:///", "sqlite+pysqlite:///")
    for prefix in sqlite_prefixes:
        if not database_uri.startswith(prefix):
            continue

        raw_path = database_uri[len(prefix):]
        path_part = raw_path.split("?", 1)[0]
        if not path_part or path_part == ":memory:" or path_part.startswith("file:"):
            return

        database_path = Path(path_part)
        if not database_path.is_absolute():
            database_path = (Path(app.config["BASE_DIR"]) / database_path).resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return


def _ensure_runtime_schema(app: Flask) -> None:
    with app.app_context():
        db.create_all()

        inspector = inspect(db.engine)
        if "user" not in inspector.get_table_names():
            return

        user_columns = {column["name"] for column in inspector.get_columns("user")}
        statements: list[str] = []
        if "api_key_hash" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN api_key_hash VARCHAR(64)')
        if "api_key_last4" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN api_key_last4 VARCHAR(4)')
        if "api_key_created_at" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN api_key_created_at TIMESTAMP')
        if "email_verified_at" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN email_verified_at TIMESTAMP')
        if "email_verification_sent_at" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN email_verification_sent_at TIMESTAMP')
        if "storage_quota_bytes" not in user_columns:
            statements.append('ALTER TABLE "user" ADD COLUMN storage_quota_bytes BIGINT')
        statements.append('CREATE INDEX IF NOT EXISTS ix_user_api_key_hash ON "user" (api_key_hash)')

        folder_columns = {column["name"] for column in inspector.get_columns("folder")}
        if "shared_drive_id" not in folder_columns:
            statements.append('ALTER TABLE "folder" ADD COLUMN shared_drive_id INTEGER')
        statements.append('CREATE INDEX IF NOT EXISTS ix_folder_shared_drive_id ON "folder" (shared_drive_id)')

        file_columns = {column["name"] for column in inspector.get_columns("file")}
        if "shared_drive_id" not in file_columns:
            statements.append('ALTER TABLE "file" ADD COLUMN shared_drive_id INTEGER')
        statements.append('CREATE INDEX IF NOT EXISTS ix_file_shared_drive_id ON "file" (shared_drive_id)')

        with db.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
            connection.execute(
                text(
                    'UPDATE "user" '
                    "SET storage_quota_bytes = CASE "
                    "WHEN role = 'admin' THEN :admin_default "
                    "ELSE :user_default "
                    "END "
                    "WHERE storage_quota_bytes IS NULL"
                ),
                {
                    "admin_default": app.config["DEFAULT_ADMIN_STORAGE_QUOTA_BYTES"],
                    "user_default": app.config["DEFAULT_USER_STORAGE_QUOTA_BYTES"],
                },
            )


def _ensure_default_admin(app: Flask) -> None:
    with app.app_context():
        AuthService.ensure_default_admin(config=app.config)


def _register_template_helpers(app: Flask) -> None:
    @app.template_filter("filesize")
    def filesize_filter(value: int | None) -> str:
        if value is None:
            return "-"
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    @app.template_filter("datetime")
    def datetime_filter(value) -> str:
        if not value:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M")

    @app.context_processor
    def inject_globals():
        sidebar_tree = []
        sidebar_usage = None
        sidebar_shared_drives = []
        if current_user.is_authenticated:
            from novadrive.services.file_service import FileService
            from novadrive.services.shared_drive_service import SharedDriveService

            sidebar_tree = FileService.folder_tree(current_user)
            sidebar_usage = FileService.usage_summary(current_user)
            sidebar_shared_drives = SharedDriveService.visible_drives(current_user)
        return {
            "app_name": app.config["APP_NAME"],
            "allow_public_sharing": app.config["ALLOW_PUBLIC_SHARING"],
            "current_user_obj": current_user if current_user.is_authenticated else None,
            "sidebar_tree": sidebar_tree,
            "sidebar_usage": sidebar_usage,
            "personal_sidebar_usage": sidebar_usage,
            "sidebar_shared_drives": sidebar_shared_drives,
            "configured_storage_backend": configured_storage_backend_name(app.config),
            "configured_storage_backend_label": storage_backend_label(
                configured_storage_backend_name(app.config)
            ),
            "external_url": external_url,
            "requires_default_admin_change": (
                AuthService.must_change_default_admin_credentials(current_user)
                if current_user.is_authenticated
                else False
            ),
        }


def _register_request_guards(app: Flask) -> None:
    @app.before_request
    def enforce_default_admin_rotation():
        if not current_user.is_authenticated:
            return None
        if not AuthService.must_change_default_admin_credentials(current_user):
            return None

        allowed_endpoints = {
            "auth.complete_default_admin_setup",
            "auth.logout",
            "static",
        }
        if request.endpoint in allowed_endpoints or request.endpoint is None:
            return None
        return redirect(url_for("auth.complete_default_admin_setup"))


def _register_error_handlers(app: Flask) -> None:
    def format_bytes(value: int | None) -> str:
        if value is None:
            return "-"
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    def wants_json_error() -> bool:
        if request.blueprint == "api":
            return True
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
        accepted = request.accept_mimetypes
        return accepted.accept_json and not accepted.accept_html

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def request_too_large(error):
        limit_label = format_bytes(app.config["MAX_UPLOAD_SIZE_BYTES"])
        message = f"This file cannot be uploaded because it exceeds the maximum upload size of {limit_label}."
        if app.config.get("CLOUDFLARE_TUNNEL_COMPAT"):
            plan = str(app.config["CLOUDFLARE_TUNNEL_PLAN"]).capitalize()
            message = (
                f"This file cannot be uploaded because this NovaDrive instance is running through "
                f"Cloudflare {plan} tier compatibility mode. Maximum upload size: {limit_label}."
            )
        if wants_json_error():
            return jsonify({"success": False, "error": message}), 413
        flash(message, "error")
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return redirect(url_for("auth.login"))

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        return render_template("errors/500.html"), 500


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """Create all database tables."""
        db.create_all()
        click.echo("Database initialized.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin_command(username: str, email: str, password: str):
        """Create an admin user without using the UI."""
        user = AuthService.create_user(
            username=username,
            email=email,
            password=password,
            force_role="admin",
            email_verified=True,
        )
        click.echo(f"Admin user created: {user.username}")

    @app.cli.command("storage-health")
    def storage_health_command():
        """Check the configured storage backend health."""
        backend = get_storage_backend(app.config)
        result = backend.health_check()
        click.echo(result)
