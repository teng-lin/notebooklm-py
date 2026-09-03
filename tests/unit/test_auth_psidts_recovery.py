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

import itertools
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm.paths as _nb_paths
from notebooklm import auth as auth_module
from notebooklm._auth import cookies as auth_cookies
from notebooklm._auth import psidts_recovery
from notebooklm._auth.keepalive import RotationState

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


def _stage_storage_reads(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *cookie_states: list[dict],
) -> None:
    """Serve exact successive disk samples without patching an auth module."""
    real_read_text = Path.read_text
    states = iter(cookie_states)
    last = cookie_states[-1]

    def _read_text(current: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal last
        if current == path:
            last = next(states, last)
            return json.dumps({"cookies": last, "origins": []})
        return real_read_text(current, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)


def _rotate_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    """Return only the ``RotateCookies`` POSTs the mock recorded.

    Filtered rather than counted wholesale: the recovery paths run under an
    autouse keepalive mock that may record unrelated traffic, so "did the POST
    fire?" has to be asked about this URL specifically.
    """
    return [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]


def _rookiepy_recoverable() -> list[dict]:
    """Rookiepy-shaped cookies meeting the recovery precondition, PSIDTS absent.

    The snake_case twin of :data:`_RECOVERABLE_COOKIES` — rookiepy's own field
    spelling (``http_only``), which the in-memory path consumes directly without
    going through ``convert_rookiepy_cookies_to_storage_state``. ``expires`` is
    omitted (= session cookie); an explicit small ``int`` would be epoch seconds,
    landing in 1970 and being filtered as expired before reaching the wire.
    """
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
        assert _rotate_requests(httpx_mock) == []

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
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_missing_secondary_binding_returns_false_without_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """No OSID, no APISID+SAPISID — Google will reject RotateCookies."""
        cookies = [c for c in _RECOVERABLE_COOKIES if c["name"] not in {"APISID", "SAPISID"}]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert _rotate_requests(httpx_mock) == []

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
    def test_throttle_claim_failure_skips_post(self, tmp_path, httpx_mock: HTTPXMock):
        """A claimed rotation slot prevents the POST from firing."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        # Claim the real process-owned slot first, exactly as a sibling caller
        # would. The recovery attempt must observe the recent claim and stop.
        assert RotationState.process_default().try_claim(storage_path) is True

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert _rotate_requests(httpx_mock) == []


class TestPsidtsExpiryGate:
    """The precondition gate: RFC 6265 routing, not a domain-priority ranking.

    Two questions, two predicates (issue #2057):

    - ``_psidts_routes_to_rotate`` — *should we fire?* Answered against the URL
      the decision is about, so an absent, expired, or non-routing
      ``__Secure-1PSIDTS`` all mean "fire".
    - ``_psidts_is_live`` — *did the heal land?* Deliberately domain-blind,
      because it must predict the caller's retried preflight, which is
      name-presence over the allowlist.

    The predecessor gate ranked one global winner by ``_auth_domain_priority``
    and read that winner's expiry. The tiers are not distinct, so the winner
    depended on ``storage_state`` ordering; and the ranking was inverted
    relative to the action it gated, since ``accounts.google.com`` — the host
    the POST targets — sits in the lowest tier.

    A ``-1``/``None`` (session-cookie) expiry stays not-expired, matching
    ``_storage_entry_to_cookie``.
    """

    _PAST = 1_000_000_000  # 2001-09-09, comfortably in the past
    # Year 2100, comfortably in the future. Matches the project-wide
    # "far future" sentinel used elsewhere (e.g. tests/unit/cli/_session_helpers.py)
    # rather than a value that would exceed cookie_semantics.py's
    # millisecond-plausibility bound (_MAX_PLAUSIBLE_EXPIRY_SECONDS, year 3000)
    # and get misread as a millisecond timestamp.
    _FUTURE = 4_102_444_800

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

    @staticmethod
    def _psidts(domain=".google.com", *, expires, value="v", path="/") -> dict:
        return {
            "name": "__Secure-1PSIDTS",
            "value": value,
            "domain": domain,
            "path": path,
            "expires": expires,
            "secure": True,
        }

    @staticmethod
    def _routes(entries, *, now=None) -> bool:
        return psidts_recovery._psidts_routes_to_rotate(
            entries, to_cookie=psidts_recovery._storage_cookie, now=now
        )

    @staticmethod
    def _live(entries, *, now=None) -> bool:
        return psidts_recovery._psidts_is_live(
            entries, to_cookie=psidts_recovery._storage_cookie, now=now
        )

    # --- expiry, via the injectable ``now`` seam ----------------------------

    def test_expired_does_not_route_so_recovery_proceeds(self):
        """Expired against ``now`` → nothing routes → recovery fires."""
        assert self._routes([self._psidts(expires=100.0)], now=200.0) is False

    def test_fresh_routes_so_recovery_is_skipped(self):
        """Fresh against ``now`` → routes to the rotate URL → recovery is a no-op.

        Pins the fix for the seam that ``CookieJar.add_cookie_header`` would
        otherwise destroy: it resets its own clock from ``time.time()``, so an
        injected ``now`` in the past could only ever mark cookies expired. The
        probe copies carry no expiry, leaving ``now`` the sole authority.
        """
        assert self._routes([self._psidts(expires=300.0)], now=200.0) is True

    def test_session_cookie_routes(self):
        """``expires`` of -1 / None is a session cookie → never expired."""
        for sentinel in (-1, None):
            assert self._routes([self._psidts(expires=sentinel)], now=200.0) is True, sentinel

    def test_missing_psidts_does_not_route(self):
        """Absent PSIDTS → recovery proceeds (original behavior, preserved)."""
        assert self._routes(_RECOVERABLE_COOKIES, now=200.0) is False

    def test_expires_exactly_now_is_expired(self):
        """Boundary: ``expires == now`` is EXPIRED.

        Deliberate change. The predecessor gate compared ``expires < now``;
        ``http.cookiejar.Cookie.is_expired`` compares ``expires <= now``, and it
        is now the single expiry authority. One second, intentionally moved.
        """
        assert self._routes([self._psidts(expires=200.0)], now=200.0) is False
        assert self._live([self._psidts(expires=200.0)], now=200.0) is False
        assert self._routes([self._psidts(expires=201.0)], now=200.0) is True

    # --- RFC 6265 routing, not domain ranking (issue #2057) ----------------

    def test_psidts_on_unallowed_domain_does_not_skip_recovery(self):
        """A PSIDTS on a non-Google domain must NOT satisfy the precondition.

        Otherwise a stray ``__Secure-1PSIDTS`` cookie left by an unrelated site
        would falsely mark the Google session healthy and skip the heal.
        """
        entries = _RECOVERABLE_COOKIES + [self._psidts(".evil.example", expires=self._FUTURE)]
        assert self._routes(entries) is False
        assert self._live(entries) is False

    @pytest.mark.parametrize(
        "domain, routes",
        [
            (".google.com", True),
            ("google.com", True),  # no leading dot still routes (cookiejar v0)
            ("accounts.google.com", True),  # host cookie on the POST's own host
            (".accounts.google.com", True),
            (".notebooklm.google.com", False),  # app host never reaches accounts.
            ("notebooklm.google.com", False),
            (".notebook.google.com", False),  # Gemini Notebook rebrand host
            (".google.com.sg", False),  # regional ccTLD
            (".googleusercontent.com", False),
        ],
    )
    def test_routing_follows_rfc6265_not_domain_tier(self, domain, routes):
        """Whether recovery fires is decided by routing, not by priority tier.

        ``accounts.google.com`` — the exact host ``KEEPALIVE_ROTATE_URL``
        targets — sits in the LOWEST priority tier, while the app hosts that
        outrank it never reach that host at all. The ranked gate had the
        ordering exactly inverted relative to the action it gated.
        """
        entries = [self._psidts(domain, expires=self._FUTURE)]
        assert self._routes(entries) is routes
        # Every one of these is live: "did it land?" is domain-blind by design.
        assert self._live(entries) is True

    def test_path_scoped_psidts_does_not_route(self):
        """RFC 6265 §5.4 path matching applies — the ranked gate ignored ``path``."""
        entries = [self._psidts(expires=self._FUTURE, path="/elsewhere")]
        assert self._routes(entries) is False
        assert self._live(entries) is True

    def test_divergent_case_stale_ranked_winner_fresh_sibling(self):
        """The case the ranked gate got wrong (issue #2057).

        ``.google.com`` is tier 4 and outranks everything, so a stale row there
        made the ranked gate report "needs recovery" / "not persisted" even
        though a fresh ``accounts.google.com`` sibling both routes to the POST
        and satisfies the preflight.
        """
        entries = [
            self._psidts(".google.com", expires=self._PAST),
            self._psidts("accounts.google.com", expires=self._FUTURE),
        ]
        assert self._routes(entries) is True
        assert self._live(entries) is True

    def test_inverse_divergence_app_host_only(self):
        """Fresh on the app host only: the heal IS needed, and the preflight WILL pass.

        The two predicates must disagree here — that disagreement is the whole
        point of splitting them.
        """
        entries = [self._psidts(".notebooklm.google.com", expires=self._FUTURE)]
        assert self._routes(entries) is False
        assert self._live(entries) is True

    @pytest.mark.parametrize(
        "entries_factory",
        [
            lambda s: [s._psidts(".google.com", expires=s._FUTURE)],
            lambda s: [
                s._psidts(".google.com", expires=s._PAST),
                s._psidts("accounts.google.com", expires=s._FUTURE),
            ],
            lambda s: [
                s._psidts(".notebooklm.google.com", expires=s._FUTURE),
                s._psidts(".google.com.sg", expires=s._PAST),
            ],
            # Duplicate identity + a malformed row: the two cases where an
            # order-sensitive implementation is most likely to slip through.
            lambda s: [
                s._psidts(".google.com", expires=s._FUTURE, value="a"),
                s._psidts(".google.com", expires=s._PAST, value="b"),
                s._psidts(".google.com", expires="nonsense", value="c"),
            ],
        ],
    )
    def test_reordering_storage_state_cannot_change_the_answer(self, entries_factory):
        """Order-independence is the property the ranked gate could not offer.

        Within a shared priority tier the old index resolved duplicates by
        "first occurrence wins", so reordering the file changed the gate's
        answer. This is the ratchet that replaces that surface.

        Exhaustive over permutations rather than sampled: the PSIDTS rows are
        few enough that `itertools.permutations` is both cheap and strictly
        stronger than N random shuffles, and a failure reproduces exactly
        instead of depending on an unseeded RNG.
        """
        psidts_rows = entries_factory(self)
        expected = None
        for ordering in itertools.permutations(psidts_rows):
            entries = _RECOVERABLE_COOKIES + list(ordering)
            answer = (self._routes(entries), self._live(entries))
            if expected is None:
                expected = answer
            assert answer == expected, ordering

    def test_routed_implies_live(self):
        """``routed ⇒ live`` — the invariant that makes the flock-skip return True safe.

        ``_recover_psidts_inline`` reports "healed by another process" purely
        from the routed predicate. That is only sound because anything routing
        to ``accounts.google.com`` is necessarily unexpired and on an allowed
        domain, i.e. also live.
        """
        domains = [".google.com", "accounts.google.com", ".notebooklm.google.com", ".evil.example"]
        expiries = [self._FUTURE, self._PAST, -1, None, "abc", True]
        for domain in domains:
            for expires in expiries:
                for value in ("v", ""):
                    entries = [self._psidts(domain, expires=expires, value=value)]
                    if self._routes(entries):
                        assert self._live(entries), entries

    def test_live_implies_preflight_passes_on_the_psidts_half(self):
        """``live ⇒ the retried preflight passes`` — pins the §"did it land?" alignment.

        ``_is_psidts_persisted`` exists to predict the caller's preflight. If it
        were ever stricter in a way the preflight is not, a healthy session
        would be reported as unhealed and the caller would re-raise.
        """
        for domain in (".google.com", "accounts.google.com", ".notebooklm.google.com"):
            entries = _RECOVERABLE_COOKIES + [self._psidts(domain, expires=self._FUTURE)]
            assert self._live(entries) is True
            # The preflight is domain-blind name presence; it must not raise.
            auth_cookies.extract_cookies_from_storage({"cookies": entries, "origins": []})

    # --- duplicate identities and malformed rows ---------------------------

    def test_duplicate_identity_poisons_routing_but_not_liveness(self):
        """A dead twin blocks ROUTING in either order — but must not block the heal.

        ``http.cookiejar`` keeps one cookie per identity and lets a later row
        replace an earlier one, so a jar built from a duplicated identity
        depends on entry order. The routing predicate resolves that
        conservatively, which is order-independent and never over-optimistic:
        the cost is one needless POST.

        ``_psidts_is_live`` must NOT inherit that rule. It models the retried
        preflight, which is name presence and would pass. Poisoning it turns a
        working session into a PERMANENT failure loop, because
        ``save_cookies_to_storage`` CAS-matches the first stored row and leaves
        the stale twin on disk: every load would fire a POST, write to disk,
        then re-raise "Missing required cookies". The twin is issue #1523's data
        shape — #1523 fixed the producer, and nothing on the load/save path
        removes an existing one.
        """
        fresh = self._psidts(expires=self._FUTURE, value="fresh")
        stale = self._psidts(expires=self._PAST, value="stale")
        for ordering in ([fresh, stale], [stale, fresh]):
            assert self._routes(ordering) is False, ordering
            assert self._live(ordering) is True, ordering
            # The preflight the live predicate models does pass on this state.
            auth_cookies.extract_cookies_from_storage(
                {"cookies": _RECOVERABLE_COOKIES + ordering, "origins": []}
            )

    def test_duplicate_name_on_distinct_identities_is_unaffected(self):
        """Same name, different domain → different identity → no poisoning."""
        entries = [
            self._psidts(".google.com", expires=self._FUTURE),
            self._psidts(".notebooklm.google.com", expires=self._PAST),
        ]
        assert self._routes(entries) is True
        assert self._live(entries) is True

    @pytest.mark.parametrize("expires", ["", "never", float("nan"), float("inf"), [], {}])
    def test_malformed_expires_never_raises_and_splits_the_two_predicates(self, expires):
        """``Cookie.__init__`` coerces eagerly; a bad ``expires`` must not escape.

        Both predicates run inside the callers' ``except ValueError:`` handlers,
        so a coercion error would replace the actionable "Missing required
        cookies" diagnostic with a converter traceback.

        The two predicates then answer differently, and deliberately so. An
        unconvertible row cannot go in a jar, so it cannot be sent: routing says
        "fire", the safe direction. But the row IS on disk and the preflight
        will see it by name, so liveness says "present" — mirroring the
        predecessor gate, which treated an uninterpretable expiry as present
        rather than guessing. Reporting it absent would re-raise over a session
        whose preflight passes.
        """
        entries = _RECOVERABLE_COOKIES + [self._psidts(expires=expires)]
        assert self._routes(entries) is False
        assert self._live(entries) is True

    def test_expired_single_row_is_not_live(self):
        """A row KNOWN to be expired still does not count as healed (issue #1273).

        The relaxation for duplicates and malformed rows must not weaken this:
        a no-op save that leaves one stale PSIDTS behind must not fake a heal.
        """
        entries = _RECOVERABLE_COOKIES + [self._psidts(expires=self._PAST)]
        assert self._routes(entries) is False
        assert self._live(entries) is False

    def test_numeric_string_expires_is_honoured(self):
        """A numeric string coerces to a real timestamp rather than being skipped."""
        assert self._routes([self._psidts(expires=str(self._FUTURE))]) is True
        assert self._routes([self._psidts(expires=str(self._PAST))]) is False

    def test_header_name_match_is_exact_not_substring(self):
        """A lookalike name, or a value embedding the literal, must not satisfy the gate.

        Cookie values on allowlisted domains come from Chrome and are not
        shape-controlled by us. A substring test would let one skip a real heal.
        """
        lookalike = {
            "name": "X__Secure-1PSIDTSY",
            "value": "v",
            "domain": ".google.com",
            "path": "/",
            "expires": self._FUTURE,
        }
        value_embeds = {
            "name": "NID",
            "value": "__Secure-1PSIDTS=oops",
            "domain": ".google.com",
            "path": "/",
            "expires": self._FUTURE,
        }
        assert self._routes([lookalike, value_embeds]) is False
        assert self._live([lookalike, value_embeds]) is False

    # --- file-based recovery end-to-end ------------------------------------

    @pytest.mark.no_default_keepalive_mock
    def test_present_but_expired_fires_recovery(self, tmp_path, httpx_mock: HTTPXMock):
        """The idle-Chrome case: PSIDTS on disk but expired → POST fires + heals."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=self._PAST))
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery._recover_psidts_inline(storage_path) is True

        rotate_requests = _rotate_requests(httpx_mock)
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
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_present_session_cookie_skips_recovery(self, tmp_path, httpx_mock: HTTPXMock):
        """A session-cookie (-1) PSIDTS is not expired → no POST (current behavior)."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, self._with_psidts(expires=-1))

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert _rotate_requests(httpx_mock) == []

    # --- in-memory twin ----------------------------------------------------

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_present_but_expired_fires_recovery(self, httpx_mock: HTTPXMock):
        now = time.time()
        cookies = [
            *_rookiepy_recoverable(),
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
    def test_in_memory_split_state_emits_one_row_per_sidts(self, httpx_mock: HTTPXMock):
        """Split-state recovery must not write a DUPLICATE SIDTS row (issue #1523).

        Trigger: ``__Secure-1PSIDTS`` missing/expired so recovery fires, but a
        fresh ``__Secure-3PSIDTS`` is already in the source jar. RotateCookies
        rotates BOTH; the append loop must end with EXACTLY ONE row per
        ``(name, domain, path)`` carrying the ROTATED value — no second
        ``__Secure-3PSIDTS`` (or ``__Secure-1PSIDTS``) row that has no analog in
        any real browser jar.
        """
        now = time.time()
        cookies = [
            *_rookiepy_recoverable(),
            # __Secure-1PSIDTS expired → recovery fires.
            {
                "name": "__Secure-1PSIDTS",
                "value": "stale_1psidts",
                "domain": ".google.com",
                "path": "/",
                "expires": now - 3600,
            },
            # __Secure-3PSIDTS fresh and already present → would duplicate.
            {
                "name": "__Secure-3PSIDTS",
                "value": "stale_3psidts",
                "domain": ".google.com",
                "path": "/",
                "expires": now + 3600,
            },
        ]
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True

        for name, rotated in (
            ("__Secure-1PSIDTS", "fresh_psidts_value"),
            ("__Secure-3PSIDTS", "fresh_3psidts_value"),
        ):
            rows = [c for c in cookies if c["name"] == name]
            assert len(rows) == 1, f"{name} duplicated: {rows}"
            assert rows[0]["value"] == rotated, f"{name} did not carry the rotated value"

        # The resulting storage_state must likewise hold exactly one row each.
        state = auth_cookies.convert_rookiepy_cookies_to_storage_state(cookies)
        for name in ("__Secure-1PSIDTS", "__Secure-3PSIDTS"):
            state_rows = [c for c in state["cookies"] if c["name"] == name]
            assert len(state_rows) == 1, f"{name} duplicated on disk: {state_rows}"

        # Auth-relevant binding cookies are all still present and correct.
        names = {c["name"]: c["value"] for c in cookies}
        assert names["SID"] == "test_sid"
        assert names["APISID"] == "test_apisid"
        assert names["SAPISID"] == "test_sapisid"

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_present_and_fresh_skips_recovery(self, httpx_mock: HTTPXMock):
        now = time.time()
        cookies = [
            *_rookiepy_recoverable(),
            {
                "name": "__Secure-1PSIDTS",
                "value": "fresh_on_disk",
                "domain": ".google.com",
                "path": "/",
                "expires": now + 3600,
            },
        ]

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_in_memory_session_cookie_skips_recovery(self, httpx_mock: HTTPXMock):
        """A session-cookie (-1) PSIDTS on the in-memory path is not expired → no POST."""
        cookies = [
            *_rookiepy_recoverable(),
            {
                "name": "__Secure-1PSIDTS",
                "value": "session",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
            },
        ]

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert _rotate_requests(httpx_mock) == []

    # --- flock-held re-read (``_is_psidts_persisted``) ---------------------

    @pytest.mark.no_default_keepalive_mock
    def test_stale_twin_row_does_not_block_the_heal(self, tmp_path, httpx_mock: HTTPXMock):
        """A stale same-identity twin must not make a completed heal report failure.

        End-to-end pin for a permanent failure loop. ``save_cookies_to_storage``
        CAS-matches the FIRST stored row for an identity, so when disk carries
        ``__Secure-1PSIDTS`` twice on ``(.google.com, /)`` the rotation lands on
        one row and the stale twin survives the save. If ``_is_psidts_persisted``
        treated that twin as disqualifying, every load would fire a POST, write
        to disk, and then re-raise "Missing required cookies" — for a session
        whose preflight passes. Issue #1523's data shape; #1523 fixed the
        producer, not existing files.
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(
            storage_path,
            _RECOVERABLE_COOKIES
            + [
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "stale_twin",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 1_000_000_000,
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "rotated",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 4_102_444_800,
                },
            ],
        )
        assert psidts_recovery._is_psidts_persisted(storage_path) is True

        # And the whole recovery agrees. Asserting only `_is_psidts_persisted`
        # would leave a regression in `_psidts_save_succeeded` or the
        # `_recover_psidts_inline` wiring green, and that wiring is the thing
        # that used to loop. Note the POST DOES fire: the routing predicate
        # poisons the duplicated identity, which is the safe direction. What
        # must not happen is the heal being reported as a FAILURE afterwards —
        # that is what made it re-raise on every load, forever.
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())
        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        assert len([r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]) == 1

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

        rotate_requests = _rotate_requests(httpx_mock)
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
    def _install_concurrent_write(
        httpx_mock: HTTPXMock,
        storage_path: Path,
        disk_cookies: list[dict],
        *,
        rotate_sid: bool = False,
    ) -> None:
        """Write a sibling state during the POST, before the real typed merge."""

        def _respond(_request: httpx.Request) -> httpx.Response:
            _write_storage(storage_path, disk_cookies)
            response = _make_psidts_response()
            headers = list(response["headers"])
            if rotate_sid:
                headers.append(("Set-Cookie", "SID=our-rotated-sid; Domain=.google.com; Path=/"))
            return httpx.Response(
                status_code=response["status_code"],
                headers=headers,
                content=response["content"],
            )

        httpx_mock.add_callback(_respond, url=_ROTATE_URL_RE)

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
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        sibling_cookies = [
            {**cookie, "value": "sibling-sid"} if cookie["name"] == "SID" else cookie
            for cookie in _RECOVERABLE_COOKIES
        ]
        self._install_concurrent_write(
            httpx_mock,
            storage_path,
            sibling_cookies,
            rotate_sid=True,
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
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        sibling_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "sibling_fresh_value",
            "domain": ".google.com",
            "path": "/",
            "expires": 4_102_444_800,  # comfortably in the future
        }
        self._install_concurrent_write(
            httpx_mock,
            storage_path,
            [*_RECOVERABLE_COOKIES, sibling_psidts],
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
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        unusable_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "",
            "domain": ".google.com",
            "path": "/",
        }
        self._install_concurrent_write(
            httpx_mock,
            storage_path,
            [*_RECOVERABLE_COOKIES, unusable_psidts],
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
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)
        expired_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "stale_value",
            "domain": ".google.com",
            "path": "/",
            "expires": 1_000_000_000,  # 2001 — comfortably in the past
        }
        self._install_concurrent_write(
            httpx_mock,
            storage_path,
            [*_RECOVERABLE_COOKIES, expired_psidts],
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
    def test_malformed_expires_on_a_sibling_row_keeps_the_actionable_error(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A corrupt ``expires`` must not turn an auth diagnostic into a traceback.

        Regression for a crash reachable on the pre-#2057 code: the jar builder
        in ``_attempt_rotation`` converted every allowed-domain entry with no
        guard, and ``http.cookiejar.Cookie.__init__`` coerces via
        ``int(float(expires))``. One malformed sibling row — not even the PSIDTS
        row — raised ``ValueError: could not convert string to float`` from
        inside ``load_auth_from_storage``'s own ``except ValueError:`` handler,
        replacing "Missing required cookies … Run 'notebooklm login'" with a
        converter internal, after the rotation throttle slot had been claimed.
        """
        cookies = _RECOVERABLE_COOKIES + [
            {
                "name": "NID",
                "value": "junk",
                "domain": ".google.com",
                "path": "/",
                "expires": "not-a-timestamp",
            }
        ]
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, cookies)
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=500)

        with pytest.raises(ValueError, match="Missing required cookies") as excinfo:
            auth_module.load_auth_from_storage(storage_path)
        assert "could not convert string to float" not in str(excinfo.value)

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
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_empty_env_var_does_not_fall_back_to_the_default_profile(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """An EMPTY ``NOTEBOOKLM_AUTH_JSON`` must not redirect recovery to a profile file.

        The loader tests env-var *presence* and raises "set but empty" without
        inspecting a cookie; ``_resolve_recovery_path`` used to test
        *truthiness*, so an empty string fell through to the default profile.
        Recovery would then POST and persist rotated cookies to a profile the
        caller had explicitly bypassed by setting the variable at all.
        """
        default_path = tmp_path / "default_storage_state.json"
        _write_storage(default_path, _RECOVERABLE_COOKIES)
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "")
        monkeypatch.setattr(_nb_paths, "get_storage_path", Mock(return_value=default_path))

        assert psidts_recovery._resolve_recovery_path(None) is None
        assert psidts_recovery._recover_psidts_inline(None) is False
        assert _rotate_requests(httpx_mock) == []
        # The bypassed profile is untouched.
        assert json.loads(default_path.read_text(encoding="utf-8"))["cookies"] == (
            _RECOVERABLE_COOKIES
        )

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
        # Drive both call-time default-path lookups through the documented home
        # and profile environment instead of retaining a module patch seam.
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "default")
        resolved = _nb_paths.get_storage_path()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        _write_storage(resolved, _RECOVERABLE_COOKIES)
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        cookies = auth_module.load_auth_from_storage(None)

        assert cookies["__Secure-1PSIDTS"] == "fresh_psidts_value"
        assert resolved == _nb_paths.get_storage_path()


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

    @pytest.mark.no_default_keepalive_mock
    def test_rotation_keeps_a_returned_lsid(self, httpx_mock: HTTPXMock):
        """A rotated ``LSID`` must survive the write-back (#1977 review).

        This path allows the POST on ``APISID``+``SAPISID`` alone, so a rotation
        that *supplies* the missing ``LSID`` is exactly the case worth keeping —
        without it the set still has no valid secondary binding and the recovery
        accomplished nothing. The write-back previously kept only the two PSIDTS
        names and dropped it silently.
        """
        response = _make_psidts_response()
        response["headers"] = list(response["headers"]) + [
            (
                "Set-Cookie",
                "LSID=fresh_lsid_value; Domain=accounts.google.com; "
                "Path=/; Secure; HttpOnly; SameSite=Lax",
            )
        ]
        httpx_mock.add_response(url=_ROTATE_URL_RE, **response)

        cookies = _rookiepy_recoverable()
        assert psidts_recovery.recover_psidts_in_memory(cookies) is True

        lsid = [c for c in cookies if c["name"] == "LSID"]
        assert lsid, "a rotated LSID must be written back into the in-memory list"
        assert lsid[0]["value"] == "fresh_lsid_value"

    @pytest.mark.no_default_keepalive_mock
    def test_recovers_psidts_and_mutates_list_in_place(self, httpx_mock: HTTPXMock):
        cookies = _rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True

        names = {c["name"] for c in cookies}
        assert "__Secure-1PSIDTS" in names
        psidts = next(c for c in cookies if c["name"] == "__Secure-1PSIDTS")
        assert psidts["value"] == "fresh_psidts_value"
        assert psidts["domain"] == ".google.com"

    @pytest.mark.no_default_keepalive_mock
    def test_post_carries_existing_cookies(self, httpx_mock: HTTPXMock):
        cookies = _rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        psidts_recovery.recover_psidts_in_memory(cookies)

        requests = _rotate_requests(httpx_mock)
        assert len(requests) == 1
        header = requests[0].headers.get("cookie", "")
        assert "SID=test_sid" in header
        assert "APISID=test_apisid" in header
        assert "SAPISID=test_sapisid" in header

    @pytest.mark.no_default_keepalive_mock
    def test_no_sid_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = [c for c in _rookiepy_recoverable() if c["name"] != "SID"]
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_psidts_already_present_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = _rookiepy_recoverable() + [
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
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_gate_reads_rookiepy_rows_directly_not_via_conversion(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """Site 4 selects the rookiepy converter; it does not convert the list first.

        ``_rookiepy_entry_to_cookie`` reads snake_case ``http_only`` (the
        storage_state mirror reads camelCase ``httpOnly``), and a rookiepy
        ``expires`` of ``None`` is a session cookie that is NOT pre-mapped to
        ``-1`` the way ``convert_rookiepy_cookies_to_storage_state`` would. Both
        shapes must be honoured on the raw rows.
        """
        session_psidts = {
            "name": "__Secure-1PSIDTS",
            "value": "session_value",
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "http_only": True,
            "expires": None,
        }
        cookies = _rookiepy_recoverable() + [session_psidts]

        # Session expiry -> live -> routes -> no POST.
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert _rotate_requests(httpx_mock) == []

        cookie = psidts_recovery._rookiepy_entry_to_cookie(session_psidts)
        assert cookie.expires is None
        assert cookie.get_nonstandard_attr("HttpOnly") == ""

    @pytest.mark.no_default_keepalive_mock
    def test_withheld_rotation_is_detected_despite_a_pre_existing_psidts(
        self, httpx_mock: HTTPXMock
    ):
        """A pre-existing non-routing PSIDTS must not fake a successful mint.

        Routing the gate made this path newly reachable: an app-host-only PSIDTS
        now fires the POST, and the request jar still carries that cookie. A
        name-only "did the response include PSIDTS?" check would therefore see
        the caller's own pre-existing cookie and report success even when Google
        withheld the rotation — defeating the very detection the surrounding code
        exists for. The check asks whether a PSIDTS now ROUTES to the rotate URL,
        which is also proof of newness: the gate only fired because nothing
        routable was there to begin with.
        """
        cookies = _rookiepy_recoverable() + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "app_host_only",
                "domain": ".notebooklm.google.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            }
        ]
        # 2xx, but no Set-Cookie for PSIDTS — Google withholding the rotation.
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response(include_psidts=False))

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        # The POST did fire — the heal was genuinely needed — but it is correctly
        # reported as failed, and no bogus row was appended.
        assert len([r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]) == 1
        assert [c for c in cookies if c["name"] == "__Secure-1PSIDTS"] == [cookies[-1]]

    @pytest.mark.no_default_keepalive_mock
    def test_app_host_only_psidts_still_fires_the_post(self, httpx_mock: HTTPXMock):
        """A PSIDTS that never reaches ``accounts.google.com`` does not block the heal.

        The ranked gate treated ``.notebooklm.google.com`` as a high-priority
        satisfying row and skipped. It does not route to the POST's host, so the
        session still needs the mint.
        """
        cookies = _rookiepy_recoverable() + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "app_host_only",
                "domain": ".notebooklm.google.com",
                "path": "/",
                "secure": True,
                "http_only": True,
            }
        ]
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response())

        assert psidts_recovery.recover_psidts_in_memory(cookies) is True
        assert len(_rotate_requests(httpx_mock)) == 1

    @pytest.mark.no_default_keepalive_mock
    def test_missing_secondary_binding_returns_false_without_post(self, httpx_mock: HTTPXMock):
        cookies = [c for c in _rookiepy_recoverable() if c["name"] not in {"APISID", "SAPISID"}]
        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert _rotate_requests(httpx_mock) == []

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
        cookies = _rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=401)

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert "__Secure-1PSIDTS" not in {c["name"] for c in cookies}

    @pytest.mark.no_default_keepalive_mock
    def test_200_without_psidts_returns_false(self, httpx_mock: HTTPXMock):
        cookies = _rookiepy_recoverable()
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_psidts_response(include_psidts=False))

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False
        assert "__Secure-1PSIDTS" not in {c["name"] for c in cookies}

    @pytest.mark.no_default_keepalive_mock
    def test_network_error_returns_false(self, httpx_mock: HTTPXMock):
        cookies = _rookiepy_recoverable()
        httpx_mock.add_exception(httpx.ConnectError("simulated network failure"))

        assert psidts_recovery.recover_psidts_in_memory(cookies) is False

    @pytest.mark.no_default_keepalive_mock
    def test_validate_with_recovery_heals_partial_jar(self, httpx_mock: HTTPXMock):
        """End-to-end: validate-with-recovery returns (storage_state, None) after rotation."""
        cookies = _rookiepy_recoverable()
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
        cookies = [c for c in _rookiepy_recoverable() if c["name"] != "SID"]

        storage_state, error = psidts_recovery.validate_with_recovery(cookies)

        assert error is not None
        assert "SID" in str(error)
        # No POST fired (recovery preconditions failed early).
        assert _rotate_requests(httpx_mock) == []
        # storage_state still reflects the (incomplete) extraction attempt.
        assert isinstance(storage_state, dict)


class TestMalformedExpiresAcrossLoaders:
    """A row with an uncoercible ``expires`` must not take a whole profile down.

    ``http.cookiejar.Cookie.__init__`` coerces via ``int(float(expires))``, so a
    single corrupt row raised ``ValueError: could not convert string to float``
    out of every loader — including from *inside* the recovery paths'
    ``except ValueError:`` handlers, where it replaced the actionable
    "Missing required cookies … Run 'notebooklm login'" with a converter
    traceback. Guarding only the recovery module left
    ``NotebookLMClient.from_storage`` still broken, and made it worse: recovery
    would now complete, fire a POST and rewrite the file, while the strict
    loader kept raising the same error on every load, forever.
    """

    @staticmethod
    def _storage_with_bad_row(tmp_path: Path, *, include_psidts: bool) -> Path:
        rows = list(_RECOVERABLE_COOKIES)
        if include_psidts:
            rows.append(
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "fresh",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 4_102_444_800,
                }
            )
        rows.append(
            {
                "name": "NID",
                "value": "junk",
                "domain": ".google.com",
                "path": "/",
                "expires": "not-a-timestamp",
            }
        )
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, rows)
        return storage_path

    def test_strict_loader_skips_the_bad_row_and_loads(self, tmp_path):
        """The `NotebookLMClient.from_storage` path: a healthy profile still loads."""
        storage_path = self._storage_with_bad_row(tmp_path, include_psidts=True)
        jar = auth_cookies.build_httpx_cookies_from_storage(storage_path)
        names = {c.name for c in jar.jar}
        assert "__Secure-1PSIDTS" in names and "SID" in names
        assert "NID" not in names  # unusable row dropped, not fatal

    def test_download_loader_skips_the_bad_row_and_loads(self, tmp_path):
        storage_path = self._storage_with_bad_row(tmp_path, include_psidts=True)
        jar = auth_cookies.load_httpx_cookies(storage_path)
        names = {c.name for c in jar.jar}
        assert "__Secure-1PSIDTS" in names
        assert "NID" not in names

    @pytest.mark.no_default_keepalive_mock
    def test_bad_row_yields_the_actionable_error_not_a_coercion_traceback(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """When the dropped row mattered, the loaders' own validation speaks."""
        storage_path = self._storage_with_bad_row(tmp_path, include_psidts=False)
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=500)

        with pytest.raises(ValueError, match="Missing required cookies") as excinfo:
            auth_cookies.build_httpx_cookies_from_storage(storage_path)
        assert "could not convert string to float" not in str(excinfo.value)


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

        # LSID completes the binding (#1977); without it this set has no valid
        # secondary binding at all and takes the other branch.
        hint = missing_cookies_hint({"SID", "APISID", "SAPISID", "LSID"}, browser_label="firefox")
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
        assert _rotate_requests(httpx_mock) == []

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

        def _remove_destination(_request: httpx.Request) -> httpx.Response:
            storage_path.unlink()
            return httpx.Response(**_make_psidts_response())

        httpx_mock.add_callback(_remove_destination, url=_ROTATE_URL_RE)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert not storage_path.exists()

    @pytest.mark.no_default_keepalive_mock
    def test_save_raising_propagates_as_failure(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """Unexpected exception from ``save_cookies_to_storage`` → False, not propagated."""
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        def _replace_destination_with_directory(_request: httpx.Request) -> httpx.Response:
            storage_path.unlink()
            storage_path.mkdir()
            return httpx.Response(**_make_psidts_response())

        httpx_mock.add_callback(_replace_destination_with_directory, url=_ROTATE_URL_RE)

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        assert storage_path.is_dir()

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
        assert _rotate_requests(httpx_mock) == []

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
            _write_storage(storage_path, post_heal_state["cookies"])
            yield False

        # Two-phase view: precondition sees missing-PSIDTS state, post-flock
        # re-read (via _is_psidts_persisted) sees healed state.
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
        monkeypatch.setattr(psidts_recovery, "_file_lock_try_exclusive", held_lock)

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        # No POST — the holder already did the work.
        assert _rotate_requests(httpx_mock) == []

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
        _stage_storage_reads(
            monkeypatch,
            storage_path,
            _RECOVERABLE_COOKIES,
            post_heal_state["cookies"],
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        # Crucial: no POST — recheck saw the heal before we fired.
        assert _rotate_requests(httpx_mock) == []

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
        post_heal_state = {"cookies": [c for c in _RECOVERABLE_COOKIES if c["name"] != "SID"]}
        _stage_storage_reads(
            monkeypatch,
            storage_path,
            _RECOVERABLE_COOKIES,
            post_heal_state["cookies"],
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is False
        # No POST — recheck saw the broken state and aborted before firing.
        assert _rotate_requests(httpx_mock) == []

    @pytest.mark.no_default_keepalive_mock
    def test_post_flock_recheck_healed_branch_returns_before_sid_is_rechecked(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """The "healed by another process" branch returns BEFORE the SID recheck.

        Pins a deliberate asymmetry. The PSIDTS check precedes the ``SID`` and
        secondary-binding rechecks, so a concurrent write that lands a routable
        PSIDTS *and* drops ``SID`` takes the early ``return True`` without
        revalidating the rest. That is sound rather than a hole: the caller
        simply retries its preflight, which then fails honestly on the missing
        ``SID`` — no POST is fired and nothing is written. Firing here instead
        would send a request Google is guaranteed to reject.
        """
        storage_path = tmp_path / "storage_state.json"
        _write_storage(storage_path, _RECOVERABLE_COOKIES)

        healed_but_sid_lost = [c for c in _RECOVERABLE_COOKIES if c["name"] != "SID"] + [
            {
                "name": "__Secure-1PSIDTS",
                "value": "healed_by_sibling",
                "domain": ".google.com",
                "path": "/",
                "expires": 4_102_444_800,
            }
        ]
        _stage_storage_reads(
            monkeypatch,
            storage_path,
            _RECOVERABLE_COOKIES,
            healed_but_sid_lost,
        )

        assert psidts_recovery._recover_psidts_inline(storage_path) is True
        assert _rotate_requests(httpx_mock) == []
