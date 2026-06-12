from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_choice(value: str | None, allowed: set[str], default: str) -> str:
    candidate = (value or "").strip().lower()
    if candidate in allowed:
        return candidate
    return default


def _resolve_database_uri(base_dir: Path, instance_dir: Path) -> str:
    configured_value = (os.getenv("DATABASE_URL") or "").strip()
    if not configured_value:
        return f"sqlite:///{(instance_dir / 'novadrive.db').as_posix()}"

    sqlite_prefixes = ("sqlite:///", "sqlite+pysqlite:///")
    for prefix in sqlite_prefixes:
        if not configured_value.startswith(prefix):
            continue

        raw_path = configured_value[len(prefix):]
        path_part, separator, query = raw_path.partition("?")
        if not path_part or path_part == ":memory:" or path_part.startswith("/") or path_part.startswith("file:"):
            return configured_value

        absolute_path = (base_dir / path_part).resolve()
        suffix = f"{separator}{query}" if separator else ""
        return f"{prefix}{absolute_path.as_posix()}{suffix}"

    return configured_value


def _host_prefers_https(hostname: str | None) -> bool:
    if not hostname:
        return False

    normalized_host = hostname.strip().strip("[]").lower()
    if not normalized_host or normalized_host == "localhost" or "." not in normalized_host:
        return False

    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        return True
    return False


def _normalize_external_url(value: str | None) -> str:
    configured_value = (value or "").strip()
    if not configured_value:
        return ""

    if "://" in configured_value:
        return configured_value.rstrip("/")

    inferred = urlsplit(f"//{configured_value}")
    scheme = "https" if _host_prefers_https(inferred.hostname) else "http"
    return urlunsplit(
        (
            scheme,
            inferred.netloc,
            inferred.path,
            inferred.query,
            inferred.fragment,
        )
    ).rstrip("/")


def _external_url_map(primary: str, extra: str | None) -> dict[str, str]:
    """Map each known external host (``host`` or ``host:port``) to its base URL.

    The primary ``APP_EXTERNAL_URL`` is always included. Additional hosts listed
    in ``APP_EXTERNAL_URLS`` (comma/space separated) let NovaDrive serve matching
    request hosts with URLs built from that host — so the public domain and a
    LAN address such as ``192.168.1.107:5666`` both return links that resolve.
    """
    mapping: dict[str, str] = {}
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    for raw in (extra or "").replace(",", " ").split():
        normalized = _normalize_external_url(raw)
        if normalized:
            candidates.append(normalized)

    for base in candidates:
        host = urlsplit(base).netloc.lower()
        if host and host not in mapping:
            mapping[host] = base
    return mapping


def _cloudflare_safe_upload_limit_bytes(plan: str) -> int:
    limits = {
        "free": 99_000_000,
        "pro": 99_000_000,
        "business": 199_000_000,
        "enterprise": 499_000_000,
    }
    return limits.get(plan, limits["free"])


def _resolve_max_upload_size(
    configured_limit: int,
    *,
    cloudflare_tunnel_compat: bool,
    cloudflare_safe_limit: int,
) -> int:
    effective_limit = max(int(configured_limit), 1)
    if not cloudflare_tunnel_compat:
        return effective_limit

    return min(effective_limit, max(int(cloudflare_safe_limit), 1))


class Config:
    APP_NAME = "NovaDrive"

    BASE_DIR = Path(__file__).resolve().parent.parent
    INSTANCE_DIR = BASE_DIR / "instance"
    APP_EXTERNAL_URL = _normalize_external_url(os.getenv("APP_EXTERNAL_URL"))
    APP_EXTERNAL_URLS = os.getenv("APP_EXTERNAL_URLS", "")
    APP_EXTERNAL_URL_MAP = _external_url_map(APP_EXTERNAL_URL, APP_EXTERNAL_URLS)

    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri(BASE_DIR, INSTANCE_DIR)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CLOUDFLARE_TUNNEL_COMPAT = _as_bool(os.getenv("CLOUDFLARE_TUNNEL_COMPAT"), False)
    CLOUDFLARE_TUNNEL_PLAN = _as_choice(
        os.getenv("CLOUDFLARE_TUNNEL_PLAN"),
        {"free", "pro", "business", "enterprise"},
        "free",
    )
    CLOUDFLARE_TUNNEL_PLAN_SAFE_LIMIT_BYTES = _cloudflare_safe_upload_limit_bytes(
        CLOUDFLARE_TUNNEL_PLAN
    )
    CONFIGURED_MAX_UPLOAD_SIZE_BYTES = _as_int(
        os.getenv("MAX_UPLOAD_SIZE_BYTES"),
        536_870_912,
    )
    MAX_UPLOAD_SIZE_BYTES = _resolve_max_upload_size(
        CONFIGURED_MAX_UPLOAD_SIZE_BYTES,
        cloudflare_tunnel_compat=CLOUDFLARE_TUNNEL_COMPAT,
        cloudflare_safe_limit=CLOUDFLARE_TUNNEL_PLAN_SAFE_LIMIT_BYTES,
    )
    MAX_CONTENT_LENGTH = MAX_UPLOAD_SIZE_BYTES
    SPOOL_MAX_MEMORY_BYTES = _as_int(os.getenv("SPOOL_MAX_MEMORY_BYTES"), 8_388_608)
    TEXT_PREVIEW_MAX_BYTES = _as_int(os.getenv("TEXT_PREVIEW_MAX_BYTES"), 1_048_576)
    DEFAULT_USER_STORAGE_QUOTA_BYTES = _as_int(
        os.getenv("DEFAULT_USER_STORAGE_QUOTA_BYTES"),
        10 * 1024 * 1024 * 1024,
    )
    DEFAULT_ADMIN_STORAGE_QUOTA_BYTES = _as_int(
        os.getenv("DEFAULT_ADMIN_STORAGE_QUOTA_BYTES"),
        0,
    )

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _as_bool(os.getenv("SESSION_COOKIE_SECURE"), False)
    PERMANENT_SESSION_LIFETIME_HOURS = _as_int(
        os.getenv("PERMANENT_SESSION_LIFETIME_HOURS"),
        24,
    )
    ALLOW_PUBLIC_REGISTRATION = _as_bool(os.getenv("ALLOW_PUBLIC_REGISTRATION"), True)
    TWO_FACTOR_ISSUER_NAME = (os.getenv("TWO_FACTOR_ISSUER_NAME") or APP_NAME).strip() or APP_NAME

    WTF_CSRF_TIME_LIMIT = None

    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "discord")
    ALLOW_PUBLIC_SHARING = _as_bool(os.getenv("ALLOW_PUBLIC_SHARING"), True)
    SOFT_DELETE_ENABLED = _as_bool(os.getenv("SOFT_DELETE_ENABLED"), True)
    WEBDAV_ENABLED = _as_bool(os.getenv("WEBDAV_ENABLED"), True)
    WEBDAV_REALM = os.getenv("WEBDAV_REALM", "NovaDrive WebDAV")
    S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
    S3_REGION = os.getenv("S3_REGION", "").strip()
    S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "").strip()
    S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "").strip()
    S3_SESSION_TOKEN = os.getenv("S3_SESSION_TOKEN", "").strip()
    S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "").strip()
    S3_PREFIX = os.getenv("S3_PREFIX", "novadrive").strip().strip("/")
    S3_FORCE_PATH_STYLE = _as_bool(os.getenv("S3_FORCE_PATH_STYLE"), True)
    S3_PRESIGN_TTL_SECONDS = _as_int(os.getenv("S3_PRESIGN_TTL_SECONDS"), 900)

    EMAIL_VERIFICATION_REQUIRED = _as_bool(
        os.getenv("EMAIL_VERIFICATION_REQUIRED"),
        False,
    )
    EMAIL_VERIFICATION_MAX_AGE_SECONDS = _as_int(
        os.getenv("EMAIL_VERIFICATION_MAX_AGE_SECONDS"),
        86_400,
    )
    EMAIL_VERIFICATION_RESEND_INTERVAL_SECONDS = _as_int(
        os.getenv("EMAIL_VERIFICATION_RESEND_INTERVAL_SECONDS"),
        60,
    )
    PASSWORD_RESET_MAX_AGE_SECONDS = _as_int(
        os.getenv("PASSWORD_RESET_MAX_AGE_SECONDS"),
        3_600,
    )
    PASSWORD_RESET_RESEND_INTERVAL_SECONDS = _as_int(
        os.getenv("PASSWORD_RESET_RESEND_INTERVAL_SECONDS"),
        60,
    )

    SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT = _as_int(os.getenv("SMTP_PORT"), 587)
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _as_bool(os.getenv("SMTP_USE_TLS"), True)
    SMTP_USE_SSL = _as_bool(os.getenv("SMTP_USE_SSL"), False)
    SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "").strip()
    SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", APP_NAME)
    SMTP_TIMEOUT_SECONDS = _as_int(os.getenv("SMTP_TIMEOUT_SECONDS"), 20)

    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
    DISCORD_GUILD_ID = _as_int(os.getenv("DISCORD_GUILD_ID"), 0)
    DISCORD_STORAGE_CHANNEL_IDS = [
        int(channel_id.strip())
        for channel_id in os.getenv("DISCORD_STORAGE_CHANNEL_IDS", "").split(",")
        if channel_id.strip().isdigit()
    ]
    DISCORD_ATTACHMENT_LIMIT_BYTES = _as_int(
        os.getenv("DISCORD_ATTACHMENT_LIMIT_BYTES"),
        8_000_000,
    )
    DISCORD_CHUNK_MARGIN_BYTES = _as_int(
        os.getenv("DISCORD_CHUNK_MARGIN_BYTES"),
        262_144,
    )
    DISCORD_CHUNK_SIZE_BYTES = _as_int(
        os.getenv("DISCORD_CHUNK_SIZE_BYTES"),
        max(1, DISCORD_ATTACHMENT_LIMIT_BYTES - DISCORD_CHUNK_MARGIN_BYTES),
    )
    DISCORD_BOT_BRIDGE_URL = os.getenv(
        "DISCORD_BOT_BRIDGE_URL",
        "http://127.0.0.1:5051",
    ).rstrip("/")
    DISCORD_BOT_BRIDGE_SHARED_SECRET = os.getenv(
        "DISCORD_BOT_BRIDGE_SHARED_SECRET",
        "novadrive-local-secret",
    )
    DISCORD_BOT_BRIDGE_CONNECT_TIMEOUT_SECONDS = _as_int(
        os.getenv("DISCORD_BOT_BRIDGE_CONNECT_TIMEOUT_SECONDS"),
        2,
    )
    DISCORD_BOT_BRIDGE_TIMEOUT_SECONDS = _as_int(
        os.getenv("DISCORD_BOT_BRIDGE_TIMEOUT_SECONDS"),
        60,
    )
    DISCORD_UPLOAD_RETRY_COUNT = _as_int(os.getenv("DISCORD_UPLOAD_RETRY_COUNT"), 3)
    DISCORD_FETCH_RETRY_COUNT = _as_int(os.getenv("DISCORD_FETCH_RETRY_COUNT"), 0)

    SHARE_TOKEN_BYTES = _as_int(os.getenv("SHARE_TOKEN_BYTES"), 24)
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

