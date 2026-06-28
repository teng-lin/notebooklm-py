"""Tests for ``notebooklm._app.auth_check`` — the ``auth check`` diagnostics core.

Covers :func:`run_auth_check` over the probe matrix:

* storage-exists (file vs. inline env-auth),
* JSON-valid (file read errors + env-JSON decode errors via the injected reader),
* cookies-present + SID-cookie lookup,
* the optional ``--test`` token-fetch round-trip (patched at ``notebooklm.auth``),
* the ``AuthCheckResult.all_passed`` rollup (``None`` = not-tested is ignored).

Direct ``_app`` calls only — :class:`AuthCheckPlan` built inline + the
``read_env_auth_json`` reader injected as a plain callable, no Click / CliRunner.
The real :func:`notebooklm.auth.extract_cookies_from_storage` runs against
hand-built ``storage_state`` dicts so the cookie/SID probes exercise production
parsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import notebooklm.auth as auth_module
from notebooklm._app.auth_check import (
    AuthCheckPlan,
    AuthCheckResult,
    run_auth_check,
)


def _plan(
    *,
    storage_path: Path,
    has_env_auth: bool = False,
    has_home_env: bool = False,
    profile: str | None = "default",
    test_fetch: bool = False,
    json_output: bool = False,
) -> AuthCheckPlan:
    return AuthCheckPlan(
        storage_path=storage_path,
        profile=profile,
        has_env_auth=has_env_auth,
        has_home_env=has_home_env,
        auth_source_label="file (storage_state.json)",
        test_fetch=test_fetch,
        json_output=json_output,
    )


#: The cookies ``extract_cookies_from_storage`` requires, or it raises
#: ``ValueError`` (which the auth-check core maps to ``cookies_present=False``).
_REQUIRED_COOKIES = ("SID", "__Secure-1PSIDTS")


def _storage_state(*cookie_names: str, domain: str = ".google.com") -> dict[str, Any]:
    """A Playwright storage_state with the given Google cookies."""
    return {
        "cookies": [
            {"name": name, "value": f"{name}-val", "domain": domain, "path": "/"}
            for name in cookie_names
        ]
    }


def _valid_storage_state(*extra: str, domain: str = ".google.com") -> dict[str, Any]:
    """A storage_state that satisfies ``extract_cookies_from_storage`` (SID + 1PSIDTS)."""
    return _storage_state(*_REQUIRED_COOKIES, *extra, domain=domain)


def _never_read_env() -> str:  # pragma: no cover - guard for non-env paths
    raise AssertionError("read_env_auth_json must not be called when has_env_auth is False")


# ---------------------------------------------------------------------------
# Check 1: storage exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_storage_file_fails_first_check(tmp_path: Path) -> None:
    plan = _plan(storage_path=tmp_path / "missing.json")

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert isinstance(result, AuthCheckResult)
    assert result.checks["storage_exists"] is False
    assert result.checks["json_valid"] is False
    assert result.all_passed is False
    assert "Storage file not found" in result.details["error"]
    # The plan-resolved auth-source label is echoed into details.
    assert result.details["auth_source"] == "file (storage_state.json)"


@pytest.mark.asyncio
async def test_env_auth_treats_storage_as_present(tmp_path: Path) -> None:
    """With env-auth active, storage-exists is True without touching the file."""
    state = _valid_storage_state("HSID")
    plan = _plan(storage_path=tmp_path / "ignored.json", has_env_auth=True)

    result = await run_auth_check(plan, read_env_auth_json=lambda: json.dumps(state))

    assert result.checks["storage_exists"] is True
    assert result.checks["json_valid"] is True
    assert result.checks["cookies_present"] is True
    assert result.checks["sid_cookie"] is True
    assert result.all_passed is True


# ---------------------------------------------------------------------------
# Check 2: JSON valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_on_disk_fails_json_check(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text("{not json", encoding="utf-8")
    plan = _plan(storage_path=storage)

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["storage_exists"] is True
    assert result.checks["json_valid"] is False
    assert result.checks["cookies_present"] is False
    assert "Invalid JSON" in result.details["error"]


@pytest.mark.asyncio
async def test_env_auth_invalid_json_fails_json_check(tmp_path: Path) -> None:
    """An env-supplied payload that fails to decode → json_valid False, no KeyError."""
    plan = _plan(storage_path=tmp_path / "ignored.json", has_env_auth=True)

    result = await run_auth_check(plan, read_env_auth_json=lambda: "{not json")

    assert result.checks["storage_exists"] is True
    assert result.checks["json_valid"] is False
    assert "Invalid JSON" in result.details["error"]


@pytest.mark.asyncio
async def test_storage_unreadable_oserror_maps_to_error(tmp_path: Path) -> None:
    """A directory at the storage path (OSError on read) → structured error."""
    storage_dir = tmp_path / "storage_state.json"
    storage_dir.mkdir()  # reading a directory as text raises OSError
    plan = _plan(storage_path=storage_dir)

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["storage_exists"] is True
    assert result.checks["json_valid"] is False
    assert "Storage unreadable" in result.details["error"]


# ---------------------------------------------------------------------------
# Check 3: cookies present + SID lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_present_with_sid(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps(_valid_storage_state("HSID", "SSID")), encoding="utf-8")
    plan = _plan(storage_path=storage)

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["cookies_present"] is True
    assert result.checks["sid_cookie"] is True
    assert "SID" in result.details["cookies_found"]
    # Google-domain grouping is surfaced for the renderer. ``cookie_domains`` is
    # a list of exact domain keys; assert exact element membership (an explicit
    # ``==`` per element, not a substring ``in``, so CodeQL's
    # incomplete-url-substring-sanitization heuristic does not flag this test).
    assert any(domain == ".google.com" for domain in result.details["cookie_domains"])
    assert result.all_passed is True


@pytest.mark.asyncio
async def test_missing_required_cookies_fails_cookie_check(tmp_path: Path) -> None:
    """A storage without the required cookies → ``extract_cookies_from_storage``
    raises ``ValueError``, which the core maps to cookies_present=False + error."""
    storage = tmp_path / "storage_state.json"
    # Only HSID/SSID present — missing the required SID + __Secure-1PSIDTS pair.
    storage.write_text(json.dumps(_storage_state("HSID", "SSID")), encoding="utf-8")
    plan = _plan(storage_path=storage)

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["json_valid"] is True
    assert result.checks["cookies_present"] is False
    assert result.checks["sid_cookie"] is False
    assert result.details["error"]
    assert result.all_passed is False


# ---------------------------------------------------------------------------
# Check 4: optional token-fetch round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_fetch_success(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps(_valid_storage_state("HSID")), encoding="utf-8")
    plan = _plan(storage_path=storage, test_fetch=True)

    fetch = AsyncMock(return_value=("csrf-token-value", "session-id-value"))
    # ``run_auth_check`` does ``from ..auth import fetch_tokens_with_domains``
    # at call time, so patch the public facade name it resolves.
    with patch.object(auth_module, "fetch_tokens_with_domains", fetch):
        result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["token_fetch"] is True
    assert result.details["csrf_length"] == len("csrf-token-value")
    assert result.details["session_id_length"] == len("session-id-value")
    assert result.all_passed is True
    # File-based auth → the storage path is forwarded to the fetch.
    fetch.assert_awaited_once_with(storage, "default")


@pytest.mark.asyncio
async def test_token_fetch_failure_sets_false(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps(_valid_storage_state()), encoding="utf-8")
    plan = _plan(storage_path=storage, test_fetch=True)

    fetch = AsyncMock(side_effect=RuntimeError("network down"))
    with patch.object(auth_module, "fetch_tokens_with_domains", fetch):
        result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["token_fetch"] is False
    assert "Token fetch failed" in result.details["error"]
    assert "network down" in result.details["error"]
    assert result.all_passed is False


@pytest.mark.asyncio
async def test_token_fetch_env_auth_passes_none_path(tmp_path: Path) -> None:
    """Env-auth token-fetch forwards a ``None`` path (the env JSON is the source)."""
    plan = _plan(
        storage_path=tmp_path / "ignored.json",
        has_env_auth=True,
        profile="work",
        test_fetch=True,
    )
    fetch = AsyncMock(return_value=("c", "s"))
    with patch.object(auth_module, "fetch_tokens_with_domains", fetch):
        result = await run_auth_check(
            plan, read_env_auth_json=lambda: json.dumps(_valid_storage_state())
        )

    assert result.checks["token_fetch"] is True
    fetch.assert_awaited_once_with(None, "work")


@pytest.mark.asyncio
async def test_token_fetch_not_run_when_test_fetch_false(tmp_path: Path) -> None:
    """Without ``--test`` the token_fetch check stays ``None`` (not tested)."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps(_valid_storage_state()), encoding="utf-8")
    plan = _plan(storage_path=storage, test_fetch=False)

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.checks["token_fetch"] is None
    # all_passed ignores the not-tested (None) token_fetch.
    assert result.all_passed is True


# ---------------------------------------------------------------------------
# Identity + location facts (issue #1640)
# ---------------------------------------------------------------------------


def _storage_with_account(
    *cookie_names: str, email: str | None = None, authuser: int = 0
) -> dict[str, Any]:
    """A valid storage_state carrying the in-band account namespace."""
    state = _valid_storage_state(*cookie_names)
    if email is not None:
        state["notebooklm"] = {"account": {"email": email, "authuser": authuser}}
    return state


@pytest.mark.asyncio
async def test_identity_fields_populated_from_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(_storage_with_account("APISID", "SAPISID", email="you@gmail.com", authuser=2)),
        encoding="utf-8",
    )
    plan = _plan(storage_path=storage, profile="github")

    result = await run_auth_check(plan, read_env_auth_json=_never_read_env)

    assert result.details["account"] == {"email": "you@gmail.com", "authuser": 2}
    assert result.details["profile"] == "github"
    assert result.details["storage_path"] == str(storage)
    assert result.details["psidts"]["present"] is True
    # No sibling master_token.json → reported absent with the path it looked for.
    assert result.details["master_token"]["present"] is False
    assert result.details["master_token"]["path"] == str(storage.with_name("master_token.json"))


@pytest.mark.asyncio
async def test_psidts_expiry_surfaced(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "v",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": 1_800_000_000,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = await run_auth_check(_plan(storage_path=storage), read_env_auth_json=_never_read_env)

    psidts = result.details["psidts"]
    assert psidts["present"] is True
    # Epoch 1_800_000_000 → 2027-01-15T08:00:00+00:00 (UTC ISO).
    assert psidts["expires_at"].startswith("2027-01-15T08:00:00")


@pytest.mark.asyncio
async def test_psidts_out_of_range_expiry_degrades_to_none(tmp_path: Path) -> None:
    """A corrupt/out-of-range ``expires`` must not abort the check (auth check
    has no error envelope); the field degrades to present + expires_at=None."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "v",
                        "domain": ".google.com",
                        "path": "/",
                        "expires": 1e20,  # OverflowError from datetime.fromtimestamp
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = await run_auth_check(_plan(storage_path=storage), read_env_auth_json=_never_read_env)

    assert result.details["psidts"] == {"present": True, "expires_at": None}


@pytest.mark.asyncio
async def test_master_token_present_and_account_read(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps(_valid_storage_state("APISID", "SAPISID")), encoding="utf-8")
    auth_module.write_master_token(
        storage.with_name("master_token.json"),
        email="owner@gmail.com",
        master_token="aas_et/secret",
        android_id="0123456789abcdef",
    )
    result = await run_auth_check(_plan(storage_path=storage), read_env_auth_json=_never_read_env)

    mt = result.details["master_token"]
    assert mt["present"] is True
    assert mt["account"] == "owner@gmail.com"
    assert mt["path"] == str(storage.with_name("master_token.json"))


@pytest.mark.asyncio
async def test_missing_psidts_with_master_token_uses_corrected_hint(tmp_path: Path) -> None:
    """A master-token profile missing PSIDTS at rest gets master-token guidance,
    not the (wrong) browser-extraction / App-Bound Encryption hint."""
    storage = tmp_path / "storage_state.json"
    # SID + secondary binding present, but no __Secure-1PSIDTS (the recoverable,
    # self-healing case for a master-token profile).
    storage.write_text(json.dumps(_storage_state("SID", "APISID", "SAPISID")), encoding="utf-8")
    auth_module.write_master_token(
        storage.with_name("master_token.json"),
        email="owner@gmail.com",
        master_token="aas_et/secret",
        android_id="0123456789abcdef",
    )
    result = await run_auth_check(_plan(storage_path=storage), read_env_auth_json=_never_read_env)

    assert result.checks["cookies_present"] is False
    assert "master_token.json is present" in result.details["error"]
    assert "App-Bound Encryption" not in result.details["error"]
    assert result.details["master_token"]["present"] is True


@pytest.mark.asyncio
async def test_env_auth_reads_account_from_inline_json(tmp_path: Path) -> None:
    plan = _plan(
        storage_path=tmp_path / "ignored.json",
        has_env_auth=True,
        profile="work",
    )
    inline = json.dumps(_storage_with_account(email="ci@gmail.com", authuser=1))
    result = await run_auth_check(plan, read_env_auth_json=lambda: inline)

    assert result.details["account"] == {"email": "ci@gmail.com", "authuser": 1}
    # Env-auth has no profile directory → master-token is N/A.
    assert result.details["master_token"] == {"present": False, "path": None, "account": None}


@pytest.mark.asyncio
async def test_env_auth_account_normalizes_whitespace_and_rejects_bool(tmp_path: Path) -> None:
    """Env-auth account mirrors the file path: whitespace-only email → None, and a
    bool authuser (``bool`` is an ``int`` subclass) falls back to 0."""
    plan = _plan(storage_path=tmp_path / "ignored.json", has_env_auth=True)
    state = _valid_storage_state()
    state["notebooklm"] = {"account": {"email": "   ", "authuser": True}}
    result = await run_auth_check(plan, read_env_auth_json=lambda: json.dumps(state))

    assert result.details["account"] == {"email": None, "authuser": 0}


@pytest.mark.asyncio
async def test_missing_sid_and_psidts_keeps_generic_hint(tmp_path: Path) -> None:
    """When BOTH SID and PSIDTS are missing the session is unrecoverable, so the
    master-token hint must NOT fire even with a sibling master_token.json."""
    storage = tmp_path / "storage_state.json"
    # Neither SID nor __Secure-1PSIDTS — only the secondary binding pair.
    storage.write_text(json.dumps(_storage_state("APISID", "SAPISID")), encoding="utf-8")
    auth_module.write_master_token(
        storage.with_name("master_token.json"),
        email="owner@gmail.com",
        master_token="aas_et/secret",
        android_id="0123456789abcdef",
    )
    result = await run_auth_check(_plan(storage_path=storage), read_env_auth_json=_never_read_env)

    assert result.checks["cookies_present"] is False
    assert "master_token.json is present" not in result.details["error"]


# ---------------------------------------------------------------------------
# all_passed rollup
# ---------------------------------------------------------------------------


def test_all_passed_ignores_none_but_fails_on_false() -> None:
    plan = _plan(storage_path=Path("/x"))
    passing = AuthCheckResult(
        plan=plan,
        checks={
            "storage_exists": True,
            "json_valid": True,
            "cookies_present": True,
            "sid_cookie": True,
            "token_fetch": None,
        },
    )
    assert passing.all_passed is True

    failing = AuthCheckResult(
        plan=plan,
        checks={"storage_exists": True, "sid_cookie": False, "token_fetch": None},
    )
    assert failing.all_passed is False
