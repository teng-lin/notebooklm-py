"""Tests for inline ``__Secure-1PSIDTS`` recovery (issue #865).

Covers :mod:`notebooklm._auth.psidts_recovery` and its integration into
:func:`notebooklm.auth.load_auth_from_storage`. The recovery breaks a closed
loop in the cold-start preflight: when ``storage_state.json`` lacks PSIDTS but
carries ``SID`` + a valid secondary binding, the preflight rejects before the
keepalive's ``RotateCookies`` POST can heal the state. This module's tests pin
the precondition gate, the throttle, the persistence, and the load-path
integration so the loop stays broken.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth as auth_module
from notebooklm._auth import psidts_recovery

_ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


# Cookies that, together, form the minimum acceptable recovery precondition:
# SID + secondary binding (APISID + SAPISID), with PSIDTS intentionally absent.
_RECOVERABLE_COOKIES: list[dict] = [
    {"name": "SID", "value": "test_sid", "domain": ".google.com", "path": "/"},
    {"name": "APISID", "value": "test_apisid", "domain": ".google.com", "path": "/"},
    {"name": "SAPISID", "value": "test_sapisid", "domain": ".google.com", "path": "/"},
    {"name": "HSID", "value": "test_hsid", "domain": ".google.com", "path": "/"},
    {"name": "SSID", "value": "test_ssid", "domain": ".google.com", "path": "/"},
]


def _write_storage(path: Path, cookies: list[dict]) -> None:
    path.write_text(json.dumps({"cookies": cookies, "origins": []}))


def _make_psidts_response(status_code: int = 200, *, include_psidts: bool = True):
    """Build a response shape matching what Google's RotateCookies returns."""
    headers: list[tuple[str, str]] = []
    if include_psidts:
        # Match Google's real Set-Cookie shape — Domain=.google.com,
        # Path=/, Secure, HttpOnly. The httpx jar parses these directly.
        headers.append(
            (
                "Set-Cookie",
                "__Secure-1PSIDTS=fresh_psidts_value; "
                "Domain=.google.com; Path=/; Secure; HttpOnly; SameSite=Lax",
            )
        )
        headers.append(
            (
                "Set-Cookie",
                "__Secure-3PSIDTS=fresh_3psidts_value; "
                "Domain=.google.com; Path=/; Secure; HttpOnly; SameSite=None",
            )
        )
    return {
        "status_code": status_code,
        "headers": headers,
        "content": b'["identity.hfcr",600]',
    }


class TestRecoveryPreconditions:
    """The precondition gate must short-circuit before the POST fires."""

    @pytest.mark.no_default_keepalive_mock
    def test_no_sid_returns_false_without_post(self, tmp_path, httpx_mock: HTTPXMock):
        """No SID → session is truly dead → recovery declines."""
        cookies = [c for c in _RECOVERABLE_COOKIES if c["name"] != "SID"]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_psidts_already_present_returns_false_without_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Nothing to recover when PSIDTS is already there."""
        cookies = _RECOVERABLE_COOKIES + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "already_present",
                "domain": ".google.com",
                "path": "/",
            }
        ]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_missing_secondary_binding_returns_false_without_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """No OSID, no APISID+SAPISID — Google will reject RotateCookies."""
        cookies = [c for c in _RECOVERABLE_COOKIES if c["name"] not in {"APISID", "SAPISID"}]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_osid_alone_satisfies_secondary_binding(self, tmp_path, httpx_mock: HTTPXMock):
        """OSID is the alternative secondary binding (per ``_has_valid_secondary_binding``)."""
        cookies = [
            {"name": "SID", "value": "test_sid", "domain": ".google.com", "path": "/"},
            {"name": "OSID", "value": "test_osid", "domain": ".google.com", "path": "/"},
        ]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

    def test_missing_file_returns_false(self, tmp_path):
        """A storage path that doesn't exist cannot be recovered."""
        storage_path = tmp_path / "does_not_exist.json"
        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_throttle_claim_failure_skips_post(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """A claimed rotation slot prevents the POST from firing."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        # Force ``_try_claim_rotation`` to deny the claim, simulating a sibling
        # caller having just claimed the slot. Patch the local alias on
        # ``psidts_recovery`` (ADR-007 object-target form) — the recovery path
        # resolves the symbol via this module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_try_claim_rotation", lambda _path: False)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []


class TestRecoveryHappyPath:
    """End-to-end recovery: POST + persist + reload."""

    @pytest.mark.no_default_keepalive_mock
    def test_persists_psidts_to_storage_state(self, tmp_path, httpx_mock: HTTPXMock):
        """The rotated PSIDTS must land in storage_state.json on disk."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

        saved = json.loads(storage_path.read_text())
        names = {c["name"] for c in saved["cookies"]}
        assert "__Secure-1PSIDTS" in names
        psidts = next(c for c in saved["cookies"] if c["name"] == "__Secure-1PSIDTS")
        assert psidts["value"] == "fresh_psidts_value"

    @pytest.mark.no_default_keepalive_mock
    def test_post_uses_existing_cookies_as_request_jar(self, tmp_path, httpx_mock: HTTPXMock):
        """The recovery POST must carry the existing auth cookies so Google honours it."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_recovery._recover_psidts_inline(storage_path)

        rotate_requests = [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]
        assert len(rotate_requests) == 1
        cookie_header = rotate_requests[0].headers.get("cookie", "")
        # Sanity-check the request carries SID + the secondary binding.
        assert "SID=test_sid" in cookie_header
        assert "APISID=test_apisid" in cookie_header
        assert "SAPISID=test_sapisid" in cookie_header

    @pytest.mark.no_default_keepalive_mock
    def test_preserves_other_cookies_in_storage(self, tmp_path, httpx_mock: HTTPXMock):
        """Cookies that weren't rotated must survive the recovery write."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_recovery._recover_psidts_inline(storage_path)

        saved = json.loads(storage_path.read_text())
        names = {c["name"] for c in saved["cookies"]}
        for original in _RECOVERABLE_COOKIES:
            assert original["name"] in names


class TestRecoveryFailureModes:
    """Network and protocol-level failures must not raise — return False."""

    @pytest.mark.no_default_keepalive_mock
    def test_4xx_response_returns_false(self, tmp_path, httpx_mock: HTTPXMock):
        """A 401/403/etc. from RotateCookies → no rotation → return False."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=401)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # PSIDTS must NOT have been written.
        saved = json.loads(storage_path.read_text())
        assert "__Secure-1PSIDTS" not in {c["name"] for c in saved["cookies"]}

    @pytest.mark.no_default_keepalive_mock
    def test_5xx_response_returns_false(self, tmp_path, httpx_mock: HTTPXMock):
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=503)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_200_without_psidts_in_response_returns_false(self, tmp_path, httpx_mock: HTTPXMock):
        """Google may 200 without minting PSIDTS — must not claim success."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(
            url=_ROTATE_URL_RE,
            **_make_psidts_response(include_psidts=False),
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        saved = json.loads(storage_path.read_text())
        assert "__Secure-1PSIDTS" not in {c["name"] for c in saved["cookies"]}

    @pytest.mark.no_default_keepalive_mock
    def test_network_error_returns_false(self, tmp_path, httpx_mock: HTTPXMock):
        """A connection error during the POST → False, not a raise."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_exception(httpx.ConnectError("simulated network failure"))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False


class TestLoadAuthFromStorageIntegration:
    """The recovery must be wired into :func:`load_auth_from_storage`."""

    @pytest.mark.no_default_keepalive_mock
    def test_recovers_psidts_before_returning_cookies(self, tmp_path, httpx_mock: HTTPXMock):
        """The first call recovers + the function returns the validated dict."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        cookies = auth_module.load_auth_from_storage(storage_path)

        assert cookies["__Secure-1PSIDTS"] == "fresh_psidts_value"
        assert cookies["SID"] == "test_sid"

    @pytest.mark.no_default_keepalive_mock
    def test_propagates_value_error_when_recovery_declines(self, tmp_path, httpx_mock: HTTPXMock):
        """Preconditions failing → original ValueError stands."""
        cookies_no_binding = [
            c for c in _RECOVERABLE_COOKIES if c["name"] not in {"APISID", "SAPISID"}
        ]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies_no_binding)

        with pytest.raises(ValueError, match="__Secure-1PSIDTS"):
            auth_module.load_auth_from_storage(storage_path)

    @pytest.mark.no_default_keepalive_mock
    def test_propagates_value_error_when_recovery_post_fails(self, tmp_path, httpx_mock: HTTPXMock):
        """Recovery attempts but fails at the POST → original ValueError."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=500)

        with pytest.raises(ValueError, match="__Secure-1PSIDTS"):
            auth_module.load_auth_from_storage(storage_path)

    @pytest.mark.no_default_keepalive_mock
    def test_does_not_attempt_recovery_for_env_var_auth(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Env-var auth (``path=None`` + ``NOTEBOOKLM_AUTH_JSON``) is out-of-scope.

        The recovery requires a writeable backing store; for env-var auth we
        let the original ValueError stand. See module docstring of
        :mod:`notebooklm._auth.psidts_recovery` for the tracked future-work
        item.
        """
        storage_state = {"cookies": _RECOVERABLE_COOKIES}
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        with pytest.raises(ValueError, match="__Secure-1PSIDTS"):
            auth_module.load_auth_from_storage(None)

        # Crucially: no RotateCookies POST must have fired for env-var auth.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_recovers_when_path_is_none_with_no_env_var(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """``load_auth_from_storage(None)`` with no env-var resolves to the default
        profile file and STILL triggers recovery (Codex Critical: issue #865).

        Before the fix, ``path is None`` was treated as a recovery skip-condition,
        but ``_load_storage_state(None)`` falls through to ``get_storage_path()``
        when ``NOTEBOOKLM_AUTH_JSON`` is unset — that's the most common library
        usage. The recovery must resolve the same default.
        """
        # Point ``get_storage_path()`` at a tmp file populated with the
        # recoverable-but-PSIDTS-missing state. Patch the SOURCE module so
        # both ``_load_storage_state`` (imports at module level into
        # ``_auth.cookies``) and the recovery's ``_resolve_recovery_path``
        # (lazy-imports from ``..paths``) see the same override.
        default_path = tmp_path / "default_storage_state.json"
        _write_storage(default_path, _RECOVERABLE_COOKIES)
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setattr("notebooklm.paths.get_storage_path", lambda: default_path)
        monkeypatch.setattr(
            "notebooklm._auth.cookies.get_storage_path",
            lambda: default_path,
        )
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        cookies = auth_module.load_auth_from_storage(None)

        assert cookies["__Secure-1PSIDTS"] == "fresh_psidts_value"


class TestBuildHttpxCookiesFromStorageIntegration:
    """Recovery must also heal the programmatic loader (``AuthTokens.from_storage``)."""

    @pytest.mark.no_default_keepalive_mock
    def test_recovers_through_build_httpx_cookies_from_storage(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """``AuthTokens.from_storage`` / ``NotebookLMClient.from_storage`` route
        through ``build_httpx_cookies_from_storage``, NOT ``load_auth_from_storage``.
        The recovery hook must heal that path too (Codex Important: issue #865).
        """
        from notebooklm._auth.cookies import build_httpx_cookies_from_storage

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        jar = build_httpx_cookies_from_storage(storage_path)

        cookie_names = {c.name for c in jar.jar}
        assert "__Secure-1PSIDTS" in cookie_names
        # The file on disk must also have been healed so subsequent loaders see it.
        saved = json.loads(storage_path.read_text())
        assert "__Secure-1PSIDTS" in {c["name"] for c in saved["cookies"]}

    @pytest.mark.no_default_keepalive_mock
    def test_build_httpx_cookies_re_raises_when_recovery_declines(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Recovery preconditions failing → original ValueError propagates."""
        from notebooklm._auth.cookies import build_httpx_cookies_from_storage

        # Strip the secondary binding so the recovery declines.
        cookies = [c for c in _RECOVERABLE_COOKIES if c["name"] not in {"APISID", "SAPISID"}]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)

        with pytest.raises(ValueError, match="__Secure-1PSIDTS"):
            build_httpx_cookies_from_storage(storage_path)


class TestEdgeCases:
    """Hardening tests for the precondition gate and post-POST persistence."""

    @pytest.mark.no_default_keepalive_mock
    def test_malformed_storage_cookies_non_list(self, tmp_path, httpx_mock: HTTPXMock):
        """``"cookies"`` key not a list → return False without firing POST."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": "not-a-list"}))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_save_returning_false_propagates_as_failure(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """``save_cookies_to_storage`` returns False on persist failure (not raises).

        Recovery must capture the return value — otherwise it logs a misleading
        INFO ``Recovered ... and persisted`` while on-disk state is still broken
        (Claude Important + Codex Important: issue #865).
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        # Force the persist step to return False (CAS rejection / I/O error / etc.).
        monkeypatch.setattr(
            "notebooklm._auth.psidts_recovery._auth_storage.save_cookies_to_storage",
            lambda *args, **kwargs: False,
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_save_raising_propagates_as_failure(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """Unexpected exception from ``save_cookies_to_storage`` → False, not propagated."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        def raise_oserror(*_args, **_kwargs):
            raise OSError("simulated disk-full")

        monkeypatch.setattr(
            "notebooklm._auth.psidts_recovery._auth_storage.save_cookies_to_storage",
            raise_oserror,
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_cross_process_flock_held_skips_post(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A held rotation flock (simulating another CLI process) → skip the POST.

        Mirrors ``_poke_session``'s outer cross-process guard (Claude Important +
        Codex Important: issue #865). Before the fix, two concurrent ``notebooklm``
        invocations could each fire ``RotateCookies``.
        """
        import contextlib

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        @contextlib.contextmanager
        def held_lock(_lock_path):
            # Simulate another process holding the lock — acquire=False.
            yield False

        # Patch the local alias on ``psidts_recovery`` (ADR-007 object-target
        # form) — the recovery path resolves ``_file_lock_try_exclusive`` via
        # this module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_file_lock_try_exclusive", held_lock)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_flock_held_returns_true_when_file_already_healed(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Flock held + on-disk file ALREADY has PSIDTS → return True without POST.

        Closes the TOCTOU window flagged by claude bot (Minor Design Gap): when
        we lose the flock race, the holder may have already finished writing.
        The cheap re-read avoids the caller's preflight re-raising stale.
        """
        import contextlib

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        @contextlib.contextmanager
        def held_lock(_lock_path):
            yield False

        # Two-phase view: precondition sees missing-PSIDTS state, post-flock
        # re-read (via _is_psidts_persisted) sees healed state.
        pre_heal_state = {"cookies": _RECOVERABLE_COOKIES}
        post_heal_state = {
            "cookies": _RECOVERABLE_COOKIES
            + [
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "healed_by_sibling_process",
                    "domain": ".google.com",
                    "path": "/",
                }
            ]
        }
        call_counter = {"n": 0}

        def staged_load(_p):
            call_counter["n"] += 1
            return pre_heal_state if call_counter["n"] == 1 else post_heal_state

        # Patch the local aliases on ``psidts_recovery`` (ADR-007 object-target
        # form) — the recovery path resolves these symbols via this module's
        # globals at call time.
        monkeypatch.setattr(psidts_recovery, "_load_storage_state", staged_load)
        monkeypatch.setattr(psidts_recovery, "_file_lock_try_exclusive", held_lock)

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        # No POST — the holder already did the work.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_post_flock_recheck_skips_post_when_file_healed_meanwhile(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Acquired the flock BUT another process healed between initial check
        and flock-acquired → don't fire POST, return True (TOCTOU close).

        Mirrors ``_poke_session``'s "one last disk recheck" at
        ``_auth/keepalive.py:283-290``. Pinned by CodeRabbit Major: issue #865.
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        pre_heal_state = {"cookies": _RECOVERABLE_COOKIES}
        post_heal_state = {
            "cookies": _RECOVERABLE_COOKIES
            + [
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "healed_meanwhile",
                    "domain": ".google.com",
                    "path": "/",
                }
            ]
        }
        call_counter = {"n": 0}

        def staged_load(_p):
            call_counter["n"] += 1
            return pre_heal_state if call_counter["n"] == 1 else post_heal_state

        # Patch the local alias on ``psidts_recovery`` (ADR-007 object-target
        # form) — the recovery path resolves ``_load_storage_state`` via this
        # module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_load_storage_state", staged_load)

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        # Crucial: no POST — recheck saw the heal before we fired.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_post_flock_recheck_re_validates_full_preconditions(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """If a concurrent write LOSES SID or secondary binding between the initial
        precondition read and acquiring the flock, the post-flock recheck must
        decline rather than fire a doomed POST (CodeRabbit follow-up: issue #865).
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        # Pre-heal: precondition gate passes. Post-heal: SID got dropped by a
        # concurrent process (e.g. logout, profile switch).
        pre_heal_state = {"cookies": _RECOVERABLE_COOKIES}
        post_heal_state = {"cookies": [c for c in _RECOVERABLE_COOKIES if c["name"] != "SID"]}
        call_counter = {"n": 0}

        def staged_load(_p):
            call_counter["n"] += 1
            return pre_heal_state if call_counter["n"] == 1 else post_heal_state

        # Patch the local alias on ``psidts_recovery`` (ADR-007 object-target
        # form) — the recovery path resolves ``_load_storage_state`` via this
        # module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_load_storage_state", staged_load)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # No POST — recheck saw the broken state and aborted before firing.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []
