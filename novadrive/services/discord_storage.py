from __future__ import annotations

import io
import json
import logging
from collections.abc import Mapping
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from novadrive.services.storage_base import StorageBackendError
from novadrive.utils.logging import structured_log

logger = logging.getLogger(__name__)


class DiscordStorageBackend:
    # Discord caps attachment size, so files are split into chunks per message.
    whole_file = False

    def __init__(self, config: Mapping[str, Any]):
        self.base_url = config["DISCORD_BOT_BRIDGE_URL"]
        self.shared_secret = config["DISCORD_BOT_BRIDGE_SHARED_SECRET"]
        self.connect_timeout = max(1, int(config["DISCORD_BOT_BRIDGE_CONNECT_TIMEOUT_SECONDS"]))
        self.timeout = config["DISCORD_BOT_BRIDGE_TIMEOUT_SECONDS"]
        self.storage_channel_ids = config["DISCORD_STORAGE_CHANNEL_IDS"]

        upload_retry = Retry(
            total=int(config["DISCORD_UPLOAD_RETRY_COUNT"]),
            connect=int(config["DISCORD_UPLOAD_RETRY_COUNT"]),
            read=int(config["DISCORD_UPLOAD_RETRY_COUNT"]),
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods={"POST", "DELETE"},
        )
        fetch_retry = Retry(
            total=int(config["DISCORD_FETCH_RETRY_COUNT"]),
            connect=int(config["DISCORD_FETCH_RETRY_COUNT"]),
            read=int(config["DISCORD_FETCH_RETRY_COUNT"]),
            backoff_factor=0.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods={"GET"},
        )
        adapter = HTTPAdapter(max_retries=upload_retry)
        fetch_adapter = HTTPAdapter(max_retries=fetch_retry)
        self.session = requests.Session()
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.fetch_session = requests.Session()
        self.fetch_session.mount("http://", fetch_adapter)
        self.fetch_session.mount("https://", fetch_adapter)

    def _headers(self) -> dict[str, str]:
        return {"X-NovaDrive-Bridge-Secret": self.shared_secret}

    def choose_channel(self, file_id: int, chunk_index: int) -> int:
        if not self.storage_channel_ids:
            raise StorageBackendError("No Discord storage channels are configured.")
        return self.storage_channel_ids[(file_id + chunk_index) % len(self.storage_channel_ids)]

    def health_check(self) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/health",
                headers=self._headers(),
                timeout=(self.connect_timeout, 5),
            )
            response.raise_for_status()
            payload = response.json()
            structured_log(logger, "storage.health_check", status="ok", payload=payload)
            return payload
        except requests.RequestException as exc:
            structured_log(logger, "storage.health_check", status="error", error=str(exc))
            raise StorageBackendError("Unable to reach the Discord bot bridge.") from exc

    def upload_chunk(
        self,
        chunk_bytes: bytes,
        filename: str,
        sha256: str,
        channel_id: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/upload-chunk",
                headers=self._headers(),
                data={
                    "filename": filename,
                    "sha256": sha256,
                    "channel_id": str(channel_id),
                    "metadata_json": json.dumps(metadata or {}),
                },
                files={
                    "chunk": (
                        filename,
                        io.BytesIO(chunk_bytes),
                        "application/octet-stream",
                    )
                },
                timeout=(self.connect_timeout, self.timeout),
            )
            response.raise_for_status()
            payload = response.json()
            structured_log(
                logger,
                "storage.chunk_uploaded",
                channel_id=channel_id,
                message_id=payload.get("message_id"),
                filename=filename,
            )
            return payload
        except requests.RequestException as exc:
            structured_log(
                logger,
                "storage.chunk_upload_failed",
                channel_id=channel_id,
                filename=filename,
                error=str(exc),
            )
            raise StorageBackendError("Chunk upload to Discord failed.") from exc

    def fetch_chunk(self, channel_id: str | int, message_id: str | int) -> bytes:
        try:
            response = self.fetch_session.get(
                f"{self.base_url}/chunks/{channel_id}/{message_id}",
                headers=self._headers(),
                timeout=(self.connect_timeout, self.timeout),
            )
            response.raise_for_status()
            structured_log(
                logger,
                "storage.chunk_fetched",
                channel_id=str(channel_id),
                message_id=str(message_id),
                chunk_size=len(response.content),
            )
            return response.content
        except requests.RequestException as exc:
            structured_log(
                logger,
                "storage.chunk_fetch_failed",
                channel_id=str(channel_id),
                message_id=str(message_id),
                error=str(exc),
            )
            raise StorageBackendError("Chunk download from Discord failed.") from exc

    def delete_chunk(self, channel_id: str | int, message_id: str | int) -> None:
        try:
            response = self.session.delete(
                f"{self.base_url}/chunks/{channel_id}/{message_id}",
                headers=self._headers(),
                timeout=(self.connect_timeout, self.timeout),
            )
            response.raise_for_status()
            structured_log(
                logger,
                "storage.chunk_deleted",
                channel_id=str(channel_id),
                message_id=str(message_id),
            )
        except requests.RequestException as exc:
            structured_log(
                logger,
                "storage.chunk_delete_failed",
                channel_id=str(channel_id),
                message_id=str(message_id),
                error=str(exc),
            )
            raise StorageBackendError("Chunk deletion from Discord failed.") from exc
