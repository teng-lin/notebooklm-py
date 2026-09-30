"""Web multi-profile admission: session identity, refusals, and diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from notebooklm._app import web_profiles
from notebooklm._app.web_profiles import (
    WebProfileSet,
    WebProfileUnavailable,
    WebSessionConflict,
    refuse_web_multi_profile_environment,
    web_session_keys,
)


def _cookie(name: str, value: str, domain: str = ".google.com") -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def _write_state(path: Path, cookies: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def _session(path: Path, psid: str, *, sid: str | None = None) -> Path:
    # Distinct sessions carry distinct SIDs; SID identifies a session, not an account.
    sid = sid if sid is not None else f"sid-{psid}"
    return _write_state(
        path,
        [
            _cookie("SID", sid),
            _cookie("__Secure-1PSID", psid),
            _cookie("__Secure-1PSIDTS", f"ts-{psid}"),
        ],
    )


def _master_token(storage_path: Path) -> None:
    token = storage_path.with_name("master_token.json")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        json.dumps(
            {
                "version": 1,
                "email": "same@example.com",
                "android_id": "1234567890123456",
                "master_token": "aas_et/fake-master-token",
            }
        ),
        encoding="utf-8",
    )
    token.chmod(0o600)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL", raising=False)
    return monkeypatch


@pytest.fixture
def profile_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        name: (tmp_path / "profiles" / name / "storage_state.json").resolve()
        for name in ("work", "personal", "other")
    }


# --- web_session_keys ---------------------------------------------------------


def test_session_keys_match_when_any_session_cookie_matches(tmp_path: Path) -> None:
    first = _session(tmp_path / "a" / "storage_state.json", "psid-value", sid="sid-a")
    second = _write_state(
        tmp_path / "b" / "storage_state.json",
        [
            _cookie("SID", "a-different-sid"),
            _cookie("HSID", "unrelated"),
            _cookie("__Secure-1PSID", "psid-value"),
        ],
    )
    first_keys, second_keys = web_session_keys(first), web_session_keys(second)
    assert first_keys and second_keys
    assert first_keys & second_keys


def test_session_keys_catch_a_copy_that_kept_only_sid(tmp_path: Path) -> None:
    both = _session(tmp_path / "a" / "storage_state.json", "psid-a", sid="shared-sid")
    sid_only = _write_state(tmp_path / "b" / "storage_state.json", [_cookie("SID", "shared-sid")])
    both_keys, sid_keys = web_session_keys(both), web_session_keys(sid_only)
    assert both_keys and sid_keys
    assert both_keys & sid_keys


def test_session_key_distinguishes_values_and_never_contains_them(tmp_path: Path) -> None:
    first = _session(tmp_path / "a" / "storage_state.json", "psid-secret-one")
    second = _session(tmp_path / "b" / "storage_state.json", "psid-secret-two")
    first_keys = web_session_keys(first)
    second_keys = web_session_keys(second)
    assert first_keys and second_keys
    assert not first_keys & second_keys
    for keys, value in ((first_keys, "psid-secret-one"), (second_keys, "psid-secret-two")):
        for key in keys:
            assert value not in key
            assert "psid" not in key and "sid-" not in key


def test_session_key_prefers_google_com_row(tmp_path: Path) -> None:
    path = _write_state(
        tmp_path / "storage_state.json",
        [
            _cookie("__Secure-1PSID", "regional", domain=".google.de"),
            _cookie("__Secure-1PSID", "base"),
        ],
    )
    base_only = _write_state(
        tmp_path / "base" / "storage_state.json", [_cookie("__Secure-1PSID", "base")]
    )
    assert web_session_keys(path) == web_session_keys(base_only)


def test_session_key_missing_file_is_none(tmp_path: Path) -> None:
    assert web_session_keys(tmp_path / "absent" / "storage_state.json") is None


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "[]",
        json.dumps({"cookies": [_cookie("HSID", "not-a-session-cookie")]}),
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=["malformed", "not-object", "missing-session-cookie", "deeply-nested"],
)
def test_session_key_unusable_storage_is_unavailable(tmp_path: Path, content: str) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(WebProfileUnavailable) as info:
        web_session_keys(path)
    # No chained parser/decoder error: it could carry the file's cookie bytes.
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_session_key_undecodable_file_chains_no_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(b'{"cookies": [{"name": "SID", "value": "secret-\xff"}]}')
    with pytest.raises(WebProfileUnavailable) as info:
        web_session_keys(path)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_session_key_hashes_unencodable_cookie_value(tmp_path: Path) -> None:
    # A lone surrogate survives JSON decoding; hashing must not raise on it.
    path = tmp_path / "storage_state.json"
    path.write_text(
        '{"cookies": [{"name": "__Secure-1PSID", "value": "\\ud800", '
        '"domain": ".google.com", "path": "/"}]}',
        encoding="utf-8",
    )
    assert web_session_keys(path) is not None


def test_session_key_falls_back_to_sid(tmp_path: Path) -> None:
    only_sid = _write_state(tmp_path / "a" / "storage_state.json", [_cookie("SID", "sid-a")])
    copy = _write_state(tmp_path / "b" / "storage_state.json", [_cookie("SID", "sid-a")])
    other = _write_state(tmp_path / "c" / "storage_state.json", [_cookie("SID", "sid-c")])
    assert web_session_keys(only_sid) is not None
    assert web_session_keys(only_sid) == web_session_keys(copy)
    assert web_session_keys(only_sid) != web_session_keys(other)


def test_session_key_does_not_require_psidts(tmp_path: Path) -> None:
    path = _write_state(tmp_path / "storage_state.json", [_cookie("__Secure-1PSID", "psid")])
    assert web_session_keys(path) is not None


# --- environment refusals ----------------------------------------------------


def test_env_refusal_inline_auth_presence(clean_env: pytest.MonkeyPatch) -> None:
    refuse_web_multi_profile_environment()
    clean_env.setenv("NOTEBOOKLM_AUTH_JSON", "")
    with pytest.raises(ValueError, match="refuses NOTEBOOKLM_AUTH_JSON"):
        refuse_web_multi_profile_environment()


def test_env_refusal_shared_cdp_browser(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL", "   ")
    refuse_web_multi_profile_environment()
    clean_env.setenv("NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL", "http://127.0.0.1:9222")
    with pytest.raises(ValueError, match="refuses NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL"):
        refuse_web_multi_profile_environment()


# --- admission ---------------------------------------------------------------


async def test_admit_classifies_ready_bootstrap_and_unavailable(
    profile_paths: dict[str, Path],
) -> None:
    _session(profile_paths["work"], "psid-work")
    _master_token(profile_paths["personal"])
    profiles = WebProfileSet(profile_paths, keepalive=None)
    assert profiles.admit("work") == "ready"
    assert profiles.admit("personal") == "bootstrap"
    with pytest.raises(WebProfileUnavailable) as caught:
        profiles.admit("other")
    assert not isinstance(caught.value, WebSessionConflict)


async def test_admit_refuses_unreadable_master_token_without_storage(
    profile_paths: dict[str, Path],
) -> None:
    token = profile_paths["work"].with_name("master_token.json")
    token.parent.mkdir(parents=True)
    token.write_text("not JSON", encoding="utf-8")
    with pytest.raises(WebProfileUnavailable):
        WebProfileSet(profile_paths, keepalive=None).admit("work")


async def test_admit_refuses_copied_session_naming_profiles_only(
    profile_paths: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    _session(profile_paths["work"], "copied-psid-secret")
    _session(profile_paths["personal"], "copied-psid-secret")
    _session(profile_paths["other"], "distinct-psid-secret")
    profiles = WebProfileSet(profile_paths, keepalive=None)
    keys = web_session_keys(profile_paths["work"])
    assert keys
    with (
        caplog.at_level(logging.WARNING, logger=web_profiles.__name__),
        pytest.raises(WebSessionConflict) as caught,
    ):
        profiles.admit("personal")
    assert str(caught.value) == (
        "Web profile 'personal' shares a Web session with configured profile(s) 'work' "
        "(copied storage_state.json); refusing it. Log in separately "
        "(notebooklm -p personal login) or delete the copy and keep master_token.json "
        "to mint a fresh session."
    )
    assert str(caught.value) in caplog.text
    for secret in ("copied-psid-secret", *keys):
        assert secret not in str(caught.value)
        assert secret not in caplog.text
    assert profiles.admit("other") == "ready"


async def test_admit_refuses_copy_that_kept_only_sid(profile_paths: dict[str, Path]) -> None:
    _session(profile_paths["work"], "psid-work", sid="shared-sid")
    _write_state(profile_paths["personal"], [_cookie("SID", "shared-sid")])
    profiles = WebProfileSet(profile_paths, keepalive=None)
    for name in ("work", "personal"):
        with pytest.raises(WebSessionConflict):
            profiles.admit(name)


async def test_admit_ignores_broken_sibling(profile_paths: dict[str, Path]) -> None:
    _session(profile_paths["work"], "psid-work")
    profile_paths["personal"].parent.mkdir(parents=True)
    profile_paths["personal"].write_text("not JSON", encoding="utf-8")
    assert WebProfileSet(profile_paths, keepalive=None).admit("work") == "ready"


@pytest.mark.parametrize(
    "content",
    [
        "[" * 100_000 + "]" * 100_000,
        '{"cookies": [{"name": "__Secure-1PSID", "value": "\\ud800", '
        '"domain": ".google.com", "path": "/"}]}',
    ],
    ids=["deeply-nested", "unencodable-value"],
)
async def test_hostile_sibling_does_not_block_admission_or_health(
    profile_paths: dict[str, Path], content: str
) -> None:
    _session(profile_paths["work"], "psid-work")
    profile_paths["personal"].parent.mkdir(parents=True)
    profile_paths["personal"].write_text(content, encoding="utf-8")
    profiles = WebProfileSet(profile_paths, keepalive=None)
    assert profiles.admit("work") == "ready"
    assert (await profiles.health("work")).session_conflict is False


# --- factory -----------------------------------------------------------------


def _fake_open(opened: list[tuple[Path, str, float | None]], *, bound: Path | None = None) -> Any:
    @asynccontextmanager
    async def open_client(path: Path, profile: str, keepalive: float | None) -> Any:
        opened.append((path, profile, keepalive))
        yield SimpleNamespace(auth=SimpleNamespace(storage_path=bound or path))

    return open_client


async def test_factory_bootstraps_then_opens_explicit_path(
    profile_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _master_token(profile_paths["work"])
    opened: list[tuple[Path, str, float | None]] = []
    bootstrapped: list[Path] = []

    async def bootstrap(path: Path) -> bool:
        bootstrapped.append(path)
        _session(path, "minted-psid")
        return True

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", bootstrap)
    monkeypatch.setattr(web_profiles, "_open_web_client", _fake_open(opened))
    profiles = WebProfileSet(profile_paths, keepalive=600.0)
    async with profiles.factory("work") as client:
        assert client.auth.storage_path == profile_paths["work"]
    assert bootstrapped == [profile_paths["work"]]
    assert opened == [(profile_paths["work"], "work", 600.0)]


async def test_factory_refuses_bootstrap_that_creates_no_session(
    profile_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _master_token(profile_paths["work"])
    opened: list[tuple[Path, str, float | None]] = []

    async def bootstrap(path: Path) -> bool:
        return False

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", bootstrap)
    monkeypatch.setattr(web_profiles, "_open_web_client", _fake_open(opened))
    with pytest.raises(WebProfileUnavailable, match="bootstrap"):
        async with WebProfileSet(profile_paths, keepalive=None).factory("work"):
            pytest.fail("no client may be yielded")
    assert opened == []


async def test_factory_fails_closed_on_foreign_storage_binding(
    profile_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _session(profile_paths["work"], "psid-work")
    opened: list[tuple[Path, str, float | None]] = []
    monkeypatch.setattr(
        web_profiles, "_open_web_client", _fake_open(opened, bound=tmp_path / "elsewhere.json")
    )
    with pytest.raises(WebProfileUnavailable, match="other storage"):
        async with WebProfileSet(profile_paths, keepalive=None).factory("work"):
            pytest.fail("no client may be yielded")
    assert len(opened) == 1


async def test_factory_repeats_environment_refusal(
    profile_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(profile_paths["work"], "psid-work")
    opened: list[tuple[Path, str, float | None]] = []
    monkeypatch.setattr(web_profiles, "_open_web_client", _fake_open(opened))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    with pytest.raises(ValueError, match="NOTEBOOKLM_AUTH_JSON"):
        async with WebProfileSet(profile_paths, keepalive=None).factory("work"):
            pytest.fail("no client may be yielded")
    assert opened == []


async def test_open_turn_is_exclusive_and_loop_bound(profile_paths: dict[str, Path]) -> None:
    profiles = WebProfileSet(profile_paths, keepalive=None)
    active = 0
    peak = 0

    async def hold() -> None:
        nonlocal active, peak
        async with profiles.open_turn():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(hold() for _ in range(3)))
    assert peak == 1

    async def foreign() -> None:
        async with profiles.open_turn():
            pass  # pragma: no cover - rejected before entering

    with pytest.raises(RuntimeError, match="owning event loop"):
        await asyncio.to_thread(asyncio.run, foreign())


# --- diagnostics -------------------------------------------------------------


async def test_health_is_file_only_and_reports_conflict(profile_paths: dict[str, Path]) -> None:
    _session(profile_paths["work"], "copied")
    _session(profile_paths["personal"], "copied")
    _master_token(profile_paths["other"])
    profiles = WebProfileSet(profile_paths, keepalive=None)
    work = await profiles.health("work")
    assert work.storage_exists and work.json_valid
    assert work.session_conflict is True
    assert work.master_token_present is False
    other = await profiles.health("other")
    assert other.storage_exists is False
    assert other.master_token_present is True
    assert other.session_conflict is False
    assert other.local_checks_passed is False
