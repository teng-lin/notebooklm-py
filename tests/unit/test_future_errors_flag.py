"""Unit tests for the ``NOTEBOOKLM_FUTURE_ERRORS`` opt-in preview flag.

The flag lets a process (or a CI job) run the **v0.8.0 error contract** early so
forward-compatibility can be tested before the breaking flips ship (ADR-0019,
umbrella #1346). When on, the three v0.7.0 deprecation *runways* that still warn
today adopt their v0.8.0 *target* behavior:

1. ``<resource>.get()`` raises the matching ``*NotFoundError`` on a miss instead
   of warning-and-returning ``None`` (#1247), routed through
   :func:`notebooklm._lookup.resolve_get`;
2. :class:`~notebooklm._deprecation.MappingCompatMixin` dict-subscript raises
   :class:`TypeError` instead of warning-and-returning the legacy dict value
   (#1251);
3. :func:`~notebooklm._deprecation.deprecated_kwarg` raises :class:`TypeError`
   on the deprecated keyword instead of warning-and-aliasing it (#1254).

Default-off must be byte-identical to current v0.7.0 behavior, and the flag
takes precedence over ``NOTEBOOKLM_QUIET_DEPRECATIONS`` (a runway raises
regardless of quiet; quiet only silences the warn path future mode replaces).
The behavioral conformance for the ``get()`` flip across all five namespaces
lives in ``test_public_api_behavior.py`` (run under both modes); this module
covers the resolver, the two non-``get`` flips, and the precedence rule.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm import _deprecation
from notebooklm._deprecation import (
    MappingCompatMixin,
    deprecated_kwarg,
    future_errors_enabled,
)
from notebooklm._lookup import resolve_get

_FLAG = "NOTEBOOKLM_FUTURE_ERRORS"
_QUIET = "NOTEBOOKLM_QUIET_DEPRECATIONS"
_UNSET = object()


# ---------------------------------------------------------------------------
# future_errors_enabled() — the resolver (mirrors the quiet resolver)
# ---------------------------------------------------------------------------


class TestFutureErrorsResolver:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "Yes", "on", "ON"])
    def test_truthy_values_enable(self, monkeypatch, truthy):
        monkeypatch.setenv(_FLAG, truthy)
        assert future_errors_enabled() is True

    @pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "  "])
    def test_falsy_values_stay_off(self, monkeypatch, falsy):
        monkeypatch.setenv(_FLAG, falsy)
        assert future_errors_enabled() is False

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "  on  ")
        assert future_errors_enabled() is True

    def test_read_live_not_cached(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False
        monkeypatch.setenv(_FLAG, "1")
        assert future_errors_enabled() is True
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False


# ---------------------------------------------------------------------------
# resolve_get() — the shared get()-miss bridge (#1247)
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    """A distinct exception type so ``pytest.raises`` cannot match by accident."""


class TestResolveGet:
    def test_hit_returns_value_no_warn_no_raise_off(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = resolve_get("found", not_found=_Sentinel(), resource="source")
        assert result == "found"

    def test_hit_returns_value_no_raise_on(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        # A hit never raises, even under future-errors: the flip is miss-only.
        result = resolve_get("found", not_found=_Sentinel(), resource="source")
        assert result == "found"

    def test_miss_off_warns_and_returns_none(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        with pytest.warns(DeprecationWarning, match="sources.get()") as record:
            result = resolve_get(None, not_found=_Sentinel(), resource="source")
        assert result is None
        assert len(record) == 1

    def test_miss_on_raises_the_not_found(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(_Sentinel):
                resolve_get(None, not_found=_Sentinel(), resource="source")

    def test_warning_points_at_caller_through_bridge(self, monkeypatch):
        # stacklevel bookkeeping: resolve_get bumps warn_get_returns_none to
        # stacklevel=4 to account for the extra bridge frame
        # (warn (1) -> resolve_get (2) -> public get() (3) -> user (4)). The
        # ``_fake_public_get`` wrapper stands in for the public ``get()`` frame
        # so the warning is attributed to the *caller of get()* — this line —
        # not to _lookup.py / _deprecation.py.
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)

        def _fake_public_get() -> object:
            return resolve_get(None, not_found=_Sentinel(), resource="source")

        with pytest.warns(DeprecationWarning) as record:
            _fake_public_get()
        assert record[0].filename == __file__


# ---------------------------------------------------------------------------
# MappingCompatMixin.__getitem__ — dict-subscript flip (#1251)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CompatProbe(MappingCompatMixin):
    """Minimal mixin subclass mirroring the real typed-dataclass returns."""

    status: str = "completed"

    def to_public_dict(self) -> dict[str, Any]:
        return {"status": self.status}


class TestMappingCompatSubscriptFlip:
    def test_off_warns_and_returns_legacy_value(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        probe = _CompatProbe()
        with pytest.warns(DeprecationWarning, match="dict-style access"):
            value = probe["status"]
        assert value == "completed"

    def test_on_raises_typeerror_not_subscriptable(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        probe = _CompatProbe()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(TypeError, match="not subscriptable"):
                probe["status"]

    def test_on_message_matches_plain_dataclass(self, monkeypatch):
        # The previewed error must read like the real v0.8.0 one: a plain
        # dataclass with no __getitem__ raises "'X' object is not subscriptable".
        @dataclass(frozen=True)
        class _Plain:
            status: str = "completed"

        plain_msg = ""
        try:
            _Plain()["status"]  # type: ignore[index]
        except TypeError as exc:
            plain_msg = str(exc)

        monkeypatch.setenv(_FLAG, "1")
        with pytest.raises(TypeError) as caught:
            _CompatProbe()["status"]
        # Same shape: "'<Type>' object is not subscriptable" (only the type name
        # differs between the plain dataclass and the mixin subclass).
        assert "object is not subscriptable" in plain_msg
        assert "object is not subscriptable" in str(caught.value)

    def test_on_silent_surface_unaffected(self, monkeypatch):
        # Only __getitem__ flips; get/keys/in/iter stay the silent legacy shape.
        monkeypatch.setenv(_FLAG, "1")
        probe = _CompatProbe()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert probe.get("status") == "completed"
            assert "status" in probe
            assert list(probe.keys()) == ["status"]


# ---------------------------------------------------------------------------
# deprecated_kwarg — renamed-keyword flip (#1254)
# ---------------------------------------------------------------------------


class TestDeprecatedKwargFlip:
    def test_off_old_only_warns_and_aliases(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        with pytest.warns(DeprecationWarning, match="deprecated"):
            result = deprecated_kwarg(
                2.0,
                _UNSET,
                old="interval",
                new="initial_interval",
                owner="X.m",
                sentinel=_UNSET,
            )
        assert result == 2.0

    def test_on_old_passed_raises_typeerror(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(TypeError, match="unexpected keyword argument 'interval'"):
                deprecated_kwarg(
                    2.0,
                    _UNSET,
                    old="interval",
                    new="initial_interval",
                    owner="X.m",
                    sentinel=_UNSET,
                )

    def test_on_new_only_still_works(self, monkeypatch):
        # The canonical keyword is unaffected by the flag.
        monkeypatch.setenv(_FLAG, "1")
        result = deprecated_kwarg(
            _UNSET,
            3.0,
            old="interval",
            new="initial_interval",
            owner="X.m",
            sentinel=_UNSET,
        )
        assert result == 3.0

    def test_on_neither_passed_returns_sentinel(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        result = deprecated_kwarg(
            _UNSET,
            _UNSET,
            old="interval",
            new="initial_interval",
            owner="X.m",
            sentinel=_UNSET,
        )
        assert result is _UNSET

    def test_both_passed_still_raises_under_both_modes(self, monkeypatch):
        # The pre-existing both-passed ambiguity TypeError is independent of the
        # flag — it must keep raising whether the preview is on or off.
        for flag in ("1", None):
            if flag is None:
                monkeypatch.delenv(_FLAG, raising=False)
            else:
                monkeypatch.setenv(_FLAG, flag)
            with pytest.raises(TypeError, match="both"):
                deprecated_kwarg(
                    2.0,
                    3.0,
                    old="interval",
                    new="initial_interval",
                    owner="X.m",
                    sentinel=_UNSET,
                )


# ---------------------------------------------------------------------------
# Precedence: FUTURE_ERRORS overrides QUIET_DEPRECATIONS for all three flips
# ---------------------------------------------------------------------------


class TestFutureErrorsTakesPrecedenceOverQuiet:
    def test_resolve_get_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(_Sentinel):
            resolve_get(None, not_found=_Sentinel(), resource="source")

    def test_subscript_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(TypeError, match="not subscriptable"):
            _CompatProbe()["status"]

    def test_deprecated_kwarg_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            deprecated_kwarg(
                2.0,
                _UNSET,
                old="interval",
                new="initial_interval",
                owner="X.m",
                sentinel=_UNSET,
            )

    def test_quiet_alone_silences_warn_path_off(self, monkeypatch):
        # Sanity: with the flag OFF, quiet still just silences (no raise),
        # proving the precedence is specifically the flag's doing.
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.setenv(_QUIET, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert resolve_get(None, not_found=_Sentinel(), resource="source") is None
            assert _CompatProbe()["status"] == "completed"


# ---------------------------------------------------------------------------
# Default-off is byte-identical: the public alias matches the private resolver
# ---------------------------------------------------------------------------


def test_public_alias_matches_private_resolver(monkeypatch):
    for value in ("1", "0", "", "yes", "off"):
        monkeypatch.setenv(_FLAG, value)
        assert future_errors_enabled() == _deprecation._future_errors_enabled()
    monkeypatch.delenv(_FLAG, raising=False)
    assert future_errors_enabled() == _deprecation._future_errors_enabled()
