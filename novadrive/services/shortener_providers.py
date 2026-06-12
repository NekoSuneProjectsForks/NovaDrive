"""Pluggable URL-shortener providers.

NovaDrive's own short links ("native") are served from this domain. External
providers turn NovaDrive into a *proxy* shortener: the real destination is sent
to a third-party service and the returned short link becomes what ``/u/<code>``
redirects to — so clicks flow through (and can earn from) that provider.

Two categories are surfaced to users:

* **free** — no ads, no income: native, is.gd, TinyURL (and Bitly if configured).
* **community** — ad-monetised "support the community" shorteners. These are NOT
  hard-coded: the instance owner provides a single list via ``SHORTENER_PROVIDERS``
  (a JSON array of ``{"name","url","key","result"}``). ``url`` is a template with
  ``{url}`` and ``{key}`` placeholders; the user then picks any of them.

Example ``SHORTENER_PROVIDERS``::

    [
      {"name": "GPLinks", "url": "https://gplinks.co/api?api={key}&url={url}", "key": "TOKEN"},
      {"name": "ShrinkEarn", "url": "https://shrinkearn.com/api?api={key}&url={url}", "key": "TOKEN"},
      {"name": "Ouo.io", "url": "https://ouo.io/api/{key}?s={url}", "key": "TOKEN"}
    ]

Each request is a GET; the response may be JSON (``shortenedUrl``/``short``/
``url`` or a custom ``result`` field) or a plain-text short URL.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import requests

from novadrive.utils.validators import ValidationError

logger = logging.getLogger(__name__)

NATIVE = "native"
CATEGORY_FREE = "free"
CATEGORY_COMMUNITY = "community"
_TIMEOUT = (8, 20)


def _configured_providers(config) -> list[dict[str, str]]:
    return config.get("SHORTENER_PROVIDERS") or []


def _find_provider(provider_id: str, config) -> dict[str, str] | None:
    for entry in _configured_providers(config):
        if entry.get("id") == provider_id:
            return entry
    return None


def available_providers(config) -> list[dict[str, str]]:
    """All offerable providers as ``[{"id","label","category"}]`` (native first)."""
    providers = [{"id": NATIVE, "label": "NovaDrive (this domain)", "category": CATEGORY_FREE}]
    if config.get("SHORTENER_PUBLIC_ENABLED", True):
        providers.append({"id": "isgd", "label": "is.gd", "category": CATEGORY_FREE})
        providers.append({"id": "tinyurl", "label": "TinyURL", "category": CATEGORY_FREE})
    if (config.get("SHORTENER_BITLY_TOKEN") or "").strip():
        providers.append({"id": "bitly", "label": "Bitly", "category": CATEGORY_FREE})
    for entry in _configured_providers(config):
        providers.append({"id": entry["id"], "label": entry["name"], "category": CATEGORY_COMMUNITY})
    return providers


def grouped_providers(config) -> list[dict]:
    """Providers grouped for an optgroup dropdown."""
    providers = available_providers(config)
    free = [p for p in providers if p["category"] == CATEGORY_FREE]
    community = [p for p in providers if p["category"] == CATEGORY_COMMUNITY]
    groups = [{"label": "Free", "providers": free}]
    if community:
        groups.append({"label": "Support the community (ads)", "providers": community})
    return groups


def is_available(provider: str, config) -> bool:
    return any(entry["id"] == provider for entry in available_providers(config))


def shorten(provider: str, target_url: str, config) -> str:
    """Return an external short URL for ``target_url`` via ``provider``."""
    if provider == "isgd":
        return _simple_get(f"https://is.gd/create.php?format=simple&url={quote(target_url, safe='')}")
    if provider == "tinyurl":
        return _simple_get(f"https://tinyurl.com/api-create.php?url={quote(target_url, safe='')}")
    if provider == "bitly":
        return _bitly(target_url, config)
    entry = _find_provider(provider, config)
    if entry is not None:
        return _from_template(entry, target_url)
    raise ValidationError(f"Unknown shortener provider '{provider}'.")


def _from_template(entry: dict[str, str], target_url: str) -> str:
    request_url = (
        entry["url"]
        .replace("{url}", quote(target_url, safe=""))
        .replace("{key}", quote(entry.get("key") or "", safe=""))
    )
    try:
        response = requests.get(request_url, timeout=_TIMEOUT, headers={"User-Agent": "NovaDrive"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValidationError(f"{entry['name']} could not be reached: {exc}") from exc

    short: object | None = None
    try:
        data = response.json()
    except ValueError:
        short = (response.text or "").strip()
    else:
        if isinstance(data, dict):
            if str(data.get("status", "success")).lower() == "error":
                raise ValidationError(data.get("message") or f"{entry['name']} rejected the link.")
            result_key = entry.get("result")
            if result_key:
                short = data.get(result_key)
            else:
                short = (
                    data.get("shortenedUrl")
                    or data.get("short")
                    or data.get("shorturl")
                    or data.get("result")
                    or data.get("url")
                )
        else:
            short = str(data).strip()

    if not short or not str(short).lower().startswith("http"):
        raise ValidationError(f"{entry['name']} returned an unexpected response: {str(short)[:120]}")
    return str(short)


def _simple_get(url: str) -> str:
    try:
        response = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "NovaDrive"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValidationError(f"The shortener service could not be reached: {exc}") from exc
    short = (response.text or "").strip()
    if not short.lower().startswith("http"):
        raise ValidationError(f"The shortener returned an unexpected response: {short[:120]}")
    return short


def _bitly(target_url: str, config) -> str:
    token = (config.get("SHORTENER_BITLY_TOKEN") or "").strip()
    if not token:
        raise ValidationError("Bitly is not configured on this instance.")
    try:
        response = requests.post(
            "https://api-ssl.bitly.com/v4/shorten",
            json={"long_url": target_url},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        link = (response.json() or {}).get("link")
    except requests.RequestException as exc:
        raise ValidationError(f"Bitly could not be reached: {exc}") from exc
    except ValueError as exc:
        raise ValidationError("Bitly returned an invalid response.") from exc
    if not link:
        raise ValidationError("Bitly did not return a short link.")
    return link
