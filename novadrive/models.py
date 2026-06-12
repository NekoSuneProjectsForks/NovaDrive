from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from novadrive.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class User(UserMixin, TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    password_changed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    role = db.Column(db.String(16), nullable=False, default="user", index=True)
    storage_quota_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    email_verification_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    password_reset_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    two_factor_secret = db.Column(db.String(64), nullable=True)
    two_factor_pending_secret = db.Column(db.String(64), nullable=True)
    two_factor_enabled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    webdav_password_hash = db.Column(db.String(64), nullable=True)
    webdav_password_last4 = db.Column(db.String(4), nullable=True)
    webdav_password_created_at = db.Column(db.DateTime(timezone=True), nullable=True)
    api_key_hash = db.Column(db.String(64), nullable=True, index=True)
    api_key_last4 = db.Column(db.String(4), nullable=True)
    api_key_created_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Per-user permission to start torrent/magnet remote downloads. NULL means
    # "inherit the REMOTE_DOWNLOAD_ALLOW_TORRENTS instance default". Admins are
    # always allowed regardless of this value.
    can_download_torrents = db.Column(db.Boolean, nullable=True)

    folders = db.relationship("Folder", back_populates="owner", lazy="select")
    files = db.relationship("File", back_populates="owner", lazy="select")
    obs_overlay_settings = db.relationship(
        "ObsOverlaySettings",
        back_populates="user",
        lazy="select",
        uselist=False,
        cascade="all, delete-orphan",
    )
    activity_logs = db.relationship("ActivityLog", back_populates="user", lazy="select")
    sessions = db.relationship("UserSession", back_populates="user", lazy="select")
    shared_drives_owned = db.relationship(
        "SharedDrive",
        foreign_keys="SharedDrive.owner_id",
        back_populates="owner",
        lazy="select",
    )
    shared_drive_memberships = db.relationship(
        "SharedDriveMember",
        foreign_keys="SharedDriveMember.user_id",
        back_populates="user",
        lazy="select",
        cascade="all, delete-orphan",
    )
    shared_drive_requests = db.relationship(
        "SharedDriveJoinRequest",
        foreign_keys="SharedDriveJoinRequest.user_id",
        back_populates="user",
        lazy="select",
        cascade="all, delete-orphan",
    )

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_hash)

    @property
    def has_webdav_password(self) -> bool:
        return bool(self.webdav_password_hash)

    @property
    def requires_password_change(self) -> bool:
        return bool(self.must_change_password)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_two_factor_enabled(self) -> bool:
        return bool(self.two_factor_secret and self.two_factor_enabled_at)

    @property
    def has_pending_two_factor_setup(self) -> bool:
        return bool(self.two_factor_pending_secret)

    @property
    def has_storage_quota(self) -> bool:
        return int(self.storage_quota_bytes or 0) > 0

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)
        self.password_changed_at = utcnow()

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Folder(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    shared_drive_id = db.Column(db.Integer, db.ForeignKey("shared_drive.id"), nullable=True, index=True)
    is_root = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship("User", back_populates="folders")
    shared_drive = db.relationship("SharedDrive", back_populates="folders")
    parent = db.relationship("Folder", remote_side=[id], back_populates="children")
    children = db.relationship("Folder", back_populates="parent", lazy="select")
    files = db.relationship("File", back_populates="folder", lazy="select")


class File(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    shared_drive_id = db.Column(db.Integer, db.ForeignKey("shared_drive.id"), nullable=True, index=True)
    filename = db.Column(db.String(255), nullable=False, index=True)
    original_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(255), nullable=False, default="application/octet-stream")
    total_size = db.Column(db.BigInteger, nullable=False, default=0)
    total_chunks = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False, default="")
    upload_status = db.Column(
        db.String(32),
        nullable=False,
        default="uploading",
        index=True,
    )
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    folder = db.relationship("Folder", back_populates="files")
    owner = db.relationship("User", back_populates="files")
    shared_drive = db.relationship("SharedDrive", back_populates="files")
    chunks = db.relationship(
        "FileChunk",
        back_populates="file",
        lazy="select",
        order_by="FileChunk.chunk_index",
        cascade="all, delete-orphan",
    )
    manifest = db.relationship(
        "FileManifest",
        back_populates="file",
        lazy="select",
        uselist=False,
        cascade="all, delete-orphan",
    )
    share_links = db.relationship("ShareLink", back_populates="file", lazy="select")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None or self.upload_status == "deleted"

    @property
    def extension(self) -> str:
        if "." not in self.filename:
            return ""
        return self.filename.rsplit(".", 1)[-1].lower()


class FileManifest(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False, unique=True)
    storage_backend = db.Column(db.String(32), nullable=False, default="discord")
    manifest_version = db.Column(db.Integer, nullable=False, default=1)
    chunk_size = db.Column(db.Integer, nullable=False)
    upload_session_token = db.Column(db.String(128), nullable=True, unique=True)
    metadata_json = db.Column(db.Text, nullable=True)
    last_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    file = db.relationship("File", back_populates="manifest")


class FileChunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False, index=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    discord_channel_id = db.Column(db.String(32), nullable=False)
    discord_message_id = db.Column(db.String(32), nullable=False)
    discord_attachment_url = db.Column(db.Text, nullable=False)
    discord_attachment_filename = db.Column(db.String(255), nullable=True)
    chunk_size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    file = db.relationship("File", back_populates="chunks")

    __table_args__ = (
        db.UniqueConstraint("file_id", "chunk_index", name="uq_file_chunk_index"),
    )


class ShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False, index=True)
    token = db.Column(db.String(128), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    file = db.relationship("File", back_populates="share_links")

    @property
    def is_expired(self) -> bool:
        expires_at = as_utc(self.expires_at)
        return expires_at is not None and expires_at <= utcnow()


class SharedDrive(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    visibility = db.Column(db.String(24), nullable=False, default="invite_only", index=True)
    storage_quota_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    owner = db.relationship("User", foreign_keys=[owner_id], back_populates="shared_drives_owned")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    folders = db.relationship("Folder", back_populates="shared_drive", lazy="select")
    files = db.relationship("File", back_populates="shared_drive", lazy="select")
    memberships = db.relationship(
        "SharedDriveMember",
        back_populates="shared_drive",
        lazy="select",
        cascade="all, delete-orphan",
    )
    join_requests = db.relationship(
        "SharedDriveJoinRequest",
        back_populates="shared_drive",
        lazy="select",
        cascade="all, delete-orphan",
    )

    @property
    def is_invite_only(self) -> bool:
        return self.visibility == "invite_only"

    @property
    def allows_join_requests(self) -> bool:
        return self.visibility == "request_access"

    @property
    def is_public(self) -> bool:
        return self.visibility == "public"


class SharedDriveMember(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shared_drive_id = db.Column(db.Integer, db.ForeignKey("shared_drive.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role = db.Column(db.String(24), nullable=False, default="viewer", index=True)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    shared_drive = db.relationship("SharedDrive", back_populates="memberships")
    user = db.relationship("User", foreign_keys=[user_id], back_populates="shared_drive_memberships")
    invited_by = db.relationship("User", foreign_keys=[invited_by_id])

    __table_args__ = (
        db.UniqueConstraint("shared_drive_id", "user_id", name="uq_shared_drive_member"),
    )

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def can_manage(self) -> bool:
        return self.role == "owner"

    @property
    def can_write(self) -> bool:
        return self.role in {"owner", "editor"}


class SharedDriveJoinRequest(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shared_drive_id = db.Column(db.Integer, db.ForeignKey("shared_drive.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    shared_drive = db.relationship("SharedDrive", back_populates="join_requests")
    user = db.relationship("User", foreign_keys=[user_id], back_populates="shared_drive_requests")
    resolved_by = db.relationship("User", foreign_keys=[resolved_by_id])


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    target_type = db.Column(db.String(32), nullable=False, index=True)
    target_id = db.Column(db.Integer, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="activity_logs")


class UserSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    session_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User", back_populates="sessions")


class ObsOverlaySettings(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True, index=True)
    media_filter = db.Column(db.String(16), nullable=False, default="image")
    fit_mode = db.Column(db.String(16), nullable=False, default="cover")
    slide_interval_seconds = db.Column(db.Integer, nullable=False, default=30)
    fade_duration_ms = db.Column(db.Integer, nullable=False, default=1000)
    shuffle = db.Column(db.Boolean, nullable=False, default=False)
    selected_file_ids_json = db.Column(db.Text, nullable=False, default="[]")

    user = db.relationship("User", back_populates="obs_overlay_settings")
    folder = db.relationship("Folder")


class RemoteDownload(TimestampMixin, db.Model):
    """A server-side "fetch by URL" job that ingests a remote file."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=False, index=True)
    shared_drive_id = db.Column(db.Integer, db.ForeignKey("shared_drive.id"), nullable=True, index=True)
    source_url = db.Column(db.Text, nullable=False)
    source_type = db.Column(db.String(16), nullable=False, default="http")
    filename = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(16), nullable=False, default=STATUS_QUEUED, index=True)
    progress_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    total_bytes = db.Column(db.BigInteger, nullable=True)
    error = db.Column(db.Text, nullable=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=True, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship("User")
    folder = db.relationship("Folder")
    file = db.relationship("File")

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def percent(self) -> int:
        if not self.total_bytes or self.total_bytes <= 0:
            return 0
        return min(100, int((self.progress_bytes / self.total_bytes) * 100))


class ExternalUpload(TimestampMixin, db.Model):
    """A background job that uploads one of the user's files to a temp host."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False, index=True)
    provider = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default=STATUS_QUEUED, index=True)
    progress_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    total_bytes = db.Column(db.BigInteger, nullable=True)
    result_url = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship("User")
    file = db.relationship("File")

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def percent(self) -> int:
        if not self.total_bytes or self.total_bytes <= 0:
            return 0
        return min(100, int((self.progress_bytes / self.total_bytes) * 100))


class ShortUrl(TimestampMixin, db.Model):
    """A short link served from this domain that redirects to a target URL."""

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code = db.Column(db.String(64), nullable=False, unique=True, index=True)
    # What /u/<code> redirects to. For an external "proxy" provider this is the
    # provider's (possibly ad-monetised) short link; ``final_url`` keeps the real
    # destination for display.
    target_url = db.Column(db.Text, nullable=False)
    final_url = db.Column(db.Text, nullable=True)
    provider = db.Column(db.String(32), nullable=False, default="native")
    click_count = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_click_at = db.Column(db.DateTime(timezone=True), nullable=True)

    owner = db.relationship("User")

    @property
    def destination(self) -> str:
        return self.final_url or self.target_url

