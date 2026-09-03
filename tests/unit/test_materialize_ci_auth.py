from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "materialize_ci_auth.py"
SPEC = importlib.util.spec_from_file_location("materialize_ci_auth", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auth
SPEC.loader.exec_module(auth)


def _env(tmp_path: Path, token: str = "opaque-token") -> dict[str, str]:
    return {"NOTEBOOKLM_HOME": str(tmp_path), "NOTEBOOKLM_MASTER_TOKEN_JSON": token}


def test_atomic_private_write_and_child_environment(tmp_path, capsys) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        storage = tmp_path / "profiles" / "ci-A-verify-package" / "storage_state.json"
        storage.write_text("{}")
        return subprocess.CompletedProcess(command, 0, "minted", "")

    attempts = auth.materialize(
        account_slot="A", profile="ci-A-verify-package", env=_env(tmp_path), run=run
    )
    assert attempts == 1
    profile = tmp_path / "profiles" / "ci-A-verify-package"
    assert (profile / "master_token.json").read_text() == "opaque-token"
    assert calls[0][0] == [sys.executable, "-m", "notebooklm", "login", "--master-token-refresh"]
    assert "NOTEBOOKLM_MASTER_TOKEN_JSON" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["NOTEBOOKLM_PROFILE"] == "ci-A-verify-package"
    assert calls[0][1]["timeout"] == auth.MINT_TIMEOUT_SECONDS
    if sys.platform != "win32":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        assert stat.S_IMODE((profile / "master_token.json").stat().st_mode) == 0o600
    assert "opaque-token" not in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability contract")
def test_atomic_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch) -> None:
    observed: list[bool] = []
    real_fsync = auth.os.fsync

    def fsync(fd: int) -> None:
        observed.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(auth.os, "fsync", fsync)
    auth.atomic_private_write(tmp_path / "profile" / "master_token.json", "opaque")
    assert observed == [False, True]


def test_three_attempt_backoff_and_early_success(tmp_path) -> None:
    sleeps = []
    attempts = 0

    def run(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            storage = tmp_path / "profiles" / "ci-B-rpc-health-web" / "storage_state.json"
            storage.write_text("{}")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "revoked")

    assert (
        auth.materialize(
            account_slot="B",
            profile="ci-B-rpc-health-web",
            env=_env(tmp_path),
            run=run,
            sleep=sleeps.append,
            randint=lambda _low, _high: 7,
        )
        == 3
    )
    assert sleeps == [22, 37]


def test_failed_mint_is_auth_category_and_scrubs_child_output(tmp_path, capsys) -> None:
    token = "ya29.super-secret-value"

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, token, f"refresh_token={token}")

    with pytest.raises(auth.AuthenticationError):
        auth.materialize(
            account_slot="C",
            profile="ci-C-nightly-web-ubuntu",
            env=_env(tmp_path, token),
            run=run,
            sleep=lambda _: None,
            randint=lambda _a, _b: 0,
        )
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_success_without_storage_is_infrastructure_error(tmp_path) -> None:
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(auth.InfrastructureError, match="without fresh storage"):
        auth.materialize(
            account_slot="A", profile="ci-A-verify-package", env=_env(tmp_path), run=run
        )


def test_subprocess_crash_is_infrastructure_error(tmp_path) -> None:
    def run(command, **kwargs):
        raise OSError("launcher broke")

    with pytest.raises(auth.InfrastructureError, match="launch"):
        auth.materialize(
            account_slot="A", profile="ci-A-verify-package", env=_env(tmp_path), run=run
        )


def test_timed_out_mint_retries_then_reports_auth_failure(tmp_path, capsys) -> None:
    calls = 0
    sleeps = []

    def run(command, **kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(auth.AuthenticationError, match="exit 124"):
        auth.materialize(
            account_slot="A",
            profile="ci-A-verify-package",
            env=_env(tmp_path),
            run=run,
            sleep=sleeps.append,
            randint=lambda _a, _b: 0,
        )
    assert calls == 3
    assert sleeps == [15, 30]
    assert "timed out after 600 seconds" in capsys.readouterr().err


def test_non_os_subprocess_crash_is_infrastructure_error(tmp_path) -> None:
    def run(command, **kwargs):
        raise RuntimeError("subprocess adapter crashed")

    with pytest.raises(auth.InfrastructureError, match="launch"):
        auth.materialize(
            account_slot="A", profile="ci-A-verify-package", env=_env(tmp_path), run=run
        )


def test_child_environment_drops_every_inline_auth_source(tmp_path) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs["env"])
        storage = tmp_path / "profiles" / "ci-A-verify-package" / "storage_state.json"
        storage.write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    env = {
        **_env(tmp_path),
        "NOTEBOOKLM_AUTH_JSON": "legacy-cookie-secret",
        "NOTEBOOKLM_MASTER_TOKEN_JSON_B": "another-pool-secret",
    }
    auth.materialize(account_slot="A", profile="ci-A-verify-package", env=env, run=run)
    assert "NOTEBOOKLM_MASTER_TOKEN_JSON" not in captured
    assert "NOTEBOOKLM_MASTER_TOKEN_JSON_B" not in captured
    assert "NOTEBOOKLM_AUTH_JSON" not in captured


@pytest.mark.parametrize(
    ("slot", "profile", "token"),
    [
        ("D", "ci-D-verify-package", "x"),
        ("A", "default", "x"),
        ("A", "ci-B-verify-package", "x"),
        ("A", "ci-A-arbitrary", "x"),
        ("A", "ci-A-verify-package", ""),
    ],
)
def test_invalid_slot_profile_or_empty_token_is_configuration_error(
    tmp_path, slot: str, profile: str, token: str
) -> None:
    with pytest.raises(auth.ConfigurationError):
        auth.materialize(
            account_slot=slot,
            profile=profile,
            env=_env(tmp_path, token),
            run=lambda *_a, **_k: None,
        )
