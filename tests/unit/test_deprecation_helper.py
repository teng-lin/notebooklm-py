"""Unit tests for the ``notebooklm._deprecation`` warn helper + quiet gate."""

import inspect
import warnings
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from notebooklm import AuthTokens
from notebooklm._deprecation import (
    DEPRECATION_SPECS,
    DeprecationSpec,
    deprecations_quiet,
    warn_deprecated,
    warn_registered_deprecation,
)

_FROM_STORAGE_MESSAGE = (
    "AuthTokens.from_storage(...) is deprecated; use "
    "notebooklm.NotebookLMClient.from_storage(...) and access client.auth within the managed "
    "client lifecycle instead. It will be removed in v1.0."
)
_SYNC_CONSTRUCTION_MESSAGE = (
    "Constructing AuthTokens(..., storage_path=..., cookie_jar=None) is deprecated because it "
    "performs synchronous storage/recovery I/O; use "
    "notebooklm.NotebookLMClient.from_storage(...) and access client.auth within the managed "
    "client lifecycle instead. It will be removed in v1.0."
)
_FLAT_COOKIES_MESSAGE = (
    "AuthTokens.flat_cookies is deprecated because its name-only projection discards domain/path "
    "siblings; use AuthTokens.jar for bootstrap cookie questions and managed NotebookLMClient "
    "request APIs for HTTP. It will be removed in v1.0."
)
_RPC_CALL_WEB_MESSAGE = (
    "NotebookLMClient.rpc_call(...) is deprecated; use client.raw.call(...) on a Web-selected "
    "client instead. It will be removed in v1.0."
)
_RPC_CALL_ANDROID_MESSAGE = (
    "NotebookLMClient.rpc_call(...) is deprecated and crosses from Android into a lazy Web "
    "compatibility transport; use client.raw.unary(...) or client.raw.unary_stream(...) for "
    "Android methods, or client.raw.call(...) on a separate Web-selected client. It will be "
    "removed in v1.0."
)


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "secret", "__Secure-1PSIDTS": "secret-ts"},
        csrf_token="csrf",
        session_id="session",
    )


def test_auth_storage_registry_is_exact_frozen_and_immutable() -> None:
    assert tuple(DEPRECATION_SPECS) == (
        "auth_tokens_from_storage",
        "auth_tokens_sync_storage_construction",
        "auth_tokens_flat_cookies",
        "client_rpc_call_web",
        "client_rpc_call_android",
    )
    assert [field.name for field in fields(DeprecationSpec)] == [
        "key",
        "message",
        "category",
        "replacement",
        "since",
        "removal",
        "stacklevel",
    ]
    expected = {
        "auth_tokens_from_storage": (
            _FROM_STORAGE_MESSAGE,
            "notebooklm.NotebookLMClient.from_storage",
            3,
        ),
        "auth_tokens_sync_storage_construction": (
            _SYNC_CONSTRUCTION_MESSAGE,
            "notebooklm.NotebookLMClient.from_storage",
            4,
        ),
        "auth_tokens_flat_cookies": (
            _FLAT_COOKIES_MESSAGE,
            "notebooklm.AuthTokens.jar",
            3,
        ),
        "client_rpc_call_web": (
            _RPC_CALL_WEB_MESSAGE,
            "notebooklm.raw.WebRawAPI.call",
            3,
        ),
        "client_rpc_call_android": (
            _RPC_CALL_ANDROID_MESSAGE,
            "notebooklm.raw.AndroidRawAPI.unary",
            3,
        ),
    }
    for key, spec in DEPRECATION_SPECS.items():
        message, replacement, stacklevel = expected[key]
        assert spec == DeprecationSpec(
            key=key,
            message=message,
            category=DeprecationWarning,
            replacement=replacement,
            since="0.9.0" if key.startswith("client_rpc_call") else "0.8.1",
            removal="1.0",
            stacklevel=stacklevel,
        )
    with pytest.raises(TypeError):
        DEPRECATION_SPECS["extra"] = DEPRECATION_SPECS["auth_tokens_from_storage"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        DEPRECATION_SPECS["auth_tokens_from_storage"].key = "changed"  # type: ignore[misc]


def test_direct_flat_cookies_access_warns_once_at_public_caller() -> None:
    auth = _auth_tokens()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        frame = inspect.currentframe()
        assert frame is not None
        caller_line = frame.f_lineno + 1
        assert auth.flat_cookies["SID"] == "secret"

    assert len(caught) == 1
    assert str(caught[0].message) == _FLAT_COOKIES_MESSAGE
    assert caught[0].filename == __file__
    assert caught[0].lineno == caller_line


def test_flat_cookies_quiet_gate_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _auth_tokens()
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert auth.flat_cookies["SID"] == "secret"


def test_other_cookie_compatibility_and_dataclass_operations_stay_quiet() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        auth = _auth_tokens()
        assert auth.cookies
        assert auth.cookie_jar is not None
        assert auth.jar
        assert auth.cookie_header
        assert auth.cookie_header_for("https://notebook.google.com/")
        assert repr(auth)
        assert auth == auth
        assert replace(auth) == auth


def test_registered_emitter_uses_live_quiet_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warn_registered_deprecation("auth_tokens_from_storage")
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS")
    with pytest.warns(DeprecationWarning, match="NotebookLMClient.from_storage"):
        warn_registered_deprecation("auth_tokens_from_storage")


def test_registered_emitter_rejects_unknown_key_without_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(KeyError, match="not_registered"):
            warn_registered_deprecation("not_registered")
    assert caught == []


class TestWarnDeprecated:
    """The generic gated primitive (issue #1369)."""

    def test_emits_deprecation_warning_with_message(self):
        with pytest.warns(DeprecationWarning, match="old thing is deprecated") as record:
            warn_deprecated("old thing is deprecated", removal="1.0")
        assert len(record) == 1
        assert "v1.0" in str(record[0].message)

    def test_appends_removal_version_when_absent(self):
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Bare message with no version.", removal="0.8.0")
        assert "v0.8.0" in str(record[0].message)

    def test_does_not_duplicate_removal_when_message_already_names_it(self):
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Removed in v1.0 already.", removal="1.0")
        msg = str(record[0].message)
        assert msg.count("v1.0") == 1

    def test_no_removal_emits_message_verbatim(self):
        # ``warn_deprecated(removal=None)`` emits the message verbatim (no
        # synthesized removal-version clause). The former removal=None callers
        # (NotebooksAPI.share(), ambiguous poll) were removed in v0.8.0 (#1363);
        # awaiting from_storage(...) remains a removal=None caller.
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Permanent shim warning with no version.", removal=None)
        msg = str(record[0].message)
        assert msg == "Permanent shim warning with no version."
        assert "removed" not in msg.lower()

    def test_quiet_env_suppresses_warning(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would fail the test
            warn_deprecated("should be silent", removal="1.0")

    def test_quiet_env_unset_still_warns(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
        with pytest.warns(DeprecationWarning):
            warn_deprecated("loud by default", removal="1.0")


class TestDeprecationsQuiet:
    """The ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate (read live)."""

    def test_quiet_env_suppresses_warn_deprecated(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        assert deprecations_quiet() is True
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # would fail if a warning fired
            warn_deprecated("silent under quiet", removal="1.0")

    def test_quiet_env_unset_is_not_quiet(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
        assert deprecations_quiet() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_quiet_env_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", value)
        assert deprecations_quiet() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "2"])
    def test_quiet_env_falsey_spellings(self, monkeypatch, value):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", value)
        assert deprecations_quiet() is False
