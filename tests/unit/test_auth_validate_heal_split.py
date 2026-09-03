"""ADR-0031 Stage 2: the validate/heal split preserves the fused behavior.

The three verbs live in ``_auth/psidts_recovery`` since ADR-0033's PR 2.1 folded
the ``browser_cookie_recovery`` leaf into it (the leaf existed only to keep the
recovery module under the old module-size cap, and the pass-through that reached
it was a cycle-breaking lazy import). The seam names are unchanged.

``validate_with_recovery`` fused three operations behind one name — check, try
to fix, check again — with the outcome threaded through an ``initial`` variable
across nested try/except/else. Stage 2 separates the verbs so a caller can ask
the pure question ("is this set usable?") without also firing a network POST
and mutating its rows.

The wrapper has four first-party callers, a ``RefreshDeps`` injection seam, and
an entry in the auth cross-boundary ledger, so "identical behavior" is the
whole contract. These tests pin each arm of the composition — including the
in-place mutation that ``cli/services/login/refresh.py`` depends on, and the
deliberate post-heal asymmetry the pre-split control flow hid.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import cookie_policy as _cookie_policy
from notebooklm._auth import psidts_recovery as recovery


def _rows(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "name": n,
            "value": f"v-{n}",
            "domain": ".google.com",
            "path": "/",
            "http_only": True,
            "secure": True,
            "expires": None,
        }
        for n in names
    ]


_COMPLETE = ("SID", "__Secure-1PSIDTS", "APISID", "SAPISID", "LSID")
_NO_PSIDTS = ("SID", "APISID", "SAPISID", "LSID")
_ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


def _add_rotation_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_ROTATE_URL_RE,
        headers=[
            (
                "Set-Cookie",
                "__Secure-1PSIDTS=fresh; Domain=.google.com; Path=/; "
                "Secure; HttpOnly; SameSite=Lax",
            )
        ],
        content=b'["identity.hfcr",600]',
    )


class TestValidateIsPure:
    """``validate`` answers the question without fixing or mutating anything."""

    def test_valid_set_passes(self) -> None:
        _, result = recovery.validate(_rows(*_COMPLETE))
        assert result.ok is True
        assert result.error is None
        assert result.reason is None

    @pytest.mark.parametrize(
        "names",
        [
            pytest.param(_NO_PSIDTS, id="missing-psidts"),
            pytest.param(("__Secure-1PSIDTS", "APISID", "SAPISID"), id="missing-sid"),
            pytest.param((), id="empty"),
        ],
    )
    def test_invalid_sets_carry_the_policy_error(self, names: tuple[str, ...]) -> None:
        """The result wraps the policy's own error, keeping its closed-enum reason."""
        _, result = recovery.validate(_rows(*names))
        assert result.ok is False
        assert isinstance(result.error, _cookie_policy.RequiredCookieValidationError)
        assert result.reason is not None

    def test_validate_never_mutates_the_caller_rows(self) -> None:
        """The whole point of the split: asking must not change the answer.

        ``validate_with_recovery`` mutates rows in place when it heals, which
        is why callers could not previously ask a read-only question.
        """
        rows = _rows(*_NO_PSIDTS)
        before = copy.deepcopy(rows)
        recovery.validate(rows)
        assert rows == before

    @pytest.mark.no_default_keepalive_mock
    def test_validate_never_fires_a_heal(self, httpx_mock: HTTPXMock) -> None:
        """No network from the pure path, even on a heal-eligible failure."""
        rows = _rows(*_NO_PSIDTS)
        before = copy.deepcopy(rows)
        _, result = recovery.validate(rows)
        assert result.ok is False
        assert rows == before
        assert httpx_mock.get_requests() == []


class TestWrapperComposition:
    """``validate_with_recovery`` == validate → heal → re-check, unchanged."""

    @staticmethod
    def _stub_heal(succeed: bool, adds: tuple[str, ...] = ()):
        def _heal(rows: list[dict[str, Any]]) -> bool:
            if succeed:
                rows.extend(_rows(*adds))
            return succeed

        return _heal

    @pytest.mark.no_default_keepalive_mock
    def test_valid_set_short_circuits_before_healing(self, httpx_mock: HTTPXMock) -> None:
        rows = _rows(*_COMPLETE)
        _, error = recovery.validate_with_recovery(rows)
        assert error is None
        assert httpx_mock.get_requests() == []

    @pytest.mark.no_default_keepalive_mock
    def test_successful_heal_clears_the_error_and_mutates_in_place(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """The in-place mutation is a contract, not an implementation detail.

        ``cli/services/login/refresh.py`` documents and depends on the healed
        rows being visible to the caller afterward.
        """
        _add_rotation_response(httpx_mock)
        rows = _rows(*_NO_PSIDTS)
        state, error = recovery.validate_with_recovery(rows)
        assert error is None
        assert "__Secure-1PSIDTS" in {c["name"] for c in rows}
        assert "__Secure-1PSIDTS" in {c["name"] for c in state["cookies"]}

    def test_heal_that_supplies_nothing_useful_still_reports_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A heal returning True is not proof the set became usable."""
        monkeypatch.setattr(recovery, "recover_psidts_in_memory", self._stub_heal(True, ("NID",)))
        _, error = recovery.validate_with_recovery(_rows(*_NO_PSIDTS))
        assert isinstance(error, _cookie_policy.RequiredCookieValidationError)

    @pytest.mark.no_default_keepalive_mock
    def test_declined_heal_preserves_the_original_error(self, httpx_mock: HTTPXMock) -> None:
        """Missing secondary bindings decline healing before any HTTP request.

        The pre-heal validator has already selected ``psidts_unroutable``;
        declining the heal must preserve that original typed reason and rows.
        """
        rows = _rows("SID")
        rows.append(
            {
                "name": "__Secure-1PSIDTS",
                "value": "host-scoped",
                "domain": "notebook.google.com",
                "path": "/",
                "http_only": True,
                "secure": True,
                "expires": None,
            }
        )
        before = copy.deepcopy(rows)
        _, error = recovery.validate_with_recovery(rows)
        assert isinstance(error, _cookie_policy.RequiredCookieValidationError)
        assert error.reason == "psidts_unroutable"
        assert rows == before  # a declined heal leaves the rows untouched
        assert httpx_mock.get_requests() == []

    @pytest.mark.no_default_keepalive_mock
    def test_post_heal_recheck_is_presence_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        httpx_mock: HTTPXMock,
    ) -> None:
        """Pins the asymmetry the pre-split control flow hid.

        After a successful heal the wrapper re-runs the Tier-1 **presence**
        check only — never the RFC 6265 routing preflight. That is intentional
        and unchanged: the heal exists to mint a PSIDTS that routes, so a
        successful rotation already established what the preflight would
        re-litigate.

        The scenario has to reach the routing check to prove anything, so it
        uses a host-scoped ``__Secure-1PSIDTS``: present (Tier-1 passes) but
        unroutable to ``accounts.google.com`` (``psidts_unroutable``). The
        routing check therefore runs exactly once, pre-heal — and must NOT run
        again afterward. A naive "just re-run validate()" refactor would make
        this two and fail here.
        """
        rows = _rows("SID", "APISID", "SAPISID", "LSID")
        rows.append(
            {
                "name": "__Secure-1PSIDTS",
                "value": "v-psidts",
                "domain": "notebook.google.com",  # host-scoped => unroutable
                "path": "/",
                "http_only": True,
                "secure": True,
                "expires": None,
            }
        )
        _, pre = recovery.validate(list(rows))
        assert pre.ok is False and pre.reason == "psidts_unroutable", (
            "fixture no longer reaches the routing check; the test would be vacuous"
        )

        # The HTTP boundary supplies a properly-scoped PSIDTS, as a real rotation would.
        _add_rotation_response(httpx_mock)
        calls = {"n": 0}
        real_check = recovery._check_routable

        def _counting(rows_: list[dict[str, Any]]) -> recovery.ValidationResult:
            calls["n"] += 1
            return real_check(rows_)

        monkeypatch.setattr(recovery, "_check_routable", _counting)
        _, error = recovery.validate_with_recovery(rows)

        assert error is None, "the heal supplied a routable PSIDTS; the set is usable"
        assert calls["n"] == 1, (
            f"routing preflight ran {calls['n']}x — it must run once pre-heal and "
            "never again post-heal (see the wrapper's asymmetry note)"
        )


class TestHealIsSeparable:
    @pytest.mark.no_default_keepalive_mock
    def test_heal_delegates_to_the_single_rotation_implementation(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """``heal`` is a named seam over the one in-memory rotation (Stage 0)."""
        _add_rotation_response(httpx_mock)
        rows = _rows(*_NO_PSIDTS)
        assert recovery.heal(rows) is True
        assert "__Secure-1PSIDTS" in {row["name"] for row in rows}
