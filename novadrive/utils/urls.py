from __future__ import annotations

from urllib.parse import urljoin

from flask import current_app, has_request_context, request, url_for


def external_url(endpoint: str, **values) -> str:
    local_path = url_for(endpoint, _external=False, **values)

    # When the incoming request arrives on an allow-listed host (the primary
    # APP_EXTERNAL_URL host or any host in APP_EXTERNAL_URLS), build the URL
    # from that exact host so the response works for whoever is connecting —
    # public domain and LAN address alike. Hosts are not auto-trusted, so this
    # cannot be abused via a spoofed Host header for out-of-band links (emails).
    url_map = current_app.config.get("APP_EXTERNAL_URL_MAP") or {}
    if url_map and has_request_context():
        matched_base = url_map.get(request.host.lower())
        if matched_base:
            return urljoin(f"{matched_base}/", local_path.lstrip("/"))

    configured_base = current_app.config["APP_EXTERNAL_URL"]
    if configured_base:
        return urljoin(f"{configured_base}/", local_path.lstrip("/"))
    return url_for(endpoint, _external=True, **values)
