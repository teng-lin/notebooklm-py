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
    # at call time, so patch the public facade name it resolves. ``notebooklm.auth``
    # is public, so this string-target ``patch`` is allowed by ADR-0007's lint
    # (which only bars ``monkeypatch.setattr(notebooklm…)`` + private ``patch``).
    with patch("notebooklm.auth.fetch_tokens_with_domains", fetch):
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
    with patch("notebooklm.auth.fetch_tokens_with_domains", fetch):
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
    with patch("notebooklm.auth.fetch_tokens_with_domains", fetch):
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
