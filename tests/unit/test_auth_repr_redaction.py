"""Unit tests for ``AuthTokens.__repr__`` secret redaction (P1-1).

Secret values that must never appear in ``repr(AuthTokens(...))``:

* Cookie values (``__Secure-1PSID``, ``__Secure-1PSIDTS``, ``SID``, …).
* ``csrf_token`` (the ``SNlM0e`` value).
* ``session_id`` (the ``FdrFJe`` value).
* Cookie values held inside the ``cookie_jar`` (``httpx.Cookies``).
* Cookie values held inside ``cookie_snapshot``.

Non-secret fields that SHOULD appear (so logs remain debuggable):

* The class name and a redacted summary indicating field presence.
* ``authuser`` index and ``account_email`` — neither is credential-equivalent
  and both help identify which profile is involved.
* ``storage_path`` (typically already in user-facing log lines).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from notebooklm.auth import AuthTokens

_SECRET_PSID = "secret-psid-value-deadbeefcafe"
_SECRET_PSIDTS = "secret-psidts-cafefeedface"
_SECRET_SID = "secret-sid-value-12345"
_SECRET_CSRF = "secret-csrf-token-SNlM0e-abc"
_SECRET_SESSION = "secret-session-id-FdrFJe-xyz"


def _build_auth_with_secrets() -> AuthTokens:
    """Build an ``AuthTokens`` populated with the canonical secret fixtures."""
    cookies = {
        ("__Secure-1PSID", ".google.com", "/"): _SECRET_PSID,
        ("__Secure-1PSIDTS", ".google.com", "/"): _SECRET_PSIDTS,
        ("SID", ".google.com", "/"): _SECRET_SID,
    }
    jar = httpx.Cookies()
    jar.set("__Secure-1PSID", _SECRET_PSID, domain=".google.com")
    jar.set("__Secure-1PSIDTS", _SECRET_PSIDTS, domain=".google.com")
    jar.set("SID", _SECRET_SID, domain=".google.com")
    return AuthTokens(
        cookies=cookies,
        csrf_token=_SECRET_CSRF,
        session_id=_SECRET_SESSION,
        storage_path=Path("/tmp/storage.json"),
        cookie_jar=jar,
        authuser=1,
        account_email="alice@example.com",
    )


def test_repr_does_not_leak_cookie_values() -> None:
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    assert _SECRET_PSID not in rendered
    assert _SECRET_PSIDTS not in rendered
    assert _SECRET_SID not in rendered


def test_repr_does_not_leak_csrf_or_session() -> None:
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    assert _SECRET_CSRF not in rendered
    assert _SECRET_SESSION not in rendered


def test_repr_does_not_leak_cookie_jar_values() -> None:
    """The cookie_jar holds the same secrets in a different container."""
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    # ``httpx.Cookies`` iterates names; reach into the underlying
    # ``http.cookiejar.CookieJar`` for the (name, value) pairs.
    for cookie in auth.cookie_jar.jar:  # type: ignore[union-attr]
        assert cookie.value not in rendered, f"Cookie value for {cookie.name} leaked into repr"


def test_repr_identifies_class_and_safe_metadata() -> None:
    """Redacted repr still names the class and shows non-secret debug info."""
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    assert rendered.startswith("AuthTokens(")
    # authuser is not credential-equivalent — surface it for debuggability.
    assert "authuser=1" in rendered
    # account_email is identity-equivalent (sub-PII) but already exposed
    # in user-facing CLI output (`notebooklm status`), so it stays.
    assert "alice@example.com" in rendered


def test_repr_marks_secret_fields_as_redacted() -> None:
    """The redaction is explicit so a reader knows fields exist + are hidden."""
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    assert "redacted" in rendered.lower()
    # The three secret-shaped fields should each show up as redacted.
    assert "csrf_token" in rendered
    assert "session_id" in rendered
    assert "cookies" in rendered


def test_repr_cookie_count_reflects_actual_size() -> None:
    """The placeholder includes the cookie count so reprs aid debugging."""
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    # 3 cookies in the fixture above.
    assert "3" in rendered


def test_repr_handles_empty_cookies_and_no_jar() -> None:
    """Edge case: empty cookies, no jar override — repr should not raise."""
    auth = AuthTokens(
        cookies={},
        csrf_token="",
        session_id="",
    )
    rendered = repr(auth)
    assert rendered.startswith("AuthTokens(")
    assert "redacted" in rendered.lower()


def test_repr_does_not_leak_cookie_snapshot_values() -> None:
    """``cookie_snapshot`` mirrors the live jar and must also be redacted."""
    from notebooklm._auth.storage import snapshot_cookie_jar

    auth = _build_auth_with_secrets()
    snapshot = snapshot_cookie_jar(auth.cookie_jar)  # type: ignore[arg-type]
    auth_with_snapshot = AuthTokens(
        cookies=auth.cookies,
        csrf_token=_SECRET_CSRF,
        session_id=_SECRET_SESSION,
        cookie_jar=auth.cookie_jar,
        cookie_snapshot=snapshot,
    )
    rendered = repr(auth_with_snapshot)
    assert _SECRET_PSID not in rendered
    assert _SECRET_PSIDTS not in rendered
    assert _SECRET_SID not in rendered


@pytest.mark.parametrize(
    "secret",
    [_SECRET_PSID, _SECRET_PSIDTS, _SECRET_SID, _SECRET_CSRF, _SECRET_SESSION],
)
def test_repr_redacts_every_canonical_secret(secret: str) -> None:
    """Belt-and-suspenders: parametrized check across all fixture secrets."""
    auth = _build_auth_with_secrets()
    rendered = repr(auth)
    assert secret not in rendered, f"Secret {secret!r} leaked into repr"
