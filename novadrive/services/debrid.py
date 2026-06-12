"""Debrid integration: unrestrict file-host links into direct download URLs.

Many file hosts (WorkUpload, MediaFire, 1fichier, …) gate downloads behind
Cloudflare, tokens, wait timers or captchas that can't be scraped server-side.
A debrid service (Real-Debrid, AllDebrid) resolves a host link to a plain direct
URL via its API; NovaDrive then downloads that URL normally. One API key covers
hundreds of hosts.

Configure with ``REMOTE_DOWNLOAD_DEBRID_PROVIDER`` (``realdebrid``/``alldebrid``)
and ``REMOTE_DOWNLOAD_DEBRID_API_KEY``.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = (15, 60)


def provider(config) -> str:
    return (config.get("REMOTE_DOWNLOAD_DEBRID_PROVIDER") or "").strip().lower()


def is_configured(config) -> bool:
    return bool(provider(config) and (config.get("REMOTE_DOWNLOAD_DEBRID_API_KEY") or "").strip())


def unrestrict(url: str, config) -> str | None:
    """Resolve a single host link to a direct URL, or None if unsupported."""
    if not is_configured(config):
        return None
    key = config["REMOTE_DOWNLOAD_DEBRID_API_KEY"]
    name = provider(config)
    try:
        if name == "realdebrid":
            return _realdebrid_unrestrict(url, key)
        if name == "alldebrid":
            return _alldebrid_unlock(url, key)
    except requests.RequestException as exc:
        logger.warning("Debrid unrestrict failed for %s: %s", name, exc)
    return None


def expand_folder(url: str, config) -> list[str]:
    """Expand a folder/archive link into the individual host links it contains.

    Only Real-Debrid exposes this; returns [] when unavailable.
    """
    if not is_configured(config):
        return []
    if provider(config) != "realdebrid":
        return []
    try:
        response = requests.post(
            "https://api.real-debrid.com/rest/1.0/unrestrict/folder",
            headers={"Authorization": f"Bearer {config['REMOTE_DOWNLOAD_DEBRID_API_KEY']}"},
            data={"link": url},
            timeout=_TIMEOUT,
        )
        if response.status_code >= 400:
            return []
        links = response.json()
    except (requests.RequestException, ValueError):
        return []
    return [link for link in links if isinstance(link, str) and link.startswith("http")]


def _realdebrid_unrestrict(url: str, key: str) -> str | None:
    response = requests.post(
        "https://api.real-debrid.com/rest/1.0/unrestrict/link",
        headers={"Authorization": f"Bearer {key}"},
        data={"link": url},
        timeout=_TIMEOUT,
    )
    if response.status_code >= 400:
        return None
    try:
        return (response.json() or {}).get("download")
    except ValueError:
        return None


def _alldebrid_unlock(url: str, key: str) -> str | None:
    response = requests.get(
        "https://api.alldebrid.com/v4/link/unlock",
        params={"agent": "novadrive", "apikey": key, "link": url},
        timeout=_TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError:
        return None
    if payload.get("status") != "success":
        return None
    return (payload.get("data") or {}).get("link")
