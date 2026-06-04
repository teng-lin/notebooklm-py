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
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm.paths as _nb_paths
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
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")


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
        # ``psidts_recovery`` (ADR-0007 object-target form) — the recovery path
        # resolves the symbol via this module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_try_claim_rotation", lambda _path: False)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []


class TestPsidtsExpiryGate:
    """The precondition gate must treat a present-but-EXPIRED PSIDTS as absent.

    Tier-0 cold-start fix: ``_recover_psidts_inline`` originally keyed purely on
    name presence, so an idle Chrome session whose ``__Secure-1PSIDTS`` row is
    still on disk but past its ``expires`` epoch silently skipped the one
    ``RotateCookies`` POST that would heal it (cold-start then failed at the
    first authed GET). A ``-1``/``None`` (session-cookie) expiry stays
    not-expired, matching ``_storage_entry_to_cookie``.
    """

    _PAST = 1_000_000_000  # 2001-09-09, comfortably in the past
    _FUTURE = 99_999_999_999  # year 5138, comfortably in the future

    @staticmethod
    def _with_psidts(*, expires) -> list[dict]:
        return _RECOVERABLE_COOKIES + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "stale_or_fresh",
                "domain": ".google.com",
                "path": "/",
                "expires": expires,
            }
        ]

    # --- direct unit tests of the helper with an injectable ``now`` ---------

    def test_helper_expired_needs_recovery(self):
        """Present + expires strictly before ``now`` → recovery proceeds."""
        assert (
            psidts_recovery._psidts_needs_recovery(
                {"__Secure-1PSIDTS"}, {"__Secure-1PSIDTS": 100.0}, now=200.0
            )
            is True
        )

    def test_helper_fresh_is_skipped(self):
        """Present + expires at/after ``now`` → recovery is a no-op."""
        assert (
            psidts_recovery._psidts_needs_recovery(
                {"__Secure-1PSIDTS"}, {"__Secure-1PSIDTS": 300.0}, now=200.0
            )
            is False
        )

    def test_helper_session_cookie_is_skipped(self):
        """``expires`` of -1 / None is a session cookie → never expired."""
        for sentinel in (-1, None):
            assert (
                psidts_recovery._psidts_needs_recovery(
                    {"__Secure-1PSIDTS"}, {"__Secure-1PSIDTS": sentinel}, now=200.0
                )
                is False
            ), sentinel

    def test_helper_missing_needs_recovery(self):
        """Absent PSIDTS → recovery proceeds (current behavior, preserved)."""
        assert psidts_recovery._psidts_needs_recovery(set(), {}, now=200.0) is True

    def test_helper_expires_exactly_now_is_skipped(self):
        """Boundary: ``expires == now`` is fresh (``expires < now`` is strict)."""
        assert (
            psidts_recovery._psidts_needs_recovery(
                {"__Secure-1PSIDTS"}, {"__Secure-1PSIDTS": 200.0}, now=200.0
            )
            is False
        )

    # --- domain-filtered / priority-resolved index gate --------------------

    def test_psidts_on_unallowed_domain_does_not_skip_recovery(self):
        """A PSIDTS on a non-Google domain must NOT satisfy the precondition.

        Otherwise a stray ``__Secure-1PSIDTS`` cookie left by an unrelated site
        would falsely mark the Google session healthy and skip the heal.
        """
        entries = _RECOVERABLE_COOKIES + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "evil",
                "domain": ".evil.example",
                "path": "/",
                "expires": self._FUTURE,
            }
        ]
        names, expiry = psidts_recovery._index_recovery_cookies(entries)
        assert "__Secure-1PSIDTS" not in names
        assert psidts_recovery._psidts_needs_recovery(names, expiry) is True

    def test_index_prefers_base_google_domain_for_duplicates(self):
        """Duplicate names resolve by ``_auth_domain_priority`` (``.google.com`` wins).

        Regardless of list order, the ``.google.com`` PSIDTS expiry must win
        over a regional-domain duplicate so the gate is order-independent.
        """
        fresh_base = {
            "name": "__Secure-1PSIDTS",
            "value": "base",
            "domain": ".google.com",
            "path": "/",
            "expires": self._FUTURE,
        }
        expired_regional = {
            "name": "__Secure-1PSIDTS",
            "value": "regional",
            "domain": ".google.com.sg",
            "path": "/",
            "expires": self._PAST,
        }
        for ordering in ([fresh_base, expired_regional], [expired_regional, fresh_base]):
            names, expiry = psidts_recovery._index_recovery_cookies(_RECOVERABLE_COOKIES + ordering)
            assert expiry["__Secure-1PSIDTS"] == self._FUTURE, ordering
            assert psidts_recovery._psidts_needs_recovery(names, expiry) is False, ordering

    # --- file-based recovery end-to-end ------------------------------------

    @pytest.mark.no_default_keepalive_mock
    def test_present_but_expired_fires_recovery(self, tmp_path, httpx_mock: HTTPXMock):
        """The idle-Chrome case: PSIDTS on disk but expired → POST fires + heals."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=self._PAST))
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

        rotate_requests = [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]
        assert len(rotate_requests) == 1
        saved = json.loads(storage_path.read_text(encoding="utf-8"))
        fresh = next(c for c in saved["cookies"] if c["name"] == "__Secure-1PSIDTS")
        assert fresh["value"] == "fresh_psidts_value"

    @pytest.mark.no_default_keepalive_mock
    def test_present_and_fresh_skips_recovery(self, tmp_path, httpx_mock: HTTPXMock):
        """A future-dated PSIDTS is healthy → no POST."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=self._FUTURE))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_present_session_cookie_skips_recovery(self, tmp_path, httpx_mock: HTTPXMock):
        """A session-cookie (-1) PSIDTS is not expired → no POST (current behavior)."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=-1))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    # --- in-memory twin ----------------------------------------------------

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_present_but_expired_fires_recovery(self, httpx_mock: HTTPXMock):
        now = time.time()
        cookies = [
            {"name": "SID", "value": "s", "domain": ".google.com", "path": "/"},
            {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "sa", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "stale",
                "domain": ".google.com",
                "path": "/",
                "expires": now - 3600,
            },
        ]
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True
        fresh = [
            c
            for c in cookies
            if c["name"] == "__Secure-1PSIDTS" and c["value"] == "fresh_psidts_value"
        ]
        assert len(fresh) == 1

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_present_and_fresh_skips_recovery(self, httpx_mock: HTTPXMock):
        now = time.time()
        cookies = [
            {"name": "SID", "value": "s", "domain": ".google.com", "path": "/"},
            {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "sa", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "fresh_on_disk",
                "domain": ".google.com",
                "path": "/",
                "expires": now + 3600,
            },
        ]

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_session_cookie_skips_recovery(self, httpx_mock: HTTPXMock):
        """A session-cookie (-1) PSIDTS on the in-memory path is not expired → no POST."""
        cookies = [
            {"name": "SID", "value": "s", "domain": ".google.com", "path": "/"},
            {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "sa", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "session",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
            },
        ]

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    # --- flock-held re-read (``_is_psidts_persisted``) ---------------------

    def test_is_psidts_persisted_false_for_expired_on_disk_row(self, tmp_path):
        """The held-flock re-read must NOT mistake a stale PSIDTS for a heal.

        ``_is_psidts_persisted`` backs the flock-held skip path: a
        present-but-expired on-disk row counts as *not* persisted, so the
        caller keeps trying to heal instead of returning a false success.
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=self._PAST))
        assert psidts_recovery._is_psidts_persisted(storage_path) is False

    def test_is_psidts_persisted_true_for_fresh_on_disk_row(self, tmp_path):
        """A future-dated on-disk PSIDTS counts as persisted (heal observed)."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=self._FUTURE))
        assert psidts_recovery._is_psidts_persisted(storage_path) is True


class TestRecoveryHappyPath:
    """End-to-end recovery: POST + persist + reload."""

    @pytest.mark.no_default_keepalive_mock
    def test_persists_psidts_to_storage_state(self, tmp_path, httpx_mock: HTTPXMock):
        """The rotated PSIDTS must land in storage_state.json on disk."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

        saved = json.loads(storage_path.read_text(encoding="utf-8"))
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

        saved = json.loads(storage_path.read_text(encoding="utf-8"))
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
        saved = json.loads(storage_path.read_text(encoding="utf-8"))
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
        saved = json.loads(storage_path.read_text(encoding="utf-8"))
        assert "__Secure-1PSIDTS" not in {c["name"] for c in saved["cookies"]}

    @pytest.mark.no_default_keepalive_mock
    def test_network_error_returns_false(self, tmp_path, httpx_mock: HTTPXMock):
        """A connection error during the POST → False, not a raise."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_exception(httpx.ConnectError("simulated network failure"))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_expired_psidts_with_200_minting_nothing_returns_false(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A no-op save over a *stale* PSIDTS must not be a false heal.

        Disk starts with an EXPIRED PSIDTS (so the gate fires recovery). The
        POST 200s but mints no fresh PSIDTS, so the expired row lingers in the
        request jar and the save is a no-op that reports ``ok=True``. Recovery
        keys on disk, not on the coarse bool, so it must DECLINE — the stale
        cookie is still all that's on disk (codex review of #1273).
        """
        storage_path = tmp_path / "storage_state.json"
        expired_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "stale_value",
            "domain": ".google.com",
            "path": "/",
            "expires": 1_000_000_000,  # 2001 — comfortably in the past
        }
        _write_storage(storage_path, [*_RECOVERABLE_COOKIES, expired_psidts])
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response(include_psidts=False))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False


class TestRecoveryConcurrentCasRejection:
    """Recovery keys on PSIDTS-on-disk, not on the coarse save bool (#1273).

    ``save_cookies_to_storage`` returns a coarse ``False`` whenever *any* key is
    CAS-rejected, even when a fresh PSIDTS is on disk. The coarse bool conflates
    (a) "an unrelated sibling cookie lost the CAS race but our PSIDTS wrote
    through" and (b) "our PSIDTS delta was rejected because a sibling already
    persisted a *fresh* PSIDTS first" — both leave disk healthy. So recovery
    must re-read disk and accept the heal iff a present, unexpired PSIDTS is
    stored, never trust ``cas_rejected_keys`` membership alone.
    """

    @staticmethod
    def _patch_save_with_cas_rejection(
        monkeypatch, *, rejected_key, do_real_write: bool, seed_disk_cookie=None
    ):
        """Replace ``save_cookies_to_storage`` with a CAS-rejecting stand-in.

        The stand-in honours ``return_result`` exactly like the real function
        (bool projection by default, ``CookieSaveResult`` when asked) so the
        test is faithful regardless of which signature the recovery path uses.
        When ``do_real_write`` is set, the rotated cookies are genuinely
        persisted first, modelling "our PSIDTS wrote through but a sibling lost".
        ``seed_disk_cookie`` lets a test inject a sibling-written row onto disk
        before reporting the coarse failure, modelling "a sibling persisted a
        fresh PSIDTS first, so our delta was CAS-rejected".
        """
        from notebooklm._auth import storage as _auth_storage

        real_save = _auth_storage.save_cookies_to_storage

        def _save(cookie_jar, path=None, **kwargs):
            return_result = kwargs.get("return_result", False)
            if do_real_write:
                real_save(cookie_jar, path, **{**kwargs, "return_result": True})
            if seed_disk_cookie is not None and path is not None:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["cookies"].append(seed_disk_cookie)
                path.write_text(json.dumps(data), encoding="utf-8")
            result = _auth_storage.CookieSaveResult(
                ok=False, cas_rejected_keys=frozenset({rejected_key})
            )
            return result if return_result else result.ok

        monkeypatch.setattr(psidts_recovery._auth_storage, "save_cookies_to_storage", _save)

    @pytest.mark.no_default_keepalive_mock
    def test_succeeds_when_sibling_cookie_cas_rejected_but_psidts_written(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Our PSIDTS wrote through; a *different* cookie was CAS-rejected.

        Models heavily-parallel multi-process CLI usage: a sibling process
        wins a CAS race on some unrelated cookie, so the save reports a coarse
        ``False`` even though the rotated PSIDTS persisted. Recovery must
        SUCCEED, not decline.
        """
        from notebooklm._auth import storage as _auth_storage

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        sibling_key = _auth_storage.CookieSnapshotKey("SID", ".google.com", "/")
        self._patch_save_with_cas_rejection(
            monkeypatch, rejected_key=sibling_key, do_real_write=True
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        saved = json.loads(storage_path.read_text(encoding="utf-8"))
        assert "__Secure-1PSIDTS" in {c["name"] for c in saved["cookies"]}

    @pytest.mark.no_default_keepalive_mock
    def test_succeeds_when_psidts_cas_rejected_but_sibling_wrote_fresh_psidts(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Our PSIDTS delta lost the CAS race because a sibling wrote a fresh one.

        A PSIDTS CAS rejection means disk diverged from our snapshot — which
        happens precisely when a sibling process persisted its *own* fresh
        PSIDTS first. Disk is healthy, so recovery must SUCCEED even though our
        write was the one rejected. Trusting ``cas_rejected_keys`` membership
        alone would wrongly decline here (codex review of #1273).
        """
        from notebooklm._auth import storage as _auth_storage

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_key = _auth_storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        sibling_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "sibling_fresh_value",
            "domain": ".google.com",
            "path": "/",
            "expires": 99_999_999_999,  # comfortably in the future
        }
        self._patch_save_with_cas_rejection(
            monkeypatch,
            rejected_key=psidts_key,
            do_real_write=False,
            seed_disk_cookie=sibling_psidts,
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

    @pytest.mark.no_default_keepalive_mock
    def test_declines_when_psidts_cas_rejected_and_disk_lacks_psidts(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """PSIDTS rejected and disk still lacks a fresh PSIDTS → must decline.

        Defends the false-heal direction: when no fresh PSIDTS is on disk after
        the save, recovery must keep failing rather than report success.
        """
        from notebooklm._auth import storage as _auth_storage

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_key = _auth_storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        self._patch_save_with_cas_rejection(
            monkeypatch, rejected_key=psidts_key, do_real_write=False
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is False

    @pytest.mark.no_default_keepalive_mock
    def test_declines_when_disk_only_has_expired_psidts(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A stale (expired) sibling PSIDTS row must NOT masquerade as a heal.

        The disk re-read mirrors the precondition gate, so an expired on-disk
        PSIDTS counts as absent and recovery must still decline.
        """
        from notebooklm._auth import storage as _auth_storage

        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_key = _auth_storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        expired_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "stale_value",
            "domain": ".google.com",
            "path": "/",
            "expires": 1_000_000_000,  # 2001 — comfortably in the past
        }
        self._patch_save_with_cas_rejection(
            monkeypatch,
            rejected_key=psidts_key,
            do_real_write=False,
            seed_disk_cookie=expired_psidts,
        )

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
        # recoverable-but-PSIDTS-missing state. Object-form patches (ADR-0007
        # Form-2) against the live module-object seams so both
        # ``_load_storage_state`` (module-level ``get_storage_path`` in
        # ``_auth.cookies``, reached via the ``psidts_recovery._auth_cookies``
        # alias) and the recovery's ``_resolve_recovery_path`` (lazy
        # ``from ..paths import get_storage_path``, reached via the
        # ``notebooklm.paths`` module object) see the same override.
        default_path = tmp_path / "default_storage_state.json"
        _write_storage(default_path, _RECOVERABLE_COOKIES)
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        fake_paths_get = Mock(return_value=default_path)
        fake_cookies_get = Mock(return_value=default_path)
        monkeypatch.setattr(_nb_paths, "get_storage_path", fake_paths_get)
        monkeypatch.setattr(psidts_recovery._auth_cookies, "get_storage_path", fake_cookies_get)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        cookies = auth_module.load_auth_from_storage(None)

        assert cookies["__Secure-1PSIDTS"] == "fresh_psidts_value"
        # Bite-check: both module-object seams must be the live resolution path.
        # ``_resolve_recovery_path`` reaches ``notebooklm.paths.get_storage_path``;
        # ``_load_storage_state`` reaches ``_auth.cookies.get_storage_path``.
        fake_paths_get.assert_called()
        fake_cookies_get.assert_called()


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
        saved = json.loads(storage_path.read_text(encoding="utf-8"))
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


class TestInMemoryRecovery:
    """In-memory recovery for the browser-extraction path (issue #990).

    Mirrors the file-based ``_recover_psidts_inline`` contract: same precondition
    gate, same failure modes return ``False`` without raising, but operates on
    a rookiepy cookie list in memory instead of a storage_state file. No file
    lock / throttle because the extraction path is a single one-shot CLI run.
    """

    # Rookiepy uses snake_case field names; mirror that shape here so the
    # in-memory recovery exercises the real format produced by rookiepy.load().
    # ``expires`` omitted = session cookie; an explicit ``int`` would be epoch
    # seconds — small values like 9999 land in 1970 and get filtered as expired
    # before reaching the wire.
    @staticmethod
    def _rookiepy_recoverable() -> list[dict]:
        return [
            {
                "name": "SID",
                "value": "test_sid",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "http_only": False,
            },
            {
                "name": "APISID",
                "value": "test_apisid",
                "domain": ".google.com",
                "path": "/",
                "secure": False,
                "http_only": False,
            },
            {
                "name": "SAPISID",
                "value": "test_sapisid",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            },
        ]

    @pytest.mark.no_default_keepalive_mock
    def test_recovers_psidts_and_mutates_list_in_place(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True

        names = {c["name"] for c in cookies}
        assert "__Secure-1PSIDTS" in names
        psidts = next(c for c in cookies if c["name"] == "__Secure-1PSIDTS")
        assert psidts["value"] == "fresh_psidts_value"
        assert psidts["domain"] == ".google.com"

    @pytest.mark.no_default_keepalive_mock
    def test_post_carries_existing_cookies(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_recovery.recover_psidts_in_memory(cookies)

        requests = [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]
        assert len(requests) == 1
        header = requests[0].headers.get("cookie", "")
        assert "SID=test_sid" in header
        assert "APISID=test_apisid" in header
        assert "SAPISID=test_sapisid" in header

    @pytest.mark.no_default_keepalive_mock
    def test_no_sid_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = [c for c in self._rookiepy_recoverable() if c["name"] != "SID"]
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_psidts_already_present_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable() + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "already_there",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            }
        ]
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_missing_secondary_binding_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = [
            c for c in self._rookiepy_recoverable() if c["name"] not in {"APISID", "SAPISID"}
        ]
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_osid_satisfies_secondary_binding(self, httpx_mock: HTTPXMock):
        cookies = [
            {
                "name": "SID",
                "value": "test_sid",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "http_only": False,
            },
            {
                "name": "OSID",
                "value": "test_osid",
                "domain": "notebooklm.google.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            },
        ]
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True

    @pytest.mark.no_default_keepalive_mock
    def test_4xx_response_returns_false(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=401)

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert "__Secure-1PSIDTS" not in {c["name"] for c in cookies}

    @pytest.mark.no_default_keepalive_mock
    def test_200_without_psidts_returns_false(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response(include_psidts=False))

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert "__Secure-1PSIDTS" not in {c["name"] for c in cookies}

    @pytest.mark.no_default_keepalive_mock
    def test_network_error_returns_false(self, httpx_mock: HTTPXMock):
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_exception(httpx.ConnectError("simulated network failure"))

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False

    @pytest.mark.no_default_keepalive_mock
    def test_validate_with_recovery_heals_partial_jar(self, httpx_mock: HTTPXMock):
        """End-to-end: validate-with-recovery returns (storage_state, None) after rotation."""
        cookies = self._rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        storage_state, error = psidts_recovery.validate_with_recovery(cookies)

        assert error is None
        names = {c["name"] for c in storage_state["cookies"]}
        assert "__Secure-1PSIDTS" in names
        # Caller's list is also healed (so downstream persistence picks it up).
        assert "__Secure-1PSIDTS" in {c["name"] for c in cookies}

    @pytest.mark.no_default_keepalive_mock
    def test_validate_with_recovery_returns_error_on_unrecoverable(self, httpx_mock: HTTPXMock):
        """When recovery declines, the original ValueError is surfaced."""
        # No SID → recovery declines → original ValueError propagates.
        cookies = [c for c in self._rookiepy_recoverable() if c["name"] != "SID"]

        storage_state, error = psidts_recovery.validate_with_recovery(cookies)

        assert error is not None
        assert "SID" in str(error)
        # No POST fired (recovery preconditions failed early).
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []
        # storage_state still reflects the (incomplete) extraction attempt.
        assert isinstance(storage_state, dict)


class TestMissingCookiesHint:
    """Diagnostic helper that branches on which cookies are missing (issue #990)."""

    def test_no_sid_suggests_signing_in(self):
        from notebooklm._auth.cookie_policy import missing_cookies_hint

        hint = missing_cookies_hint(set(), browser_label="chrome")
        assert "not signed in" in hint
        assert "chrome" in hint

    # NOTE: We assert on non-URL hint phrases rather than the
    # ``https://notebooklm.google.com`` literal so CodeQL's
    # ``py/incomplete-url-substring-sanitization`` rule doesn't flag these
    # checks (the hint itself contains the canonical URL).
    def test_missing_psidts_with_binding_suggests_rotation_or_visit(self):
        from notebooklm._auth.cookie_policy import missing_cookies_hint

        hint = missing_cookies_hint({"SID", "APISID", "SAPISID"}, browser_label="firefox")
        assert "__Secure-1PSIDTS" in hint
        assert "RotateCookies recovery" in hint
        assert "firefox" in hint

    def test_missing_psidts_and_binding_suggests_visit(self):
        from notebooklm._auth.cookie_policy import missing_cookies_hint

        hint = missing_cookies_hint({"SID"}, browser_label="chrome")
        assert "reload the page" in hint
        assert ("OSID" in hint) or ("binding" in hint.lower())

    def test_missing_binding_only_suggests_visit(self):
        from notebooklm._auth.cookie_policy import missing_cookies_hint

        # SID + PSIDTS present, but no secondary binding.
        hint = missing_cookies_hint({"SID", "__Secure-1PSIDTS"}, browser_label="chrome")
        assert "reload the page" in hint
        assert "binding" in hint.lower() or "OSID" in hint

    def test_default_browser_label_when_unspecified(self):
        from notebooklm._auth.cookie_policy import missing_cookies_hint

        hint = missing_cookies_hint(set())
        assert "your browser" in hint


class TestEdgeCases:
    """Hardening tests for the precondition gate and post-POST persistence."""

    @pytest.mark.no_default_keepalive_mock
    def test_malformed_storage_cookies_non_list(self, tmp_path, httpx_mock: HTTPXMock):
        """``"cookies"`` key not a list → return False without firing POST."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": "not-a-list"}), encoding="utf-8")

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    @pytest.mark.no_default_keepalive_mock
    def test_save_returning_false_propagates_as_failure(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """A failed persist (no disk write) must make recovery decline.

        The mock returns a falsy save result *without* writing to disk, so the
        disk re-read in ``_psidts_save_succeeded`` finds no fresh PSIDTS and
        recovery returns False. Recovery keys on disk, not on the save's return
        value (issue #1273), so a failed persistence — for any reason — declines
        rather than logging a misleading ``Recovered ... and persisted`` INFO
        over still-broken state (issue #865).
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        # Force the persist step to return False (CAS rejection / I/O error / etc.).
        # Object-form patch against the local ``_auth_storage`` module alias on
        # ``psidts_recovery`` (ADR-0007 Form-2) — the recovery resolves
        # ``_auth_storage.save_cookies_to_storage`` via this module's globals at
        # call time, so patching the alias module object is the live seam.
        fake_save = Mock(return_value=False)
        monkeypatch.setattr(psidts_recovery._auth_storage, "save_cookies_to_storage", fake_save)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # Bite-check: the recovery must actually reach the persist step.
        fake_save.assert_called()

    @pytest.mark.no_default_keepalive_mock
    def test_save_raising_propagates_as_failure(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """Unexpected exception from ``save_cookies_to_storage`` → False, not propagated."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        # Object-form patch against the local ``_auth_storage`` module alias on
        # ``psidts_recovery`` (ADR-0007 Form-2) — the recovery resolves
        # ``_auth_storage.save_cookies_to_storage`` via this module's globals at
        # call time, so patching the alias module object is the live seam.
        fake_save = Mock(side_effect=OSError("simulated disk-full"))
        monkeypatch.setattr(psidts_recovery._auth_storage, "save_cookies_to_storage", fake_save)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # Bite-check: the recovery must actually reach the persist step.
        fake_save.assert_called()

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

        # Patch the local alias on ``psidts_recovery`` (ADR-0007 object-target
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

        # Patch the local aliases on ``psidts_recovery`` (ADR-0007 object-target
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

        # Patch the local alias on ``psidts_recovery`` (ADR-0007 object-target
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

        # Patch the local alias on ``psidts_recovery`` (ADR-0007 object-target
        # form) — the recovery path resolves ``_load_storage_state`` via this
        # module's globals at call time.
        monkeypatch.setattr(psidts_recovery, "_load_storage_state", staged_load)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # No POST — recheck saw the broken state and aborted before firing.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []
