"""Thin aria2c JSON-RPC driver used by Phase 2 remote downloads.

A short-lived ``aria2c`` process is spawned per job with RPC enabled; we add the
URI, poll status (following the child download magnets/.torrents spawn), report
progress, honour cancellation, and return the downloaded file paths. The worker
then ingests those files through ``FileService.store_stream``.

aria2 is a single binary that covers http(s), ftp, sftp, magnet and .torrent —
so one driver serves every non-HTTP source type.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 20
_POLL_INTERVAL_SECONDS = 1.0
_STATUS_KEYS = ["status", "totalLength", "completedLength", "followedBy", "errorMessage", "files"]


class Aria2Error(RuntimeError):
    """aria2 reported a failure or is unavailable."""


class Aria2Canceled(Exception):
    """The job was canceled while aria2 was running."""


def is_available() -> bool:
    return shutil.which("aria2c") is not None


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _rpc(endpoint: str, secret: str, method: str, params: list | None = None):
    payload = {
        "jsonrpc": "2.0",
        "id": "novadrive",
        "method": method,
        "params": [f"token:{secret}", *(params or [])],
    }
    response = requests.post(endpoint, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise Aria2Error(data["error"].get("message", "aria2 RPC error"))
    return data.get("result")


def _wait_ready(endpoint: str, secret: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise Aria2Error("aria2c exited before it became ready.")
        try:
            _rpc(endpoint, secret, "aria2.getVersion")
            return
        except (requests.RequestException, Aria2Error):
            time.sleep(0.25)
    raise Aria2Error("aria2c RPC did not become ready in time.")


def download_to_dir(
    uri: str,
    dest_dir: str,
    *,
    config,
    on_progress,
    should_cancel,
) -> list[Path]:
    """Download ``uri`` into ``dest_dir`` and return the resulting file paths."""
    if not is_available():
        raise Aria2Error("aria2c is not installed on the server.")

    # Torrent permission is enforced per-user before the job is queued, so by the
    # time we reach aria2 we always follow magnets/.torrents to their content.
    port = _free_port()
    secret = secrets.token_hex(16)
    endpoint = f"http://127.0.0.1:{port}/jsonrpc"
    dest_path = Path(dest_dir).resolve()

    cmd = [
        "aria2c",
        "--enable-rpc",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        f"--dir={dest_path}",
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--follow-torrent=mem",
        "--file-allocation=none",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--auto-file-renaming=true",
        "--max-connection-per-server=8",
        "--check-certificate=false",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_ready(endpoint, secret, proc)
        gid = _rpc(endpoint, secret, "aria2.addUri", [[uri]])
        return _poll(endpoint, secret, gid, dest_path, on_progress, should_cancel)
    finally:
        try:
            _rpc(endpoint, secret, "aria2.forceShutdown")
        except (requests.RequestException, Aria2Error):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _poll(endpoint, secret, gid, dest_path, on_progress, should_cancel) -> list[Path]:
    current = gid
    while True:
        if should_cancel():
            try:
                _rpc(endpoint, secret, "aria2.remove", [current])
            except (requests.RequestException, Aria2Error):
                pass
            raise Aria2Canceled()

        status = _rpc(endpoint, secret, "aria2.tellStatus", [current, _STATUS_KEYS])
        followed = status.get("followedBy") or []
        if followed:
            # A magnet/.torrent produced a child download — track that instead.
            current = followed[0]
            continue

        state = status.get("status")
        on_progress(int(status.get("completedLength") or 0), int(status.get("totalLength") or 0))

        if state == "error":
            raise Aria2Error(status.get("errorMessage") or "aria2 download failed.")
        if state == "removed":
            raise Aria2Canceled()
        if state == "complete":
            return _collect_files(status, dest_path)
        time.sleep(_POLL_INTERVAL_SECONDS)


def _collect_files(status, dest_path: Path) -> list[Path]:
    results: list[Path] = []
    for entry in status.get("files") or []:
        raw_path = entry.get("path")
        if not raw_path:
            continue
        if int(entry.get("completedLength") or 0) <= 0:
            continue
        candidate = Path(raw_path).resolve()
        # Guard against path escaping the scratch directory.
        if dest_path not in candidate.parents and candidate != dest_path:
            logger.warning("Skipping aria2 file outside scratch dir: %s", raw_path)
            continue
        if candidate.is_file():
            results.append(candidate)
    return results
