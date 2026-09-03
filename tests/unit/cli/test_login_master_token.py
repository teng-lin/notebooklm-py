"""CLI tests for `notebooklm login --master-token[-refresh]` (service mocked).

Phase 13C: the Click driver invokes coarse ``notebooklm._app.master_token``
operations and patches its own bound ``bootstrap_login`` / ``remint_login``
names.  Transaction policy remains behind the app boundary; the CLI-only
service retains just interactive ``capture_oauth_token`` browser I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import notebooklm.cli.master_token_login as driver
import notebooklm.cli.services.login.master_token as mt_service
from notebooklm._app.master_token import (
    MasterTokenLoginSuccess,
    MasterTokenRemintSuccess,
)
from notebooklm._browser import browser_capture
from notebooklm.notebooklm_cli import cli
from notebooklm.paths import get_storage_path


def test_oauth_capture_adapter_delegates_to_auth_facade(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def capture(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "oauth-token"

    monkeypatch.setattr(mt_service, "capture_browser_oauth_token", capture)

    assert (
        mt_service.capture_oauth_token(
            browser="chrome",
            cdp_url="http://localhost:9222",
            timeout_s=19.0,
        )
        == "oauth-token"
    )
    assert captured == {
        "browser": "chrome",
        "cdp_url": "http://localhost:9222",
        "timeout_s": 19.0,
    }


def test_oauth_capture_adapter_scrubs_failure_frame(monkeypatch) -> None:
    secret_endpoint = "http://user:secret@localhost:9222"
    error = RuntimeError("capture failed")

    def fail_capture(**kwargs: Any) -> str:
        assert kwargs["cdp_url"] == secret_endpoint
        raise error

    monkeypatch.setattr(mt_service, "capture_browser_oauth_token", fail_capture)
    with pytest.raises(RuntimeError) as caught:
        mt_service.capture_oauth_token(cdp_url=secret_endpoint)

    adapter_frames = [
        frame_info.frame
        for frame_info in inspect.getinnerframes(caught.value.__traceback__)
        if frame_info.frame.f_code.co_name == "capture_oauth_token"
        and Path(frame_info.frame.f_code.co_filename).parts[-4:]
        == ("cli", "services", "login", "master_token.py")
    ]
    assert len(adapter_frames) == 1
    assert {"browser", "cdp_url", "timeout_s"}.isdisjoint(adapter_frames[0].f_locals)


def _seed_profile_account(monkeypatch, tmp_path, email):
    """Write a storage_state.json whose persisted account is ``email``."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    sp = get_storage_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        json.dumps(
            {
                "cookies": [],
                "notebooklm": {"version": 1, "account": {"authuser": 0, "email": email}},
            }
        )
    )


def test_master_token_refresh_calls_service(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    storage = get_storage_path()
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    with patch.object(
        driver,
        "remint_login",
        return_value=MasterTokenRemintSuccess(storage),
    ) as ref:
        result = CliRunner().invoke(cli, ["login", "--master-token-refresh"])
    assert result.exit_code == 0, result.output
    # #2103 PR-2 D2: one-path signatures — master_token_remint derives the
    # sibling internally via master_token_path_for, so the CLI passes only
    # storage_path (positionally); get_master_token_path() is no longer
    # something this call site needs to compute at all.
    ref.assert_called_once_with(storage, run_async=asyncio.run)
    assert result.output.strip() == f"Re-minted cookies -> {storage}"


def test_master_token_refresh_storage_override_canonicalizes_storage_path(tmp_path, monkeypatch):
    """#2103: an explicit ``--storage`` must reach the transaction canonicalized.

    (PR-2 note: the sibling-derivation invariant this test used to pin
    end-to-end now lives entirely inside ``master_token_path_for`` — covered
    by ``tests/unit/test_paths.py`` — since the CLI no longer computes or
    passes ``master_token_path`` itself. What remains CLI-level behavior is
    that ``--storage`` gets canonicalized before the transaction call.)
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    override_dir = tmp_path / "elsewhere"
    override_dir.mkdir()
    storage = override_dir / "foo.json"
    storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    resolved = storage.resolve()
    with patch.object(
        driver,
        "remint_login",
        return_value=MasterTokenRemintSuccess(resolved),
    ) as ref:
        result = CliRunner().invoke(
            cli, ["login", "--master-token-refresh", "--storage", str(storage)]
        )
    assert result.exit_code == 0, result.output
    # The driver canonicalizes an explicit --storage (matching auth_source), so a
    # symlink/relative alias selects the same sibling the L4 recovery rung derives.
    ref.assert_called_once_with(resolved, run_async=asyncio.run)


def test_master_token_refresh_storage_symlink_canonicalizes_to_target(tmp_path, monkeypatch):
    """#2104 review: a symlinked ``--storage`` alias resolves to the target.

    The L4 recovery rung resolves the storage path (``canonical_storage_key``)
    before deriving ``master_token.json``, so the writer must canonicalize too —
    otherwise login writes the token beside the alias while recovery looks
    beside the real file.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    storage = real_dir / "foo.json"
    storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    alias_dir = tmp_path / "alias"
    try:
        alias_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("platform cannot create directory symlinks")
    resolved = storage.resolve()  # tmp_path itself may be a symlink (macOS /tmp)
    with patch.object(
        driver,
        "remint_login",
        return_value=MasterTokenRemintSuccess(resolved),
    ) as ref:
        result = CliRunner().invoke(
            cli, ["login", "--master-token-refresh", "--storage", str(alias_dir / "foo.json")]
        )
    assert result.exit_code == 0, result.output
    ref.assert_called_once_with(resolved, run_async=asyncio.run)


def test_master_token_login_storage_override_canonicalizes_path(tmp_path, monkeypatch):
    """#2103: the bootstrap call must honor a canonicalized ``--storage``."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    storage = tmp_path / "elsewhere" / "foo.json"
    storage.parent.mkdir()
    resolved = storage.resolve()
    with patch.object(
        driver,
        "bootstrap_login",
        return_value=MasterTokenLoginSuccess(7, resolved),
    ) as boot:
        result = CliRunner().invoke(
            cli,
            [
                "login",
                "--master-token",
                "--account",
                "e@x.com",
                "--oauth-token",
                "TOK",
                "--storage",
                str(storage),
            ],
        )
    assert result.exit_code == 0, result.output
    assert boot.call_args.args[0].storage_path == resolved
    assert boot.call_args.kwargs["run_async"] is asyncio.run


def test_master_token_refresh_help_marks_forced_route_legacy():
    result = CliRunner().invoke(cli, ["login", "--help"])

    assert result.exit_code == 0, result.output
    assert "Legacy forced re-mint; prefer 'notebooklm auth refresh'." in " ".join(
        result.output.split()
    )


def test_master_token_requires_account(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    result = CliRunner().invoke(cli, ["login", "--master-token"])
    assert result.exit_code == 1
    assert "--account" in result.output


def test_master_token_bootstrap_calls_service(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    storage = get_storage_path()
    with patch.object(
        driver,
        "bootstrap_login",
        return_value=MasterTokenLoginSuccess(7, storage),
    ) as boot:
        result = CliRunner().invoke(
            cli,
            ["login", "--master-token", "--account", "e@x.com", "--oauth-token", "TOK"],
        )
    assert result.exit_code == 0, result.output
    assert boot.called
    assert boot.call_args.kwargs["oauth_token"] == "TOK"
    # #2103 PR-2 D5: android_id resolution moved inside the transaction — the
    # CLI passes it through as-is (None here, since --android-id wasn't given).
    assert boot.call_args.args[0].android_id is None
    assert "7 notebooks" in result.output


def test_master_token_bootstrap_browser_capture_when_no_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))

    def bootstrap(plan, **kwargs):
        token = kwargs["oauth_token"] or kwargs["capture_oauth_token"](
            browser=kwargs["browser"],
            cdp_url=kwargs["cdp_url"],
            timeout_s=kwargs["timeout_s"],
        )
        assert token == "CAPTOK"
        return MasterTokenLoginSuccess(3, plan.storage_path)

    with (
        patch.object(mt_service, "capture_oauth_token", return_value="CAPTOK") as cap,
        patch.object(driver, "bootstrap_login", side_effect=bootstrap) as boot,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "login",
                "--master-token",
                "--account",
                "e@x.com",
                "--browser-timeout",
                "420",
            ],
        )
    assert result.exit_code == 0, result.output
    assert cap.called
    assert boot.call_args.kwargs["oauth_token"] is None
    assert boot.call_args.kwargs["timeout_s"] == 420
    assert boot.call_args.kwargs["capture_oauth_token"] is cap
    cap.assert_called_once_with(browser="chromium", cdp_url=None, timeout_s=420)


@pytest.mark.parametrize(
    ("browser", "expected_fragment"),
    [
        # Playwright's REAL spawn-veto text (utils/processLauncher.js builds
        # `new Error("Failed to launch: " + error)`), on both the bundled build
        # and a system channel.
        ("chromium", "refused to start the browser"),
        ("chrome", "refused to start the browser"),
    ],
)
@pytest.mark.requires_playwright
def test_master_token_bootstrap_explains_a_launch_veto(
    tmp_path, monkeypatch, browser, expected_fragment
):
    """`login --master-token` spawns a headed browser, so it hits the same veto.

    docs/troubleshooting.md routes #2004 readers here as a workaround, so this
    path must not answer with the raw "This may be a bug" handler either.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    with patch("playwright.sync_api.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch.side_effect = Exception(
            "Failed to launch: Error: spawn UNKNOWN"
        )
        result = CliRunner().invoke(
            cli, ["login", "--master-token", "--account", "e@x.com", "--browser", browser]
        )

    assert result.exit_code == 1
    assert expected_fragment in result.output
    assert "This may be a bug" not in result.output


@pytest.mark.requires_playwright
def test_master_token_bootstrap_reraises_an_unclassified_launch_failure(tmp_path, monkeypatch):
    """Unrecognized failures must keep propagating rather than get a wrong hint."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    with patch("playwright.sync_api.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch.side_effect = Exception(
            "Timeout 30000ms exceeded"
        )
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])

    assert result.exit_code == 2
    assert "This may be a bug" in result.output


class _ReachedPlaywright(RuntimeError):
    pass


class _PolicyProbe:
    def __init__(self, get_policy, default_policy):
        self._get_policy = get_policy
        self._default_policy = default_policy

    def __enter__(self):
        assert self._get_policy() is self._default_policy
        raise _ReachedPlaywright

    def __exit__(self, *args):
        return False


@pytest.mark.requires_playwright
@pytest.mark.parametrize("cdp_url", [None, "http://localhost:9222"])
def test_master_token_capture_uses_default_windows_policy(monkeypatch, cdp_url):
    """The policy swap must happen before Playwright enters, then be restored."""
    original_policy = object()
    default_policy = object()
    current_policy = original_policy
    transitions = []

    def get_policy():
        return current_policy

    def set_policy(policy):
        nonlocal current_policy
        current_policy = policy
        transitions.append(policy)

    monkeypatch.setattr(asyncio, "get_event_loop_policy", get_policy)
    monkeypatch.setattr(asyncio, "set_event_loop_policy", set_policy)
    monkeypatch.setattr(asyncio, "DefaultEventLoopPolicy", lambda: default_policy)
    monkeypatch.setattr(browser_capture.sys, "platform", "win32")

    with (
        patch(
            "playwright.sync_api.sync_playwright",
            return_value=_PolicyProbe(get_policy, default_policy),
        ),
        pytest.raises(_ReachedPlaywright),
    ):
        mt_service.capture_oauth_token(cdp_url=cdp_url)

    assert transitions == [default_policy, original_policy]
    assert current_policy is original_policy


def test_master_token_refuses_account_clobber(tmp_path, monkeypatch):
    _seed_profile_account(monkeypatch, tmp_path, "other@x.com")
    # Mismatch must fail fast — before any oauth_token capture.
    with patch.object(mt_service, "capture_oauth_token") as cap:
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])
    assert result.exit_code == 1
    assert "already belongs to other@x.com" in result.output
    assert not cap.called  # guard fires before sign-in


def test_master_token_refuses_clobber_via_token_owner_only(tmp_path, monkeypatch):
    # No storage_state.json, but a master_token.json owned by a different account.
    from notebooklm.auth import write_master_token
    from notebooklm.paths import get_master_token_path

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    mtp = get_master_token_path()
    mtp.parent.mkdir(parents=True, exist_ok=True)
    write_master_token(mtp, email="other@x.com", master_token="aas_et/M", android_id="abc")
    with patch.object(mt_service, "capture_oauth_token") as cap:
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])
    assert result.exit_code == 1
    assert "already belongs to other@x.com" in result.output
    assert not cap.called


def test_master_token_force_overwrites_other_account(tmp_path, monkeypatch):
    _seed_profile_account(monkeypatch, tmp_path, "other@x.com")
    storage = get_storage_path()
    with patch.object(
        driver,
        "bootstrap_login",
        return_value=MasterTokenLoginSuccess(4, storage),
    ) as boot:
        result = CliRunner().invoke(
            cli,
            ["login", "--master-token", "--account", "e@x.com", "--oauth-token", "T", "--force"],
        )
    assert result.exit_code == 0, result.output
    assert boot.call_args.args[0].force is True
