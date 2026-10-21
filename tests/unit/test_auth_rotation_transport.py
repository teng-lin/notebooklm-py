"""Cookie recovery must use the selected transport, without live credentials."""

from __future__ import annotations

import builtins
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from notebooklm._auth import keepalive, psidts_recovery
from notebooklm._curl_cffi_transport import resolve_sync_transport_factory
from notebooklm._request_context import request_policy_scope

_COOKIES = [
    {"name": name, "value": "synthetic", "domain": ".google.com", "path": "/"}
    for name in ("SID", "APISID", "SAPISID")
]
_HEADERS = [
    ("Set-Cookie", f"{name}=rotated; Domain=.google.com; Path=/; Secure; HttpOnly")
    for name in ("__Secure-1PSIDTS", "__Secure-3PSIDTS")
]


@pytest.fixture
def curl_session(monkeypatch):
    """Replace curl's wire session with a synthetic two-cookie rotation response."""
    requests = pytest.importorskip("curl_cffi.requests")
    session = Mock()
    session.request.return_value = SimpleNamespace(
        status_code=200,
        headers=httpx.Headers(_HEADERS),
        content=b"[]",
    )
    factory = Mock(return_value=session)
    monkeypatch.setattr(requests, "Session", factory)
    return factory, session


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize("source", ["storage", "browser_cookies"])
def test_psidts_recovery_uses_selected_transport(
    source, tmp_path, monkeypatch, httpx_mock, curl_session
):
    """Both recovery entry points use curl and retain both newly minted cookies."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    monkeypatch.setenv("NOTEBOOKLM_IMPERSONATE", "chrome")
    # Negative control: old code reaches HTTPX, never the selected curl session.
    # Stub that path too so the regression cannot send a real Google request.
    httpx_mock.add_response(url=keepalive.KEEPALIVE_ROTATE_URL, headers=_HEADERS, is_optional=True)
    rows = [dict(row) for row in _COOKIES]
    if source == "storage":
        path = tmp_path / "storage_state.json"
        path.write_text(json.dumps({"cookies": rows, "origins": []}))
        assert psidts_recovery._recover_psidts_inline(path)
        rows = json.loads(path.read_text())["cookies"]
    else:
        assert psidts_recovery.recover_psidts_in_memory(rows)

    factory, session = curl_session
    factory.assert_called_once_with(impersonate="chrome")
    session.request.assert_called_once()
    assert session.request.call_args.args == ("POST", keepalive.KEEPALIVE_ROTATE_URL)
    assert session.request.call_args.kwargs["data"] == keepalive._KEEPALIVE_ROTATE_BODY.encode()
    assert httpx_mock.get_requests() == []
    assert {row["name"] for row in rows} >= {"__Secure-1PSIDTS", "__Secure-3PSIDTS"}
    session.close.assert_called_once()


@pytest.mark.parametrize("transport", ["", "httpx"])
def test_default_rotation_transport_remains_httpx(transport, monkeypatch, httpx_mock):
    """The default transport keeps HTTPX behavior and does not mutate the input jar."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", transport)
    jar = httpx.Cookies({"SID": "synthetic"})
    httpx_mock.add_response(url="https://example.test/", headers={"Set-Cookie": "new=value"})
    with keepalive._rotation_http_client(jar) as client:
        response = client.post("https://example.test/")
        assert response.status_code == 200
        assert client.cookies["new"] == "value"
    assert "new" not in jar


def test_rotation_rejects_unknown_transport(monkeypatch):
    """A transport typo fails before any authentication request can be sent."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curlcffi")
    with (
        pytest.raises(ValueError, match="Unknown NOTEBOOKLM_TRANSPORT"),
        keepalive._rotation_http_client(httpx.Cookies()),
    ):
        pass


def test_rotation_respects_bound_transport_policy(monkeypatch, curl_session):
    """The request-bound transport and fingerprint override ambient settings."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "httpx")
    monkeypatch.setenv("NOTEBOOKLM_IMPERSONATE", "firefox")
    policy = SimpleNamespace(
        identity="test-policy",
        environment={"NOTEBOOKLM_TRANSPORT": "curl_cffi", "NOTEBOOKLM_IMPERSONATE": "chrome"},
    )
    with request_policy_scope(policy), keepalive._rotation_http_client(httpx.Cookies()):
        pass
    factory, session = curl_session
    factory.assert_called_once_with(impersonate="chrome")
    session.close.assert_called_once()


def test_rotation_does_not_fall_back_when_curl_is_missing(monkeypatch):
    """An unavailable selected dependency must not silently change the wire transport."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    original_import = builtins.__import__

    def without_curl(name, *args, **kwargs):
        """Simulate only the missing optional curl import, leaving other imports intact."""
        if name == "curl_cffi.requests":
            raise ImportError("synthetic missing optional dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_curl)
    with (
        pytest.raises(ImportError, match="synthetic missing optional dependency"),
        keepalive._rotation_http_client(httpx.Cookies()),
    ):
        pass


@pytest.mark.parametrize("failure", ["network", "status"])
def test_curl_recovery_failure_is_nonfatal_and_preserves_storage(
    failure, tmp_path, monkeypatch, curl_session
):
    """HTTP and transport failures preserve stored cookies and release the session."""
    from curl_cffi.requests import RequestsError

    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    path = tmp_path / "storage_state.json"
    original = json.dumps({"cookies": _COOKIES, "origins": []})
    path.write_text(original)
    _, session = curl_session
    if failure == "network":
        session.request.side_effect = RequestsError("synthetic transport failure")
    else:
        session.request.return_value.status_code = 403

    assert psidts_recovery._recover_psidts_inline(path) is False
    assert path.read_text() == original
    session.close.assert_called_once()


@pytest.mark.parametrize("timeout", [None, 15])
def test_curl_rotation_preserves_request_options(timeout, monkeypatch, curl_session):
    """Forward payload, cookies and timeout without taking over redirect ownership."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    _, session = curl_session
    jar = httpx.Cookies()
    jar.set("SID", "synthetic", domain="accounts.google.com", path="/")
    with keepalive._rotation_http_client(jar) as client:
        response = client.post(
            keepalive.KEEPALIVE_ROTATE_URL,
            content="payload",
            headers={
                "Origin": "https://accounts.google.com",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        assert isinstance(response, httpx.Response)
        assert client.cookies["__Secure-1PSIDTS"] == "rotated"
        assert client.cookies["__Secure-3PSIDTS"] == "rotated"
    assert "__Secure-1PSIDTS" not in jar
    options = session.request.call_args.kwargs
    headers = httpx.Headers(options["headers"])
    assert "SID=synthetic" in headers["Cookie"]
    assert headers["Origin"] == "https://accounts.google.com"
    assert headers["Content-Type"] == "application/json"
    for name in ("user-agent", "accept", "accept-encoding", "connection"):
        assert name not in headers  # curl supplies the selected browser's defaults
    assert options["data"] == b"payload"
    assert options["timeout"] == (None if timeout is None else (timeout, timeout))
    assert options["allow_redirects"] is False  # HTTPX handles redirect cookies
    assert options["discard_cookies"] is True


@pytest.mark.parametrize("source", ["client", "request"])
@pytest.mark.parametrize("name", ["User-Agent", "Accept", "Accept-Encoding", "Connection"])
def test_curl_rotation_preserves_explicit_headers(source, name, monkeypatch, curl_session):
    """Preserve each explicit header while removing the other HTTPX defaults."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    _, session = curl_session
    explicit = {name: "caller-value"}
    kwargs = {"headers": explicit} if source == "client" else {}
    with resolve_sync_transport_factory()(**kwargs) as client:
        client.post(
            keepalive.KEEPALIVE_ROTATE_URL,
            headers=explicit if source == "request" else None,
        )
    headers = httpx.Headers(session.request.call_args.kwargs["headers"])
    assert headers[name] == "caller-value"
    for default in ("user-agent", "accept", "accept-encoding", "connection"):
        if default != name.lower():
            assert default not in headers
