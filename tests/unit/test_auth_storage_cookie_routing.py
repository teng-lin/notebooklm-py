"""Local storage-cookie prerequisites for browser capture and doctor (#2467)."""

from __future__ import annotations

import copy
import json
import time
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from notebooklm import auth
from notebooklm._auth import cookies
from notebooklm.exceptions import ConfigurationError

_APP_URL = "https://notebook.google.com/"
_LEGACY_URL = "https://notebooklm.google.com/"


def _sid(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "SID",
        "value": "synthetic-session",
        "domain": ".google.com",
        "path": "/",
        "expires": -1,
        "secure": True,
        **overrides,
    }


@pytest.mark.parametrize(
    ("row", "url", "expected"),
    [
        pytest.param(_sid(), _APP_URL, True, id="domain-cookie"),
        pytest.param(_sid(), _LEGACY_URL, True, id="domain-cookie-legacy"),
        pytest.param(_sid(domain="notebook.google.com"), _APP_URL, True, id="app-cookie"),
        pytest.param(_sid(domain="notebook.google.com"), _LEGACY_URL, False, id="app-to-alias"),
        pytest.param(_sid(domain="notebooklm.google.com"), _APP_URL, False, id="alias-to-app"),
        pytest.param(_sid(domain="notebooklm.google.com"), _LEGACY_URL, True, id="alias-cookie"),
        pytest.param(_sid(domain=".google.co.uk"), _APP_URL, False, id="regional-domain"),
        pytest.param(_sid(domain="accounts.google.com"), _APP_URL, False, id="accounts-domain"),
        pytest.param(_sid(domain="example.com"), "https://example.com/", False, id="disallowed"),
        pytest.param(_sid(path="/app"), _APP_URL, False, id="wrong-path"),
        pytest.param(_sid(path="/app"), _APP_URL + "app/query", True, id="matching-path"),
        pytest.param(_sid(path="/app"), _APP_URL + "application", False, id="path-boundary"),
        pytest.param(_sid(), "http://notebook.google.com/", False, id="secure-to-http"),
        pytest.param(_sid(secure=False), "http://notebook.google.com/", True, id="http-cookie"),
        pytest.param(_sid(value=""), _APP_URL, False, id="empty-value"),
        pytest.param(_sid(value=None), _APP_URL, False, id="non-string-value"),
        pytest.param(_sid(expires=None), _APP_URL, True, id="none-session-expiry"),
        pytest.param(_sid(expires=0), _APP_URL, False, id="epoch-expiry"),
        pytest.param(_sid(expires=1), _APP_URL, False, id="past-expiry"),
        pytest.param(_sid(expires=-1.0), _APP_URL, False, id="dated-minus-one-float"),
        pytest.param(_sid(expires="-1"), _APP_URL, False, id="dated-minus-one-string"),
        pytest.param(_sid(expires="never"), _APP_URL, False, id="malformed-expiry"),
        pytest.param(_sid(expires=float("nan")), _APP_URL, False, id="nonfinite-expiry"),
        pytest.param(_sid(path=[]), _APP_URL, False, id="malformed-path"),
        pytest.param(_sid(domain=None), _APP_URL, False, id="malformed-domain"),
    ],
)
def test_storage_cookie_routes_with_original_attributes(
    row: dict[str, Any], url: str, expected: bool
) -> None:
    assert cookies._storage_has_routable_cookie({"cookies": [row]}, "SID", url) is expected


def test_storage_cookie_defaults_to_current_personal_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    assert auth._storage_has_routable_cookie(
        {"cookies": [_sid(domain="notebook.google.com")]}, "SID"
    )
    assert not auth._storage_has_routable_cookie(
        {"cookies": [_sid(domain="notebooklm.google.com")]}, "SID"
    )


@pytest.mark.parametrize(
    "host", ["notebook.google.com", "notebooklm.google.com", "notebooklm.cloud.google.com"]
)
def test_storage_cookie_default_obeys_configured_root(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{host}/")
    assert auth._storage_has_routable_cookie({"cookies": [_sid(domain=host)]}, "SID")
    assert not auth._storage_has_routable_cookie(
        {"cookies": [_sid(domain="accounts.google.com")]}, "SID"
    )


def test_explicit_cookie_route_does_not_resolve_configured_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://invalid.example/")
    state = {"cookies": [_sid()]}
    assert auth._storage_has_routable_cookie(state, "SID", _APP_URL)
    with pytest.raises(ConfigurationError, match="NOTEBOOKLM_BASE_URL"):
        auth._storage_has_routable_cookie(state, "SID")


@pytest.mark.parametrize(
    "configured",
    [
        "https://synthetic-credential@notebook.google.com/",
        "https://notebook.google.com:synthetic-credential/",
        "https://invalid.example/?token=synthetic-credential",
    ],
)
def test_default_cookie_route_classifies_invalid_config_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", configured)

    with pytest.raises(ConfigurationError, match="NOTEBOOKLM_BASE_URL") as caught:
        auth._storage_has_routable_cookie({"cookies": [_sid()]}, "SID")

    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value) == str(caught.value.__cause__)
    assert "synthetic-credential" not in str(caught.value)


def test_inline_parser_facade_uses_supplied_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "invalid ambient value")
    state = {"cookies": [_sid()], "origins": []}
    assert auth._load_storage_state_from_env_value(json.dumps(state)) == state


def test_storage_cookie_expiry_uses_canonical_units() -> None:
    # Browser imports may carry milliseconds rather than seconds. Use the
    # existing normalization rather than interpreting these as far-future dates.
    past_ms = int(time.time() - 3600) * 1000
    future_ms = int(time.time() + 3600) * 1000
    assert not cookies._storage_has_routable_cookie(
        {"cookies": [_sid(expires=past_ms)]}, "SID", _APP_URL
    )
    assert cookies._storage_has_routable_cookie(
        {"cookies": [_sid(expires=future_ms)]}, "SID", _APP_URL
    )


@pytest.mark.parametrize("raw_rows", [None, {}, "SID", [None, [], {}, 3]])
def test_storage_cookie_rejects_unusable_rows(raw_rows: Any) -> None:
    assert not cookies._storage_has_routable_cookie({"cookies": raw_rows}, "SID", _APP_URL)


@pytest.mark.parametrize("valid_first", [False, True])
def test_unusable_cookie_siblings_do_not_hide_a_routable_sid(valid_first: bool) -> None:
    invalid_rows = [
        None,
        {"name": "NID", "domain": ".google.com", "expires": "broken"},
        _sid(domain=".google.co.uk", expires="broken"),
        _sid(path="/app", expires=1),
        _sid(domain="accounts.google.com", value=""),
    ]
    rows = [_sid(), *invalid_rows] if valid_first else [*invalid_rows, _sid()]
    assert cookies._storage_has_routable_cookie({"cookies": rows}, "SID", _APP_URL)


@pytest.mark.parametrize("first_expiry", [1, -1])
def test_duplicate_identity_follows_runtime_first_usable_row(first_expiry: int) -> None:
    other_expiry = -1 if first_expiry == 1 else 1
    state = {"cookies": [_sid(expires=first_expiry), _sid(expires=other_expiry)]}
    assert cookies._storage_has_routable_cookie(state, "SID", _APP_URL) is (first_expiry == -1)


def test_cookie_projection_ignores_other_names() -> None:
    state = {"cookies": [_sid(name="NID")]}
    assert not cookies._storage_has_routable_cookie(state, "SID", _APP_URL)
    assert cookies._storage_has_routable_cookie(state, "NID", _APP_URL)


def test_sid_only_projection_has_no_loader_recovery_or_io(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("local routing must not load or send"))
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)

    state = {"cookies": [_sid()], "origins": [{"origin": _APP_URL, "localStorage": []}]}
    before = copy.deepcopy(state)
    assert auth._storage_has_routable_cookie(MappingProxyType(state), "SID", _APP_URL)
    assert state == before
    forbidden.assert_not_called()
