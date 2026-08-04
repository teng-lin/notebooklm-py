"""Cold-start CLI contracts for ``notebooklm auth refresh``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.services.auth_refresh as auth_refresh_service
from notebooklm.auth import MasterTokenError
from notebooklm.notebooklm_cli import cli


def _cold_profile(tmp_path):
    storage = tmp_path / "profile" / "storage_state.json"
    storage.parent.mkdir(parents=True)
    token = storage.parent / "master_token.json"
    token.write_text("{}", encoding="utf-8")
    return storage, token


def _minting_mock(storage):
    def mint(*, storage_path, master_token_path):
        assert storage_path == storage
        assert master_token_path == storage.parent / "master_token.json"
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


@pytest.mark.parametrize(
    ("extra_args", "verified", "expects_verified_line"),
    [([], False, False), (["--verify"], True, True), (["--json"], False, False)],
)
def test_missing_storage_bootstraps_once_without_ordinary_recovery(
    tmp_path, extra_args, verified, expects_verified_line
):
    storage, token = _cold_profile(tmp_path)
    mint = _minting_mock(storage)
    ordinary = AsyncMock()
    passive = AsyncMock(return_value=("csrf", "session"))
    with (
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
        patch.object(auth_module, "fetch_tokens_passive", new=passive),
    ):
        result = CliRunner().invoke(
            cli,
            ["--storage", str(storage), "auth", "refresh", *extra_args],
        )

    assert result.exit_code == 0, result.output
    mint.assert_awaited_once_with(storage_path=storage, master_token_path=token)
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
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


def test_missing_storage_quiet_success_is_empty(tmp_path):
    storage, _ = _cold_profile(tmp_path)
    with (
        patch.object(
            auth_refresh_service.master_token,
            "refresh",
            new=_minting_mock(storage),
        ),
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
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
        patch.object(
            auth_refresh_service.master_token,
            "refresh",
            new=_minting_mock(storage),
        ),
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
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
        patch.object(auth_refresh_service.master_token, "refresh", new=mint),
        patch.object(auth_module, "fetch_tokens_with_domains", new=ordinary),
    ):
        result = CliRunner().invoke(cli, ["--storage", str(storage), "auth", "refresh"])

    assert result.exit_code == 2
    assert "malformed storage" in result.output
    mint.assert_not_awaited()
    ordinary.assert_awaited_once()
