from __future__ import annotations

from urllib.parse import urljoin

from flask import current_app, url_for


def external_url(endpoint: str, **values) -> str:
    configured_base = current_app.config["APP_EXTERNAL_URL"]
    local_path = url_for(endpoint, _external=False, **values)
    if configured_base:
        return urljoin(f"{configured_base}/", local_path.lstrip("/"))
    return url_for(endpoint, _external=True, **values)
