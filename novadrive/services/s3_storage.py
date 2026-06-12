from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from novadrive.services.storage_base import StorageBackendError
from novadrive.utils.logging import structured_log

logger = logging.getLogger(__name__)


class S3StorageBackend:
    # S3 has no per-object size limit comparable to Discord's attachment cap, so
    # files are stored as a single object instead of being split into chunks.
    whole_file = True

    def __init__(self, config: Mapping[str, Any]):
        self.endpoint_url = (config.get("S3_ENDPOINT_URL") or "").strip() or None
        self.region = (config.get("S3_REGION") or "").strip() or None
        self.access_key_id = (config.get("S3_ACCESS_KEY_ID") or "").strip() or None
        self.secret_access_key = (config.get("S3_SECRET_ACCESS_KEY") or "").strip() or None
        self.session_token = (config.get("S3_SESSION_TOKEN") or "").strip() or None
        self.bucket_name = (config.get("S3_BUCKET_NAME") or "").strip()
        self.prefix = (config.get("S3_PREFIX") or "").strip().strip("/")
        self.force_path_style = bool(config.get("S3_FORCE_PATH_STYLE", True))

        if not self.bucket_name:
            raise StorageBackendError("S3_BUCKET_NAME must be configured when STORAGE_BACKEND=s3.")

        addressing_style = "path" if self.force_path_style else "virtual"
        session = boto3.session.Session(
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            aws_session_token=self.session_token,
            region_name=self.region,
        )
        self.client = session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            config=BotoConfig(
                s3={"addressing_style": addressing_style},
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )

    def choose_channel(self, file_id: int, chunk_index: int) -> str:
        return self.bucket_name

    def health_check(self) -> dict[str, Any]:
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            payload = {
                "ok": True,
                "backend": "s3",
                "bucket_name": self.bucket_name,
                "endpoint_url": self.endpoint_url or "aws-default",
                "region": self.region or "auto",
                "prefix": self.prefix or "/",
                "channels": [
                    {
                        "name": self.bucket_name,
                        "resolved": True,
                    }
                ],
            }
            structured_log(
                logger,
                "storage.health_check",
                status="ok",
                backend="s3",
                bucket_name=self.bucket_name,
            )
            return payload
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.health_check",
                status="error",
                backend="s3",
                error=str(exc),
            )
            raise StorageBackendError("Unable to reach the configured S3 bucket.") from exc

    def upload_chunk(
        self,
        chunk_bytes: bytes,
        filename: str,
        sha256: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        object_key = self._build_object_key(filename, sha256, metadata or {})
        safe_metadata = self._sanitize_metadata(metadata or {})
        safe_metadata["sha256"] = sha256

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=object_key,
                Body=chunk_bytes,
                ContentType="application/octet-stream",
                Metadata=safe_metadata,
            )
            payload = {
                "channel_id": self.bucket_name,
                "message_id": object_key,
                "attachment_url": f"s3://{self.bucket_name}/{object_key}",
                "attachment_filename": filename,
            }
            structured_log(
                logger,
                "storage.chunk_uploaded",
                backend="s3",
                bucket_name=self.bucket_name,
                object_key=object_key,
                filename=filename,
            )
            return payload
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.chunk_upload_failed",
                backend="s3",
                bucket_name=self.bucket_name,
                filename=filename,
                error=str(exc),
            )
            raise StorageBackendError("Chunk upload to S3 failed.") from exc

    def fetch_chunk(self, channel_id: str | int, message_id: str | int) -> bytes:
        bucket_name = str(channel_id or self.bucket_name)
        object_key = str(message_id)
        try:
            response = self.client.get_object(Bucket=bucket_name, Key=object_key)
            chunk_bytes = response["Body"].read()
            structured_log(
                logger,
                "storage.chunk_fetched",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
                chunk_size=len(chunk_bytes),
            )
            return chunk_bytes
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.chunk_fetch_failed",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
                error=str(exc),
            )
            raise StorageBackendError("Chunk download from S3 failed.") from exc

    def delete_chunk(self, channel_id: str | int, message_id: str | int) -> None:
        bucket_name = str(channel_id or self.bucket_name)
        object_key = str(message_id)
        try:
            self.client.delete_object(Bucket=bucket_name, Key=object_key)
            structured_log(
                logger,
                "storage.chunk_deleted",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
            )
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.chunk_delete_failed",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
                error=str(exc),
            )
            raise StorageBackendError("Chunk deletion from S3 failed.") from exc

    def upload_whole(
        self,
        stream: Any,
        filename: str,
        sha256: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store the entire file as one S3 object, streamed from ``stream``.

        Unlike :meth:`upload_chunk`, the body is never fully materialised in
        memory: ``upload_fileobj`` streams it and transparently switches to a
        multipart upload for large files.
        """
        object_key = self._build_object_key(filename, sha256, metadata or {})
        safe_metadata = self._sanitize_metadata(metadata or {})
        safe_metadata["sha256"] = sha256

        try:
            self.client.upload_fileobj(
                Fileobj=stream,
                Bucket=self.bucket_name,
                Key=object_key,
                ExtraArgs={
                    "ContentType": "application/octet-stream",
                    "Metadata": safe_metadata,
                },
            )
            payload = {
                "channel_id": self.bucket_name,
                "message_id": object_key,
                "attachment_url": f"s3://{self.bucket_name}/{object_key}",
                "attachment_filename": filename,
            }
            structured_log(
                logger,
                "storage.object_uploaded",
                backend="s3",
                bucket_name=self.bucket_name,
                object_key=object_key,
                filename=filename,
            )
            return payload
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.object_upload_failed",
                backend="s3",
                bucket_name=self.bucket_name,
                filename=filename,
                error=str(exc),
            )
            raise StorageBackendError("Object upload to S3 failed.") from exc

    def fetch_whole(
        self,
        channel_id: str | int,
        message_id: str | int,
        dest_stream: Any,
        hasher: Any | None = None,
    ) -> int:
        """Stream an S3 object into ``dest_stream`` without buffering it whole.

        Returns the number of bytes written. When ``hasher`` is provided it is
        updated incrementally so the caller can verify the digest.
        """
        bucket_name = str(channel_id or self.bucket_name)
        object_key = str(message_id)
        try:
            response = self.client.get_object(Bucket=bucket_name, Key=object_key)
            body = response["Body"]
            total = 0
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                dest_stream.write(chunk)
                if hasher is not None:
                    hasher.update(chunk)
                total += len(chunk)
            structured_log(
                logger,
                "storage.object_fetched",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
                object_size=total,
            )
            return total
        except (BotoCoreError, ClientError) as exc:
            structured_log(
                logger,
                "storage.object_fetch_failed",
                backend="s3",
                bucket_name=bucket_name,
                object_key=object_key,
                error=str(exc),
            )
            raise StorageBackendError("Object download from S3 failed.") from exc

    def _build_object_key(self, filename: str, sha256: str, metadata: dict[str, Any]) -> str:
        file_id = metadata.get("file_id", "file")
        chunk_index = int(metadata.get("chunk_index", 0))
        prefix = f"{self.prefix}/" if self.prefix else ""
        return (
            f"{prefix}files/{file_id}/"
            f"{chunk_index:06d}-{sha256[:12]}-{filename}"
        )

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in metadata.items():
            normalized_key = str(key).strip().lower().replace(" ", "_").replace("-", "_")
            if not normalized_key:
                continue
            result[normalized_key[:128]] = str(value)[:1024]
        return result
