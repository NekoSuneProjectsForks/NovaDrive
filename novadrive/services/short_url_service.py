"""URL shortener: create and resolve short links served from this domain."""

from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

from flask import current_app

from novadrive.extensions import db
from novadrive.models import ShortUrl, User, utcnow
from novadrive.services import shortener_providers
from novadrive.utils.urls import external_url
from novadrive.utils.validators import ValidationError

_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_DEFAULT_CODE_LENGTH = 6
_RESERVED_CODES = {"api", "admin", "links", "share", "static", "u", "s"}


class ShortUrlService:
    @staticmethod
    def _random_code(length: int = _DEFAULT_CODE_LENGTH) -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))

    @staticmethod
    def _normalize_target(target_url: str) -> str:
        url = (target_url or "").strip()
        if not url:
            raise ValidationError("Enter a URL to shorten.")
        if len(url) > 4096:
            raise ValidationError("That URL is too long.")
        if "://" not in url:
            url = f"https://{url}"
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValidationError("Only http and https URLs can be shortened.")
        if not urlsplit(url).netloc:
            raise ValidationError("That URL is missing a host.")
        return url

    @staticmethod
    def _resolve_code(custom_code: str | None) -> str:
        if custom_code:
            code = custom_code.strip()
            if not _CODE_PATTERN.match(code):
                raise ValidationError(
                    "A custom alias may only contain letters, numbers, '-' and '_' (2-64 chars)."
                )
            if code.lower() in _RESERVED_CODES:
                raise ValidationError("That alias is reserved.")
            if ShortUrl.query.filter_by(code=code).first():
                raise ValidationError("That alias is already taken.")
            return code

        for _ in range(12):
            code = ShortUrlService._random_code()
            if not ShortUrl.query.filter_by(code=code).first():
                return code
        # Extremely unlikely; widen the space and try once more.
        code = ShortUrlService._random_code(_DEFAULT_CODE_LENGTH + 4)
        if ShortUrl.query.filter_by(code=code).first():
            raise ValidationError("Could not allocate a short code, please retry.")
        return code

    @staticmethod
    def create(
        user: User,
        target_url: str,
        custom_code: str | None = None,
        provider: str | None = None,
    ) -> ShortUrl:
        normalized = ShortUrlService._normalize_target(target_url)
        config = current_app.config
        provider = (provider or shortener_providers.NATIVE).strip().lower()
        if provider != shortener_providers.NATIVE and not shortener_providers.is_available(provider, config):
            raise ValidationError("That shortener provider is not available.")

        redirect_target = normalized
        final_url = normalized
        if provider != shortener_providers.NATIVE:
            # Proxy through the external provider: /u/<code> -> provider link -> target.
            redirect_target = shortener_providers.shorten(provider, normalized, config)

        code = ShortUrlService._resolve_code(custom_code)
        short_url = ShortUrl(
            owner_id=user.id,
            code=code,
            target_url=redirect_target,
            final_url=final_url,
            provider=provider,
        )
        db.session.add(short_url)
        db.session.commit()
        return short_url

    @staticmethod
    def resolve(code: str) -> ShortUrl | None:
        if not code:
            return None
        return ShortUrl.query.filter_by(code=code, is_active=True).first()

    @staticmethod
    def register_click(short_url: ShortUrl) -> None:
        short_url.click_count = (short_url.click_count or 0) + 1
        short_url.last_click_at = utcnow()
        db.session.commit()

    @staticmethod
    def list_for_user(user: User, limit: int = 200) -> list[ShortUrl]:
        return (
            ShortUrl.query.filter_by(owner_id=user.id)
            .order_by(ShortUrl.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_owned(user: User, short_url_id: int) -> ShortUrl | None:
        return ShortUrl.query.filter_by(id=short_url_id, owner_id=user.id).first()

    @staticmethod
    def delete(short_url: ShortUrl) -> None:
        db.session.delete(short_url)
        db.session.commit()

    @staticmethod
    def short_link(short_url: ShortUrl) -> str:
        return external_url("shorturl.redirect", code=short_url.code)
