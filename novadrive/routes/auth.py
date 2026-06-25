from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from novadrive.extensions import db, limiter
from novadrive.models import User, as_utc, utcnow
from novadrive.forms import (
    DefaultAdminSetupForm,
    ForgotPasswordForm,
    LoginForm,
    PasswordResetForm,
    RegistrationForm,
    TwoFactorChallengeForm,
    TwoFactorDisableForm,
)
from novadrive.services.auth_service import AuthService
from novadrive.services.email_service import EmailDeliveryError, EmailService
from novadrive.services.verification_service import VerificationService, VerificationTokenError
from novadrive.utils.session_state import clear_novadrive_session_state
from novadrive.utils.urls import external_url

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
TWO_FACTOR_LOGIN_SESSION_KEYS = ("nova_2fa_user_id", "nova_2fa_remember", "nova_2fa_next")


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if not current_app.config["ALLOW_PUBLIC_REGISTRATION"]:
        flash("Public registration is disabled on this NovaDrive instance.", "error")
        return redirect(url_for("auth.login"))

    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            require_verification = current_app.config["EMAIL_VERIFICATION_REQUIRED"]
            if require_verification:
                VerificationService.ensure_smtp_available(current_app.config)

            user = AuthService.create_user(
                username=form.username.data,
                email=form.email.data,
                password=form.password.data,
                email_verified=not require_verification,
            )
        except (ValueError, EmailDeliveryError) as exc:
            flash(str(exc), "error")
        else:
            if require_verification:
                try:
                    _send_verification_email(user)
                    flash(
                        "Account created. Confirm your email before signing in."
                        if user.role != "admin"
                        else "Admin account created. Confirm your email before signing in.",
                        "success",
                    )
                except EmailDeliveryError as exc:
                    flash(
                        f"Account created, but the verification email could not be sent: {exc}",
                        "error",
                    )
                return redirect(url_for("auth.login", email=user.email))

            flash(
                "Account created successfully. You can sign in now."
                if user.role != "admin"
                else "Admin account created successfully. You can sign in now.",
                "success",
            )
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    pending_verification_email = request.args.get("email", "").strip().lower()
    if form.validate_on_submit():
        _clear_pending_two_factor_login()
        user = AuthService.authenticate(
            form.login.data,
            form.password.data,
            record_login=False,
        )
        if not user:
            flash("Invalid credentials. Please try again.", "error")
        elif not AuthService.can_use_password_login(user, current_app.config):
            pending_verification_email = user.email
            flash("Confirm your email before signing in.", "error")
        elif user.is_two_factor_enabled:
            _store_pending_two_factor_login(
                user=user,
                remember=form.remember.data,
                next_target=request.args.get("next"),
            )
            flash(
                "Enter the 6-digit code from your authenticator app to finish signing in.",
                "info",
            )
            return redirect(url_for("auth.two_factor_login"))
        else:
            return _complete_login(user, remember=form.remember.data, next_target=request.args.get("next"))
    return render_template(
        "auth/login.html",
        form=form,
        pending_verification_email=pending_verification_email,
    )


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        if not EmailService.is_configured(current_app.config):
            flash("Password recovery email is unavailable because SMTP is not configured.", "error")
            return render_template("auth/forgot_password.html", form=form)

        user = AuthService.find_by_email(form.email.data)
        if user:
            resend_interval = current_app.config["PASSWORD_RESET_RESEND_INTERVAL_SECONDS"]
            last_sent_at = as_utc(user.password_reset_sent_at)
            if not last_sent_at or last_sent_at + timedelta(seconds=resend_interval) <= utcnow():
                try:
                    _send_password_reset_email(user)
                except EmailDeliveryError as exc:
                    flash(str(exc), "error")
                    return render_template("auth/forgot_password.html", form=form)

        flash(
            "If that account exists, a password recovery link has been sent to the email address on file.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute; 40 per hour", methods=["POST"])
def reset_password(token: str):
    if current_user.is_authenticated and not AuthService.must_change_password(current_user):
        return redirect(url_for("dashboard.index"))

    form = PasswordResetForm()
    try:
        target_user = _load_password_reset_user(token)
    except VerificationTokenError as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.forgot_password"))

    if form.validate_on_submit():
        try:
            AuthService.complete_password_recovery(
                target_user,
                password=form.password.data,
                actor_id=target_user.id,
            )
            AuthService.deactivate_all_user_sessions(target_user)
            if current_user.is_authenticated:
                logout_user()
                clear_novadrive_session_state(session)
            _clear_pending_two_factor_login()
            flash("Password updated successfully. You can sign in with the new password now.", "success")
            return redirect(url_for("auth.login", email=target_user.email))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template(
        "auth/reset_password.html",
        form=form,
        reset_email=target_user.email,
    )


@auth_bp.route("/login/two-factor", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def two_factor_login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    user = _get_pending_two_factor_user()
    if not user:
        _clear_pending_two_factor_login()
        flash("Your two-factor sign-in session expired. Start again from the login page.", "error")
        return redirect(url_for("auth.login"))

    form = TwoFactorChallengeForm()
    if form.validate_on_submit():
        if not AuthService.can_use_password_login(user, current_app.config):
            _clear_pending_two_factor_login()
            flash("Confirm your email before signing in.", "error")
            return redirect(url_for("auth.login", email=user.email))
        if not user.is_two_factor_enabled:
            _clear_pending_two_factor_login()
            flash("Two-factor authentication is no longer enabled for this account. Sign in again.", "error")
            return redirect(url_for("auth.login"))
        if not AuthService.verify_two_factor_code(user.two_factor_secret, form.code.data):
            flash("Invalid authentication code. Try the latest 6-digit code from your authenticator app.", "error")
        else:
            remember = bool(session.get("nova_2fa_remember"))
            next_target = session.get("nova_2fa_next")
            _clear_pending_two_factor_login()
            return _complete_login(user, remember=remember, next_target=next_target)

    return render_template("auth/two_factor_login.html", form=form, pending_user=user)


@auth_bp.post("/login/two-factor/cancel")
def cancel_two_factor_login():
    _clear_pending_two_factor_login()
    flash("Two-factor sign-in was cancelled.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/complete-default-admin", methods=["GET", "POST"])
@login_required
def complete_default_admin_setup():
    if not AuthService.must_change_default_admin_credentials(current_user):
        return redirect(url_for("dashboard.index"))

    form = DefaultAdminSetupForm(
        username=current_user.username,
        email=current_user.email,
    )
    if form.validate_on_submit():
        try:
            AuthService.replace_default_admin_credentials(
                current_user,
                username=form.username.data,
                email=form.email.data,
                password=form.password.data,
                actor_id=current_user.id,
            )
            flash("Default admin credentials replaced successfully.", "success")
            return redirect(url_for("dashboard.index"))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template(
        "auth/default_admin_setup.html",
        form=form,
        default_admin_username=AuthService.DEFAULT_ADMIN_USERNAME,
        default_admin_email=AuthService.DEFAULT_ADMIN_EMAIL,
    )


@auth_bp.route("/force-password-change", methods=["GET", "POST"])
@login_required
def force_password_change():
    if not AuthService.must_change_password(current_user):
        return redirect(url_for("dashboard.index"))

    form = PasswordResetForm()
    if form.validate_on_submit():
        try:
            AuthService.complete_forced_password_change(
                current_user,
                password=form.password.data,
                actor_id=current_user.id,
            )
            AuthService.deactivate_all_user_sessions(
                current_user,
                exclude_session_token=session.get("nova_session_token"),
            )
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard.index"))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("auth/force_password_change.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    AuthService.deactivate_user_session(session.get("nova_session_token"))
    logout_user()
    clear_novadrive_session_state(session)
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/api-key/regenerate", methods=["POST"])
@login_required
def regenerate_api_key():
    session["nova_generated_api_key"] = AuthService.generate_api_key(current_user)
    flash("A new API key is ready. Copy it now because it will not be shown again.", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


@auth_bp.route("/api-key/revoke", methods=["POST"])
@login_required
def revoke_api_key():
    AuthService.revoke_api_key(current_user)
    session.pop("nova_generated_api_key", None)
    flash("API key revoked.", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


@auth_bp.route("/webdav-password/regenerate", methods=["POST"])
@login_required
def regenerate_webdav_password():
    session["nova_generated_webdav_password"] = AuthService.generate_webdav_password(current_user)
    flash(
        "A new WebDAV app password is ready. Copy it now because it will not be shown again.",
        "success",
    )
    return redirect(request.referrer or url_for("dashboard.index"))


@auth_bp.route("/webdav-password/revoke", methods=["POST"])
@login_required
def revoke_webdav_password():
    AuthService.revoke_webdav_password(current_user)
    session.pop("nova_generated_webdav_password", None)
    flash("WebDAV app password revoked.", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


@auth_bp.post("/two-factor/setup/start")
@login_required
def start_two_factor_setup():
    if current_user.is_two_factor_enabled:
        flash("Two-factor authentication is already enabled for this account.", "info")
        return _security_redirect()
    AuthService.begin_two_factor_setup(current_user, actor_id=current_user.id)
    flash(
        "A new two-factor secret is ready. Add it to your authenticator app, then enter the 6-digit code to enable 2FA.",
        "success",
    )
    return _security_redirect()


@auth_bp.post("/two-factor/setup/confirm")
@login_required
def confirm_two_factor_setup():
    form = TwoFactorChallengeForm(prefix="two_factor_setup")
    if not form.validate_on_submit():
        flash("Enter the current 6-digit code from your authenticator app.", "error")
        return _security_redirect()

    try:
        AuthService.confirm_two_factor_setup(
            current_user,
            form.code.data,
            actor_id=current_user.id,
        )
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("Two-factor authentication is now enabled for your account.", "success")
    return _security_redirect()


@auth_bp.post("/two-factor/setup/cancel")
@login_required
def cancel_two_factor_setup():
    AuthService.cancel_two_factor_setup(current_user, actor_id=current_user.id)
    flash("Two-factor setup was cancelled.", "info")
    return _security_redirect()


@auth_bp.post("/two-factor/disable")
@login_required
def disable_two_factor():
    form = TwoFactorDisableForm(prefix="two_factor_disable")
    if not form.validate_on_submit():
        flash("Enter your current password and a valid 6-digit authentication code.", "error")
        return _security_redirect()

    try:
        AuthService.disable_two_factor(
            current_user,
            password=form.password.data,
            code=form.code.data,
            actor_id=current_user.id,
        )
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("Two-factor authentication has been disabled for this account.", "success")
    return _security_redirect()


@auth_bp.get("/verify-email/<token>")
def verify_email(token: str):
    try:
        payload = VerificationService.verify_email_token(
            token,
            current_app.secret_key,
            current_app.config["EMAIL_VERIFICATION_MAX_AGE_SECONDS"],
        )
        user = db.session.get(User, int(payload["user_id"]))
        if not user:
            raise VerificationTokenError("That verification link is invalid.")
        if user.email.lower() != str(payload["email"]).lower():
            raise VerificationTokenError("That verification link is invalid.")
        AuthService.mark_email_verified(user)
        flash("Email confirmed. You can sign in now.", "success")
    except VerificationTokenError as exc:
        flash(str(exc), "error")
    return redirect(url_for("auth.login"))


@auth_bp.route("/resend-verification", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def resend_verification():
    email = (request.form.get("email") or "").strip().lower()
    if current_user.is_authenticated and not email:
        email = current_user.email.lower()

    if not email:
        flash("Enter the email address that needs a verification link.", "error")
        return redirect(url_for("auth.login"))

    # A single neutral message for every branch below so the response never
    # reveals whether the address belongs to a real (and verified) account.
    neutral_message = "If that account exists and needs confirmation, a new verification email has been sent."

    user = AuthService.find_by_email(email)
    if (
        user
        and not user.is_email_verified
        and current_app.config["EMAIL_VERIFICATION_REQUIRED"]
    ):
        last_sent_at = as_utc(user.email_verification_sent_at)
        resend_interval = current_app.config["EMAIL_VERIFICATION_RESEND_INTERVAL_SECONDS"]
        if not last_sent_at or last_sent_at + timedelta(seconds=resend_interval) <= utcnow():
            try:
                _send_verification_email(user)
            except EmailDeliveryError:
                pass

    flash(neutral_message, "success")
    return redirect(url_for("auth.login", email=email))


def _send_verification_email(user: User) -> None:
    token = VerificationService.generate_email_token(user, current_app.secret_key)
    verify_url = external_url("auth.verify_email", token=token)
    VerificationService.send_verification_email(
        user=user,
        verify_url=verify_url,
        config=current_app.config,
    )
    AuthService.note_verification_email_sent(user)


def _send_password_reset_email(user: User) -> None:
    token = VerificationService.generate_password_reset_token(user, current_app.secret_key)
    reset_url = external_url("auth.reset_password", token=token)
    VerificationService.send_password_reset_email(
        user=user,
        reset_url=reset_url,
        config=current_app.config,
    )
    AuthService.note_password_reset_email_sent(user)


def _complete_login(user: User, *, remember: bool, next_target: str | None):
    login_user(user, remember=remember)
    session["nova_session_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    AuthService.note_successful_login(user)
    AuthService.ensure_user_session(
        user=user,
        session_token=session["nova_session_token"],
        user_agent=request.headers.get("User-Agent"),
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        lifetime_hours=current_app.config["PERMANENT_SESSION_LIFETIME_HOURS"],
    )
    if AuthService.must_change_default_admin_credentials(user):
        flash(
            "Default admin credentials are still active. Change the username, email, and password now.",
            "error",
        )
        return redirect(url_for("auth.complete_default_admin_setup"))
    if AuthService.must_change_password(user):
        flash("An administrator requires this account to set a new password before continuing.", "error")
        return redirect(url_for("auth.force_password_change"))
    flash("Welcome back to NovaDrive.", "success")
    return redirect(_safe_next(next_target) or url_for("dashboard.index"))


def _safe_next(target: str | None) -> str | None:
    """Allow only same-site relative redirect targets (blocks open redirect)."""
    if not target:
        return None
    if any(ch in target for ch in ("\\", "\n", "\r", "\t")):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _store_pending_two_factor_login(
    *,
    user: User,
    remember: bool,
    next_target: str | None,
) -> None:
    session["nova_2fa_user_id"] = user.id
    session["nova_2fa_remember"] = bool(remember)
    session["nova_2fa_next"] = next_target or ""


def _clear_pending_two_factor_login() -> None:
    for key in TWO_FACTOR_LOGIN_SESSION_KEYS:
        session.pop(key, None)


def _get_pending_two_factor_user() -> User | None:
    user_id = session.get("nova_2fa_user_id")
    if not user_id:
        return None
    return db.session.get(User, int(user_id))


def _security_redirect():
    return redirect(url_for("dashboard.index", _anchor="security-panel"))


def _load_password_reset_user(token: str) -> User:
    payload = VerificationService.verify_password_reset_token(
        token,
        current_app.secret_key,
        current_app.config["PASSWORD_RESET_MAX_AGE_SECONDS"],
    )
    user = db.session.get(User, int(payload["user_id"]))
    if not user:
        raise VerificationTokenError("That password reset link is invalid.")
    if user.email.lower() != str(payload["email"]).lower():
        raise VerificationTokenError("That password reset link is invalid.")
    fingerprint = str(payload.get("fingerprint") or "")
    if fingerprint != VerificationService.password_reset_fingerprint(user):
        raise VerificationTokenError("That password reset link is no longer valid.")
    return user
