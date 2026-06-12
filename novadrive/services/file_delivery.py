from __future__ import annotations

import re

from flask import Response, request, stream_with_context

from novadrive.models import File
from novadrive.services.file_service import FileService

TEXT_MIME_TYPES = {
    "application/json",
    "application/javascript",
    "application/xml",
    "application/x-sh",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
}

TEXT_EXTENSIONS = {
    "bat",
    "c",
    "conf",
    "cpp",
    "css",
    "csv",
    "env",
    "go",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "log",
    "md",
    "py",
    "rs",
    "sh",
    "sql",
    "svg",
    "toml",
    "ts",
    "txt",
    "xml",
    "yaml",
    "yml",
}

_RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)$")


class FileDeliveryService:
    @staticmethod
    def preview_kind(file_record: File) -> str | None:
        if file_record.mime_type.startswith("image/"):
            return "image"
        if file_record.mime_type.startswith("video/"):
            return "video"
        if file_record.mime_type.startswith("audio/"):
            return "audio"
        if FileDeliveryService.is_text_previewable(file_record):
            return "text"
        return None

    @staticmethod
    def is_text_previewable(file_record: File) -> bool:
        if file_record.mime_type.startswith("text/"):
            return True
        if file_record.mime_type in TEXT_MIME_TYPES:
            return True
        return file_record.extension in TEXT_EXTENSIONS

    @staticmethod
    def get_text_preview(file_record: File, config) -> dict[str, object] | None:
        limit = config["TEXT_PREVIEW_MAX_BYTES"]
        if not FileDeliveryService.is_text_previewable(file_record):
            return None
        if file_record.total_size > limit:
            return {
                "content": None,
                "truncated": False,
                "too_large": True,
                "limit_bytes": limit,
            }

        file_stream, _ = FileService.rebuild_file(file_record, config)
        try:
            data = file_stream.read(limit + 1)
        finally:
            file_stream.close()

        truncated = len(data) > limit
        encoding = FileDeliveryService._detect_charset(file_record.mime_type)
        return {
            "content": data[:limit].decode(encoding, errors="replace"),
            "truncated": truncated,
            "too_large": False,
            "limit_bytes": limit,
        }

    @staticmethod
    def build_response(
        file_record: File,
        config,
        *,
        as_attachment: bool,
        download_name: str | None = None,
    ) -> Response:
        """Stream a file to the client.

        The body is streamed straight from storage rather than rebuilt to a
        local buffer first, so large downloads start immediately and don't trip
        reverse-proxy gateway timeouts (504). Byte-range requests are honoured.
        """
        total_size = int(file_record.total_size or 0)
        resolved_name = download_name or file_record.filename
        requested_range = FileDeliveryService._parse_range_header(
            request.headers.get("Range"),
            total_size,
        )

        if requested_range:
            start, end = requested_range
            status_code = 206
            content_length = end - start + 1
        else:
            start, end = 0, (total_size - 1 if total_size > 0 else 0)
            status_code = 200
            content_length = total_size

        body = stream_with_context(
            FileService.iter_file_content(file_record, config, start, end)
        )
        response = Response(body, status_code, mimetype=file_record.mime_type)
        response.headers["Content-Length"] = str(content_length)
        response.headers["Accept-Ranges"] = "bytes"
        disposition = "attachment" if as_attachment else "inline"
        response.headers["Content-Disposition"] = f'{disposition}; filename="{resolved_name}"'
        if status_code == 206:
            response.headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        return response

    @staticmethod
    def _parse_range_header(range_header: str | None, total_size: int) -> tuple[int, int] | None:
        if not range_header or total_size <= 0:
            return None

        match = _RANGE_PATTERN.fullmatch(range_header.strip())
        if not match:
            return None

        start_text, end_text = match.groups()
        if not start_text and not end_text:
            return None

        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else total_size - 1
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(total_size - suffix_length, 0)
            end = total_size - 1

        if start < 0 or end < start or start >= total_size:
            return None

        return start, min(end, total_size - 1)

    @staticmethod
    def _detect_charset(mime_type: str) -> str:
        if "charset=" in mime_type:
            _, _, charset = mime_type.partition("charset=")
            return charset.strip() or "utf-8"
        return "utf-8"
