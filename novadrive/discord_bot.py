from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import threading
from http import HTTPStatus
from urllib.parse import urlparse

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from waitress import serve

from novadrive.config import Config
from novadrive.utils.logging import configure_logging, structured_log

load_dotenv()
configure_logging(Config.LOG_LEVEL)

logger = logging.getLogger(__name__)


class NovaStorageBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = Config

    async def setup_hook(self) -> None:
        structured_log(logger, "bot.setup", guild_id=self.config.DISCORD_GUILD_ID)

    async def on_ready(self):
        structured_log(
            logger,
            "bot.ready",
            user=str(self.user),
            guild_id=self.config.DISCORD_GUILD_ID,
        )

    async def resolve_channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    async def upload_chunk(
        self,
        channel_id: int,
        filename: str,
        sha256: str,
        data: bytes,
        metadata: dict | None = None,
    ) -> dict:
        await self.wait_until_ready()
        channel = await self.resolve_channel(channel_id)
        content = json.dumps(
            {
                "app": "NovaDrive",
                "sha256": sha256,
                "filename": filename,
                "metadata": metadata or {},
            },
            separators=(",", ":"),
        )[:1900]
        message = await channel.send(
            content=content,
            file=discord.File(io.BytesIO(data), filename=filename),
        )
        attachment = message.attachments[0]
        return {
            "channel_id": str(channel.id),
            "message_id": str(message.id),
            "attachment_url": attachment.url,
            "attachment_filename": attachment.filename,
            "attachment_size": attachment.size,
        }

    async def fetch_chunk(self, channel_id: int, message_id: int) -> tuple[bytes, dict]:
        await self.wait_until_ready()
        channel = await self.resolve_channel(channel_id)
        message = await channel.fetch_message(message_id)
        if not message.attachments:
            raise FileNotFoundError("No attachment exists on the target Discord message.")
        attachment = message.attachments[0]
        data = await attachment.read(use_cached=False)
        return data, {
            "filename": attachment.filename,
            "content_type": attachment.content_type or "application/octet-stream",
            "size": attachment.size,
        }

    async def delete_chunk(self, channel_id: int, message_id: int) -> None:
        await self.wait_until_ready()
        channel = await self.resolve_channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.delete()

    async def health_snapshot(self) -> dict:
        guild = self.get_guild(self.config.DISCORD_GUILD_ID) if self.config.DISCORD_GUILD_ID else None
        channels = []
        for channel_id in self.config.DISCORD_STORAGE_CHANNEL_IDS:
            channel = self.get_channel(channel_id)
            channels.append(
                {
                    "id": channel_id,
                    "name": getattr(channel, "name", "unresolved"),
                    "resolved": channel is not None,
                }
            )
        return {
            "ok": self.is_ready(),
            "bot_user": str(self.user) if self.user else None,
            "guild_id": str(guild.id) if guild else str(self.config.DISCORD_GUILD_ID or ""),
            "guild_name": guild.name if guild else None,
            "channels": channels,
        }


class BotBridge:
    def __init__(self, bot: NovaStorageBot):
        self.bot = bot

    def run(self, coro, timeout: int = 180):
        loop = getattr(self.bot, "loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("Discord bot event loop is not ready yet.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)


bot = NovaStorageBot()
bridge = BotBridge(bot)
bridge_app = Flask("novadrive-bot-bridge")


_DEFAULT_BRIDGE_SECRET = "novadrive-local-secret"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@bridge_app.before_request
def validate_bridge_secret():
    provided = request.headers.get("X-NovaDrive-Bridge-Secret", "")
    expected = Config.DISCORD_BOT_BRIDGE_SHARED_SECRET or ""
    if not hmac.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "Unauthorized"}), HTTPStatus.UNAUTHORIZED


@bridge_app.get("/health")
def health():
    try:
        snapshot = bridge.run(bot.health_snapshot())
        return jsonify(snapshot)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), HTTPStatus.SERVICE_UNAVAILABLE


@bridge_app.post("/upload-chunk")
def upload_chunk():
    uploaded_file = request.files.get("chunk")
    if uploaded_file is None:
        return jsonify({"ok": False, "error": "Missing chunk file."}), HTTPStatus.BAD_REQUEST

    filename = request.form.get("filename") or uploaded_file.filename or "chunk.bin"
    sha256 = request.form.get("sha256", "")
    channel_id = request.form.get("channel_id", type=int)
    metadata_json = request.form.get("metadata_json", "{}")

    if not channel_id:
        return jsonify({"ok": False, "error": "Missing channel_id."}), HTTPStatus.BAD_REQUEST

    try:
        metadata = json.loads(metadata_json or "{}")
        payload = bridge.run(
            bot.upload_chunk(
                channel_id=channel_id,
                filename=filename,
                sha256=sha256,
                data=uploaded_file.read(),
                metadata=metadata,
            )
        )
        return jsonify(payload)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "metadata_json is not valid JSON."}), HTTPStatus.BAD_REQUEST
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), HTTPStatus.SERVICE_UNAVAILABLE


@bridge_app.get("/chunks/<channel_id>/<message_id>")
def fetch_chunk(channel_id: str, message_id: str):
    try:
        data, metadata = bridge.run(bot.fetch_chunk(int(channel_id), int(message_id)))
        response = Response(data, mimetype=metadata["content_type"])
        response.headers["X-Discord-Attachment-Filename"] = metadata["filename"]
        response.headers["Content-Length"] = str(metadata["size"])
        return response
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), HTTPStatus.SERVICE_UNAVAILABLE


@bridge_app.delete("/chunks/<channel_id>/<message_id>")
def delete_chunk(channel_id: str, message_id: str):
    try:
        bridge.run(bot.delete_chunk(int(channel_id), int(message_id)))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), HTTPStatus.SERVICE_UNAVAILABLE


def run_bridge_server() -> None:
    parsed = urlparse(Config.DISCORD_BOT_BRIDGE_URL)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5051
    # The bridge grants full control over Discord-backed storage, gated only by
    # the shared secret. Refuse to expose it beyond loopback while that secret is
    # still the public default — otherwise anyone on the network owns storage.
    if host not in _LOOPBACK_HOSTS and Config.DISCORD_BOT_BRIDGE_SHARED_SECRET == _DEFAULT_BRIDGE_SECRET:
        raise RuntimeError(
            "Refusing to bind the Discord bridge to a non-loopback host while "
            "DISCORD_BOT_BRIDGE_SHARED_SECRET is the default. Set a strong secret first."
        )
    structured_log(
        logger,
        "bridge.starting",
        host=host,
        port=port,
    )
    serve(bridge_app, host=host, port=port, threads=8)


def main() -> None:
    if not Config.DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")

    server_thread = threading.Thread(target=run_bridge_server, daemon=True)
    server_thread.start()
    bot.run(Config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
