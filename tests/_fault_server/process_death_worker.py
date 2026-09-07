"""Child-only abrupt-death probes; stdout contains bounded, secret-free events."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from notebooklm import _atomic_io
from notebooklm._auth.storage import save_cookies_to_storage, snapshot_cookie_jar

from .http import LogicalHostTransport
from .web import build_fault_client
from .web_transfers import MEDIA, NOTEBOOK, _download


def emit(kind: str, **values: object) -> None:
    print(json.dumps({"kind": kind, **values}), flush=True)


def hold(staging: Path) -> None:
    body = staging.read_bytes()
    emit("staged", bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    # Parent kills this process. No finalizer is expected to execute.
    threading.Event().wait(30)
    raise TimeoutError("parent did not terminate staged child")


def storage(path: Path, crash: bool) -> None:
    old = httpx.Cookies()
    old.set("SID", "synthetic-old", domain=".google.com", path="/")
    fresh = httpx.Cookies()
    fresh.set("SID", "synthetic-new", domain=".google.com", path="/")
    replace = _atomic_io.replace_file_atomically

    def before_replace(staging: Path, destination: Path) -> None:
        hold(staging)
        replace(staging, destination)

    with patch.object(_atomic_io, "replace_file_atomically", before_replace if crash else replace):
        saved = save_cookies_to_storage(fresh, path, original_snapshot=snapshot_cookie_jar(old))
    emit("completed", saved=saved)


async def network(mode: str, directory: Path, port: int) -> None:
    def factory(**kwargs):
        kwargs["transport"] = LogicalHostTransport(
            dict.fromkeys(["notebook.google.com", "lh3.googleusercontent.com"], ("127.0.0.1", port))
        )
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    client = build_fault_client(
        SimpleNamespace(client_factory=factory),
        timeout=20,
        transfer_timeout=20,
        rate_limit_max_retries=0,
        server_error_max_retries=0,
    )
    async with client:
        if mode.startswith("download"):
            if mode.endswith("crash"):
                client.artifacts._asset_downloads._publish_download = lambda staging, target: hold(
                    staging
                )
            destination = directory / "asset.wav"
            await _download(client, destination, batch=False)
            emit("completed", valid=destination.read_bytes() == MEDIA)
        elif mode == "commit_crash":
            emit("operation_started")
            source = await client.sources.add_file(NOTEBOOK, directory / "source.txt")
            emit("completed", source_id=source.id)
        elif mode == "reconcile":
            sources = await client.sources.list(NOTEBOOK)
            emit("reconciled", candidate_ids=[source.id for source in sources])
        else:
            raise ValueError("unknown worker mode")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.mode.startswith("storage"):
            storage(args.directory / "storage_state.json", args.mode.endswith("crash"))
        else:
            asyncio.run(network(args.mode, args.directory, args.port))
    except BaseException as error:
        emit("error", error_type=type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
