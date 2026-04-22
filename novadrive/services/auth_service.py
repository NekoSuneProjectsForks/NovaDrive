from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from flask import current_app, has_app_context
from sqlalchemy import func, or_

from novadrive.extensions import db
from novadrive.models import ActivityLog, Folder, User, UserSession, utcnow
from novadrive.services.activity_service import ActivityService


class AuthService:
    API_KEY_PREFIX = "ndv_"
    DEFAULT_ADMIN_MARKER_ACTION = "system.default_admin.provisioned"
    DEFAULT_ADMIN_USERNAME = "admin"
    DEFAULT_ADMIN_EMAIL = "admin@example.com"
    DEFAULT_ADMIN_PASSWORD = "changeme123"

    @staticmethod
    def normalize_role(role: str | None) -> str:
        value = (role or "user").strip().lower()
        if value not in {"admin", "user"}:
            raise ValueError("Invalid role.")
        return value

    @staticmethod
    def create_user(
        username: str,
        email: str,
        password: str,
        force_role: str | None = None,
        email_verified: bool = False,
        storage_quota_bytes: int | None = None,
    ) -> User:
        normalized_username = username.strip()
        normalized_email = email.strip().lower()

        if AuthService.find_by_username(normalized_username):
            raise ValueError("That username is already taken.")
        if AuthService.find_by_email(normalized_email):
            raise ValueError("That email is already in use.")

        role = (
            AuthService.normalize_role(force_role)
            if force_role is not None
            else AuthService.default_role_for_new_user()
        )
        user = User(
            username=normalized_username,
            email=normalized_email,
            role=role,
            storage_quota_bytes=storage_quota_bytes
            if storage_quota_bytes is not None
            else AuthService.default_storage_quota_bytes(role),
            email_verified_at=utcnow() if email_verified else None,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        root_folder = Folder(
            name="My Drive",
            owner_id=user.id,
            shared_drive_id=None,
            is_root=True,
        )
        db.session.add(root_folder)
        db.session.commit()

        ActivityService.log(
            action="user.created",
            target_type="user",
            target_id=user.id,
            user_id=user.id,
            metadata={"role": role},
        )
        return user

    @staticmethod
    def authenticate(login: str, password: str, *, record_login: bool = True) -> User | None:
        identity = login.strip().lower()
        user = User.query.filter(
            or_(
                func.lower(User.username) == identity,
                func.lower(User.email) == identity,
            )
        ).first()
        if user and user.check_password(password):
            if record_login:
                user.last_login_at = utcnow()
                db.session.commit()
            return user
        return None

    @staticmethod
    def can_use_password_login(user: User, config) -> bool:
        if not config["EMAIL_VERIFICATION_REQUIRED"]:
            return True
        return user.is_email_verified

    @staticmethod
    def ensure_user_session(
        user: User,
        session_token: str,
        user_agent: str | None,
        ip_address: str | None,
        lifetime_hours: int,
    ) -> UserSession:
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        session = UserSession(
            user_id=user.id,
            session_token_hash=token_hash,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=ip_address,
            expires_at=utcnow() + timedelta(hours=lifetime_hours),
        )
        db.session.add(session)
        db.session.commit()
        return session

    @staticmethod
    def deactivate_user_session(session_token: str | None) -> None:
        if not session_token:
            return
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        session = UserSession.query.filter_by(session_token_hash=token_hash, is_active=True).first()
        if not session:
            return
        session.is_active = False
        db.session.commit()

    @staticmethod
    def get_root_folder(user: User) -> Folder:
        root = Folder.query.filter_by(
            owner_id=user.id,
            shared_drive_id=None,
            is_root=True,
            deleted_at=None,
        ).first()
        if root:
            return root
        root = Folder(name="My Drive", owner_id=user.id, shared_drive_id=None, is_root=True)
        db.session.add(root)
        db.session.commit()
        return root

    @staticmethod
    def find_by_username(username: str) -> User | None:
        return User.query.filter(func.lower(User.username) == username.lower()).first()

    @staticmethod
    def find_by_email(email: str) -> User | None:
        return User.query.filter(func.lower(User.email) == email.lower()).first()

    @staticmethod
    def generate_api_key(user: User) -> str:
        raw_key = f"{AuthService.API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        user.api_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        user.api_key_last4 = raw_key[-4:]
        user.api_key_created_at = utcnow()
        db.session.commit()

        ActivityService.log(
            action="user.api_key.generated",
            target_type="user",
            target_id=user.id,
            user_id=user.id,
        )
        return raw_key

    @staticmethod
    def ensure_api_key(user: User) -> str | None:
        if user.has_api_key:
            return None
        return AuthService.generate_api_key(user)

    @staticmethod
    def revoke_api_key(user: User) -> None:
        if not user.has_api_key:
            return

        user.api_key_hash = None
        user.api_key_last4 = None
        user.api_key_created_at = None
        db.session.commit()

        ActivityService.log(
            action="user.api_key.revoked",
            target_type="user",
            target_id=user.id,
            user_id=user.id,
        )

    @staticmethod
    def authenticate_api_key(api_key: str | None) -> User | None:
        candidate = (api_key or "").strip()
        if not candidate:
            return None
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        user = User.query.filter_by(api_key_hash=digest).first()
        if user and AuthService.must_change_default_admin_credentials(user):
            return None
        return user

    @staticmethod
    def mark_email_verified(user: User) -> User:
        if user.is_email_verified:
            return user

        user.email_verified_at = utcnow()
        db.session.commit()

        ActivityService.log(
            action="user.email_verified",
            target_type="user",
            target_id=user.id,
            user_id=user.id,
        )
        return user

    @staticmethod
    def default_role_for_new_user() -> str:
        if User.query.count() == 0 and not AuthService.has_default_admin_bootstrap_marker():
            return "admin"
        return "user"

    @staticmethod
    def has_default_admin_bootstrap_marker() -> bool:
        return (
            ActivityLog.query.filter_by(action=AuthService.DEFAULT_ADMIN_MARKER_ACTION).first()
            is not None
        )

    @staticmethod
    def ensure_default_admin(config=None) -> User | None:
        if User.query.count() > 0 or AuthService.has_default_admin_bootstrap_marker():
            return None

        user = AuthService.create_user(
            username=AuthService.DEFAULT_ADMIN_USERNAME,
            email=AuthService.DEFAULT_ADMIN_EMAIL,
            password=AuthService.DEFAULT_ADMIN_PASSWORD,
            force_role="admin",
            email_verified=True,
            storage_quota_bytes=AuthService.default_storage_quota_bytes("admin", config=config),
        )
        ActivityService.log(
            action=AuthService.DEFAULT_ADMIN_MARKER_ACTION,
            target_type="user",
            target_id=user.id,
            user_id=user.id,
            metadata={
                "username": user.username,
                "email": user.email,
            },
        )
        return user

    @staticmethod
    def must_change_default_admin_credentials(user: User) -> bool:
        if not user.is_admin:
            return False
        if user.username.strip().lower() == AuthService.DEFAULT_ADMIN_USERNAME.lower():
            return True
        if user.email.lower() == AuthService.DEFAULT_ADMIN_EMAIL.lower():
            return True
        return user.check_password(AuthService.DEFAULT_ADMIN_PASSWORD)

    @staticmethod
    def validate_default_admin_replacement(
        *,
        username: str,
        email: str,
        password: str,
    ) -> None:
        normalized_username = username.strip().lower()
        normalized_email = email.strip().lower()
        if normalized_username == AuthService.DEFAULT_ADMIN_USERNAME.lower():
            raise ValueError("Change the default admin username before continuing.")
        if normalized_email == AuthService.DEFAULT_ADMIN_EMAIL.lower():
            raise ValueError("Change the default admin email before continuing.")
        if password == AuthService.DEFAULT_ADMIN_PASSWORD:
            raise ValueError("Change the default admin password before continuing.")

    @staticmethod
    def replace_default_admin_credentials(
        user: User,
        *,
        username: str,
        email: str,
        password: str,
        actor_id: int | None = None,
    ) -> User:
        AuthService.validate_default_admin_replacement(
            username=username,
            email=email,
            password=password,
        )
        updated_user = AuthService.update_user_profile(
            user,
            username=username,
            email=email,
            password=password,
            actor_id=actor_id,
        )
        ActivityService.log(
            action="user.default_admin_credentials.replaced",
            target_type="user",
            target_id=updated_user.id,
            user_id=actor_id or updated_user.id,
        )
        return updated_user

    @staticmethod
    def note_verification_email_sent(user: User) -> None:
        user.email_verification_sent_at = utcnow()
        db.session.commit()

        ActivityService.log(
            action="user.verification_email.sent",
            target_type="user",
            target_id=user.id,
            user_id=user.id,
        )

    @staticmethod
    def update_role(user: User, role: str, actor_id: int | None = None) -> User:
        role_value = AuthService.normalize_role(role)

        if user.role == "admin" and role_value != "admin" and AuthService.count_admins() <= 1:
            raise ValueError("At least one admin account must remain.")

        user.role = role_value
        db.session.commit()

        ActivityService.log(
            action="user.role.updated",
            target_type="user",
            target_id=user.id,
            user_id=actor_id,
            metadata={"role": role_value},
        )
        return user

    @staticmethod
    def update_user_profile(
        user: User,
        *,
        username: str | None = None,
        email: str | None = None,
        password: str | None = None,
        role: str | None = None,
        email_verified: bool | None = None,
        storage_quota_bytes: int | None = None,
        actor_id: int | None = None,
    ) -> User:
        updates: dict[str, object] = {}

        if username is not None:
            normalized_username = username.strip()
            if not normalized_username:
                raise ValueError("Username is required.")
            existing_user = AuthService.find_by_username(normalized_username)
            if existing_user and existing_user.id != user.id:
                raise ValueError("That username is already taken.")
            if user.username != normalized_username:
                user.username = normalized_username
                updates["username"] = normalized_username

        if email is not None:
            normalized_email = email.strip().lower()
            if not normalized_email:
                raise ValueError("Email is required.")
            existing_user = AuthService.find_by_email(normalized_email)
            if existing_user and existing_user.id != user.id:
                raise ValueError("That email is already in use.")
            if user.email != normalized_email:
                user.email = normalized_email
                updates["email"] = normalized_email

        if role is not None:
            role_value = AuthService.normalize_role(role)
            if user.role == "admin" and role_value != "admin" and AuthService.count_admins() <= 1:
                raise ValueError("At least one admin account must remain.")
            if user.role != role_value:
                user.role = role_value
                updates["role"] = role_value

        if storage_quota_bytes is not None:
            if storage_quota_bytes < 0:
                raise ValueError("Storage quota must be zero or greater.")
            normalized_quota = int(storage_quota_bytes)
            if int(user.storage_quota_bytes or 0) != normalized_quota:
                user.storage_quota_bytes = normalized_quota
                updates["storage_quota_bytes"] = normalized_quota

        if password is not None:
            normalized_password = password.strip()
            if not normalized_password:
                raise ValueError("Password cannot be empty.")
            user.set_password(normalized_password)
            updates["password_reset"] = True

        if email_verified is not None:
            if email_verified and not user.is_email_verified:
                user.email_verified_at = utcnow()
                updates["email_verified"] = True
            if not email_verified and user.is_email_verified:
                user.email_verified_at = None
                user.email_verification_sent_at = None
                updates["email_verified"] = False

        db.session.commit()

        if updates:
            ActivityService.log(
                action="user.profile.updated",
                target_type="user",
                target_id=user.id,
                user_id=actor_id,
                metadata=updates,
            )
        return user

    @staticmethod
    def count_admins() -> int:
        return User.query.filter_by(role="admin").count()

    @staticmethod
    def default_storage_quota_bytes(role: str, config=None) -> int:
        resolved_config = config
        if resolved_config is None and has_app_context():
            resolved_config = current_app.config
        if resolved_config is None:
            return 0 if role == "admin" else 10 * 1024 * 1024 * 1024
        if role == "admin":
            return int(resolved_config["DEFAULT_ADMIN_STORAGE_QUOTA_BYTES"])
        return int(resolved_config["DEFAULT_USER_STORAGE_QUOTA_BYTES"])

    @staticmethod
    def storage_quota_bytes_for_user(user: User, config=None) -> int:
        configured_quota = user.storage_quota_bytes
        if configured_quota is None:
            return AuthService.default_storage_quota_bytes(user.role, config=config)
        return int(configured_quota)

    @staticmethod
    def update_storage_quota(user: User, storage_quota_bytes: int, actor_id: int | None = None) -> User:
        if storage_quota_bytes < 0:
            raise ValueError("Storage quota must be zero or greater.")

        user.storage_quota_bytes = int(storage_quota_bytes)
        db.session.commit()

        ActivityService.log(
            action="user.storage_quota.updated",
            target_type="user",
            target_id=user.id,
            user_id=actor_id,
            metadata={"storage_quota_bytes": int(storage_quota_bytes)},
        )
        return user

