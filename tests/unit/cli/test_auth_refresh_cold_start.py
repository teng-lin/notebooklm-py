"""Cold-start CLI contracts for ``notebooklm auth refresh``.

#2103 PR-2 structural follow-up: the bootstrap flock/shield/recheck machinery
relocated from ``cli/services/auth_refresh.py`` into
``notebooklm._auth.master_token`` (``bootstrap_storage_from_master_token``,
returning a four-state ``BootstrapOutcome`` rather than a bare bool). The CLI
The coarse app operation preserves that boolean adapter, and the CLI service
(``auth_refresh_service`` below) is now its pure re-export.
These tests therefore patch
``MasterTokenBootstrapper.remint_from_stored_token`` (the coordinator method
the bootstrap transaction calls to do its actual mint) rather than either the
retired coarse adapter seam or the old CLI-side ``master_token.refresh``. They
reach into ``_auth.master_token`` directly for the lock-path/outcome internals
— tests are not bound by the CLI-boundary guardrail that keeps
``src/notebooklm/cli/`` off of ``notebooklm._*``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from filelock import FileLock

import notebooklm.auth as auth_module
import notebooklm.cli.services.auth_refresh as auth_refresh_service
from notebooklm._app.master_token import (
    bootstrap_missing_storage_from_master_token as app_bootstrap_missing_storage,
)
from notebooklm._auth import master_token as mt
from notebooklm._auth.master_token_bootstrap import MasterTokenBootstrapper
from notebooklm._auth.paths import _storage_state_lock_path
from notebooklm.auth import MasterTokenError
from notebooklm.notebooklm_cli import cli


def _cold_profile(tmp_path):
    storage = tmp_path / "profile" / "storage_state.json"
    storage.parent.mkdir(parents=True)
    token = storage.parent / "master_token.json"
    token.write_text("{}", encoding="utf-8")
    return storage, token


def _minting_mock(storage):
    def mint(storage_path):
        assert storage_path == storage
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "notebooklm": {
                        "version": 1,
                        "account": {"authuser": 0, "email": "user@example.com"},
                    },
                }
            ),
            encoding="utf-8",
        )

    return AsyncMock(side_effect=mint)


def _patch_remint(effect):
    """Retarget the retired public seam to the coordinator's call-time owner."""

    async def remint(bootstrapper, *, strict_loader):
        del strict_loader
        return await effect(bootstrapper._store.path)

    return patch.object(
        MasterTokenBootstrapper,
        "remint_from_stored_token",
        autospec=True,
        side_effect=remint,
    )


@pytest.mark.parametrize(
    ("extra_args", "verified", "expects_verified_line"),
    [([], False, False), (["--verify"], True, True), (["--json"], False, False)],
)
def test_missing_storage_bootstraps_once_without_ordinary_recovery(
    tmp_path, extra_args, verified, expects_verified_line
):
    assert (
        auth_refresh_service.bootstrap_missing_storage_from_master_token
        is app_bootstrap_missing_storage
    )
    storage, _token = _cold_profile(tmp_path)
    mint = _minting_mock(storage)
    ordinary = AsyncMock()
    passive = AsyncMock(return_value=("csrf", "session"))
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
        patch.object(auth_module, "fetch_tokens_passive", new=passive),
    ):
        result = CliRunner().invoke(
            cli,
            ["--storage", str(storage), "auth", "refresh", *extra_args],
        )

    assert result.exit_code == 0, result.output
    mint.assert_awaited_once_with(storage)
    passive.assert_awaited_once_with(storage, None)
    ordinary.assert_not_awaited()
    if "--json" in extra_args:
        assert json.loads(result.stdout) == {
            "status": "ok",
            "storage_path": str(storage),
            "verified": verified,
        }
        assert result.stderr == ""
    else:
        output = " ".join(result.output.split()).lower()
        assert "ok refreshed:" in output
        assert ("ok verified: token fetch succeeds after refresh" in output) is (
            expects_verified_line
        )


def test_missing_storage_json_verify_reuses_one_passive_probe(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    mint = _minting_mock(storage)
    ordinary = AsyncMock()
    passive = AsyncMock(return_value=("csrf", "session"))
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
        patch.object(auth_module, "fetch_tokens_passive", new=passive),
    ):
        result = CliRunner().invoke(
            cli,
            ["--storage", str(storage), "auth", "refresh", "--verify", "--json"],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verified"] is True
    assert result.stderr == ""
    mint.assert_awaited_once()
    passive.assert_awaited_once()
    ordinary.assert_not_awaited()


def test_concurrent_processes_serialize_bootstrap_across_path_aliases(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    alias_storage = tmp_path / "alias-profile" / "storage_state.json.bootstrap"
    alias_storage.parent.mkdir()
    alias_storage.symlink_to(storage)
    (alias_storage.parent / "master_token.json").write_text("{}", encoding="utf-8")
    assert mt._bootstrap_lock_path(alias_storage) == mt._bootstrap_lock_path(storage)
    assert mt._bootstrap_lock_path(alias_storage) != _storage_state_lock_path(alias_storage)
    start = tmp_path / "start"
    ready_paths = [tmp_path / f"ready-{index}" for index in range(2)]
    marker_paths = [tmp_path / f"mint-{index}" for index in range(2)]
    worker_storage_paths = [storage, alias_storage]
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import sys
        import time
        from pathlib import Path

        from filelock import FileLock
        import notebooklm.cli.services.auth_refresh as service
        from notebooklm._auth import master_token as mt
        from notebooklm._auth.master_token_bootstrap import MasterTokenBootstrapper
        from notebooklm._auth.paths import _storage_state_lock_path

        storage, ready, start, marker = map(Path, sys.argv[1:])

        async def mint(bootstrapper, *, strict_loader):
            del strict_loader
            storage_path = bootstrapper._store.path
            # #2103 PR-1: master_token_path_for resolves the storage path
            # (expanduser().resolve()) before deriving the sibling, so the
            # ALIAS worker's derived master_token_path resolves THROUGH the
            # symlink to the real directory's master_token.json — not a
            # sibling of the alias path itself (the old raw
            # ``storage_path.parent`` join). This IS the invariant this PR
            # restores: both workers must agree on the same real file
            # regardless of which path alias they were given, exactly like
            # they already agree on the same bootstrap lock (asserted above).
            marker.write_text("minted", encoding="utf-8")
            await asyncio.sleep(0.25)
            with FileLock(str(_storage_state_lock_path(storage_path)), timeout=2):
                storage_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")

        MasterTokenBootstrapper.remint_from_stored_token = mint
        ready.write_text("ready", encoding="utf-8")
        while not start.exists():
            time.sleep(0.01)
        print(json.dumps(asyncio.run(service.bootstrap_missing_storage_from_master_token(storage))))
        """
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(worker_storage),
                str(ready),
                str(start),
                str(marker),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for ready, marker, worker_storage in zip(
            ready_paths, marker_paths, worker_storage_paths, strict=True
        )
    ]

    try:
        deadline = time.monotonic() + 20
        while not all(path.exists() for path in ready_paths):
            assert time.monotonic() < deadline, "bootstrap workers did not become ready"
            assert all(process.poll() is None for process in processes), (
                "bootstrap worker exited before the concurrency barrier"
            )
            time.sleep(0.01)
        start.write_text("start", encoding="utf-8")
        completed = [process.communicate(timeout=20) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    for process, (_stdout, stderr) in zip(processes, completed, strict=True):
        assert process.returncode == 0, stderr
    assert [json.loads(stdout) for stdout, _ in completed] == [True, True]
    assert sum(path.exists() for path in marker_paths) == 1


async def test_cancelled_bootstrap_settles_persistence_before_unlocking(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    persist_started = threading.Event()
    persist_release = threading.Event()

    def persist() -> None:
        persist_started.set()
        assert persist_release.wait(timeout=5)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    async def mint(storage_path):
        assert storage_path == storage
        await asyncio.to_thread(persist)

    mint_mock = AsyncMock(side_effect=mint)
    with _patch_remint(mint_mock):
        leader = asyncio.create_task(
            auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
        )
        assert await asyncio.to_thread(persist_started.wait, 5)
        leader.cancel()
        follower = asyncio.create_task(
            auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
        )
        await asyncio.sleep(0.1)
        assert not leader.done()
        assert not follower.done()

        persist_release.set()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert await follower is True

    mint_mock.assert_awaited_once()


async def test_cancelled_bootstrap_lock_waiter_does_not_leak_lock(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    lock_path = mt._bootstrap_lock_path(storage)
    holder = FileLock(str(lock_path))
    holder.acquire()
    waiter = asyncio.create_task(
        auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
    )
    await asyncio.sleep(0.1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.release()

    with _patch_remint(_minting_mock(storage)) as mint:
        assert await auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
    mint.assert_awaited_once()


def test_missing_storage_quiet_success_is_empty(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    with (
        _patch_remint(_minting_mock(storage)),
        patch.object(auth_module, "fetch_tokens_with_domains", new=AsyncMock()) as ordinary,
        patch.object(
            auth_module,
            "fetch_tokens_passive",
            new=AsyncMock(return_value=("csrf", "session")),
        ) as passive,
    ):
        result = CliRunner().invoke(
            cli,
            ["--storage", str(storage), "auth", "refresh", "--quiet"],
        )

    assert result.exit_code == 0, result.output
    assert result.output == ""
    ordinary.assert_not_awaited()
    passive.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_missing_storage_bootstrap_mints_once(tmp_path):
    storage, _token = _cold_profile(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def mint(storage_path):
        assert storage_path == storage
        started.set()
        await release.wait()
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    effect = AsyncMock(side_effect=mint)
    with _patch_remint(effect) as mint_mock:
        first = asyncio.create_task(
            auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
        )
        await started.wait()
        follower = asyncio.create_task(
            auth_refresh_service.bootstrap_missing_storage_from_master_token(storage)
        )
        await asyncio.sleep(0)
        assert not follower.done()
        release.set()

        assert await asyncio.gather(first, follower) == [True, True]

    mint_mock.assert_awaited_once()


@pytest.mark.parametrize("json_output", [False, True])
def test_missing_storage_mint_failure_is_typed(tmp_path, json_output):
    storage, _ = _cold_profile(tmp_path)
    mint = AsyncMock(side_effect=MasterTokenError("master token revoked"))
    ordinary = AsyncMock()
    passive = AsyncMock()
    args = ["--storage", str(storage), "auth", "refresh"]
    if json_output:
        args.append("--json")
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
        patch.object(auth_module, "fetch_tokens_passive", new=passive),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    if json_output:
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "error": True,
            "code": "master_token_refresh_failed",
            "message": "master token revoked",
        }
    else:
        assert result.stdout == ""
        assert result.stderr.strip() == "Error: master token revoked"
    ordinary.assert_not_awaited()
    passive.assert_not_awaited()


@pytest.mark.parametrize("json_output", [False, True])
def test_post_mint_passive_failure_does_not_enter_recovery(tmp_path, json_output):
    storage, _ = _cold_profile(tmp_path)
    mint = _minting_mock(storage)
    ordinary = AsyncMock()
    passive = AsyncMock(side_effect=ValueError("still redirected"))
    args = ["--storage", str(storage), "auth", "refresh"]
    if json_output:
        args.extend(["--quiet", "--json"])
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
        patch.object(auth_module, "fetch_tokens_passive", new=passive),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    expected = "refresh completed but the post-refresh token fetch failed: still redirected"
    if json_output:
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "error": True,
            "code": "post_refresh_token_fetch_failed",
            "message": expected,
        }
    else:
        assert result.stdout == ""
        assert result.stderr.strip() == f"Error: {expected}"
    mint.assert_awaited_once()
    passive.assert_awaited_once()
    ordinary.assert_not_awaited()


def test_allow_headless_bootstrap_still_skips_ordinary_recovery(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    with (
        _patch_remint(_minting_mock(storage)),
        patch.object(auth_module, "fetch_tokens_with_domains", new=AsyncMock()) as ordinary,
        patch.object(
            auth_module,
            "fetch_tokens_passive",
            new=AsyncMock(return_value=("csrf", "session")),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            ["--storage", str(storage), "auth", "refresh", "--allow-headless"],
        )

    assert result.exit_code == 0, result.output
    ordinary.assert_not_awaited()


def test_healthy_storage_with_sibling_token_does_not_mint(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    mint = AsyncMock()
    ordinary = AsyncMock(return_value=("csrf", "session"))
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
    ):
        result = CliRunner().invoke(cli, ["--storage", str(storage), "auth", "refresh"])

    assert result.exit_code == 0, result.output
    mint.assert_not_awaited()
    ordinary.assert_awaited_once_with(storage, None, allow_headless=False)


def test_missing_storage_without_token_preserves_unexpected_error(tmp_path):
    storage = tmp_path / "profile" / "storage_state.json"
    storage.parent.mkdir(parents=True)
    mint = AsyncMock()
    ordinary = AsyncMock(side_effect=FileNotFoundError("storage_state.json not found"))
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
    ):
        result = CliRunner().invoke(cli, ["--storage", str(storage), "auth", "refresh"])

    assert result.exit_code == 2
    assert "Unexpected error" in result.output
    mint.assert_not_awaited()
    ordinary.assert_awaited_once()


def test_malformed_existing_storage_never_bootstraps(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    storage.write_text("not-json", encoding="utf-8")
    mint = AsyncMock()
    ordinary = AsyncMock(side_effect=ValueError("malformed storage"))
    with (
        _patch_remint(mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
    ):
        result = CliRunner().invoke(cli, ["--storage", str(storage), "auth", "refresh"])

    assert result.exit_code == 2
    assert "malformed storage" in result.output
    mint.assert_not_awaited()
    ordinary.assert_awaited_once()
