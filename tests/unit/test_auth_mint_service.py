"""Direct behavior matrix for the stateless master-token network service."""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import re
import sys
import threading
import traceback
import types
import weakref
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from notebooklm import auth
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._auth import master_token
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import (
    _CHROMECAST_OAUTH_SPEC,
    _KEEPALIVE_POKE_TIMEOUT,
    _KEEPALIVE_ROTATE_BODY,
    _KEEPALIVE_ROTATE_HEADERS,
    KEEPALIVE_ROTATE_URL,
    MintedOAuthToken,
    MintService,
    OAuthClientSpec,
    OAuthMintError,
    _MintError,
    _parse_oauth_expiry,
    _perform_oauth,
    _rotate_post,
    _rotate_post_sync,
)
from notebooklm.exceptions import MissingDependencyError

_OAUTHLOGIN_RE = re.compile(r"^https://accounts\.google\.com/OAuthLogin")
_MERGESESSION_RE = re.compile(r"^https://accounts\.google\.com/MergeSession")
_ROTATE_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")
_LOGGER_NAMES = ("urllib3", "requests", "urllib3.connectionpool")


@pytest.fixture
def fake_gpsoauth(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a recording external dependency without patching an auth module."""
    module = types.ModuleType("gpsoauth")
    module.exchange_return = {"Token": "aas_et/MASTER"}
    module.oauth_return = {"Auth": "ya29.FAKE"}
    module.exchange_calls = []
    module.oauth_calls = []
    module.exchange_error = None
    module.oauth_error = None

    def exchange_token(email: object, oauth_token: object, android_id: object) -> object:
        module.exchange_calls.append((email, oauth_token, android_id))
        if module.exchange_error is not None:
            raise module.exchange_error
        return module.exchange_return

    def perform_oauth(*args: object, **kwargs: object) -> object:
        module.oauth_calls.append((args, kwargs))
        if module.oauth_error is not None:
            raise module.oauth_error
        return module.oauth_return

    module.exchange_token = exchange_token
    module.perform_oauth = perform_oauth
    monkeypatch.setitem(sys.modules, "gpsoauth", module)
    return module


def _cookie_headers(*rows: str) -> list[tuple[str, str]]:
    return [("set-cookie", row) for row in rows]


def _valid_merge_headers() -> list[tuple[str, str]]:
    return _cookie_headers(
        "SID=sid; Domain=.google.com; Path=/; Secure; HttpOnly",
        "APISID=apisid; Domain=.google.com; Path=/auth; Secure",
        "SAPISID=sapisid; Domain=.google.com; Path=/; Secure; "
        "Expires=Wed, 09 Jun 2038 10:18:14 GMT",
        "HOSTONLY=host; Path=/host; HttpOnly",
    )


def _add_success_responses(httpx_mock: Any, *, merge_status: int = 200) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="  APh-UBERAUTH  ")
    httpx_mock.add_response(
        url=_MERGESESSION_RE,
        status_code=merge_status,
        headers=_valid_merge_headers(),
    )
    httpx_mock.add_response(
        url=_ROTATE_RE,
        headers=_cookie_headers(
            "__Secure-1PSIDTS=fresh; Domain=.google.com; Path=/; Secure; HttpOnly"
        ),
    )


def _assert_chain(
    error: BaseException,
    *,
    cause: BaseException | None,
    context: BaseException | None,
    suppress: bool,
) -> None:
    assert error.__cause__ is cause
    assert error.__context__ is context
    assert error.__suppress_context__ is suppress


def _chain_objects(error: BaseException) -> list[BaseException]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return result


def _assert_public_projection_has_no_private_error(error: BaseException) -> None:
    assert not any(isinstance(item, _MintError) for item in _chain_objects(error))
    current = error.__traceback__
    while current is not None:
        assert not any(
            isinstance(value, _MintError) for value in current.tb_frame.f_locals.values()
        )
        current = current.tb_next


def _assert_traceback_does_not_retain_secret(error: BaseException, secret: str) -> None:
    """Pin secrets out of frames for exceptions that intentionally escape raw."""
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_globals.get("__name__", "").startswith("notebooklm."):
            for value in current.tb_frame.f_locals.values():
                assert value != secret
                if isinstance(value, MasterToken):
                    assert value.secret != secret
        current = current.tb_next


def _value_retains_secret(value: object, secret: str, seen: set[int] | None = None) -> bool:
    """Inspect the concrete carrier shapes used by auth exceptions and frames."""
    if value == secret:
        return True
    if isinstance(value, MasterToken):
        return value.secret == secret
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, dict):
        return any(
            _value_retains_secret(item, secret, seen) for pair in value.items() for item in pair
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_value_retains_secret(item, secret, seen) for item in value)
    return False


def _assert_error_graph_does_not_retain_secret(error: BaseException, secret: str) -> None:
    """Check exception text/fields, every chain node, and every traceback local."""
    for current_error in _chain_objects(error):
        assert secret not in str(current_error)
        assert secret not in repr(current_error)
        assert not _value_retains_secret(current_error.args, secret)
        current_traceback = current_error.__traceback__
        while current_traceback is not None:
            if current_traceback.tb_frame.f_globals.get("__name__", "").startswith("notebooklm."):
                assert not any(
                    _value_retains_secret(value, secret)
                    for value in current_traceback.tb_frame.f_locals.values()
                )
            current_traceback = current_traceback.tb_next


def _adapter_traceback_retains(
    error: BaseException,
    target: BaseException,
    adapter_name: str,
) -> bool:
    current = error.__traceback__
    while current is not None:
        frame = current.tb_frame
        if frame.f_code.co_name == adapter_name:
            return any(value is target for value in frame.f_locals.values())
        current = current.tb_next
    raise AssertionError(f"adapter frame {adapter_name!r} missing from traceback")


class _RawRecordHandler(logging.Handler):
    """Capture scalar logging arguments before the package redactor clears them."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[tuple[object, object, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.entries.append((record.msg, record.args, record.getMessage()))


def test_service_and_error_shapes_are_exact_and_stateless() -> None:
    service = MintService()

    assert vars(service) == {}
    assert str(inspect.signature(MintService)) == "()"
    assert str(inspect.signature(MintService.exchange)) == (
        "(self, email: 'str', oauth_token: 'str', android_id: 'str') -> 'MasterToken'"
    )
    assert str(inspect.signature(MintService.mint_oauth)) == (
        "(self, master_token: 'MasterToken', spec: 'OAuthClientSpec') -> 'MintedOAuthToken'"
    )
    assert str(inspect.signature(MintService.mint)) == (
        "(self, token: 'MasterToken') -> 'httpx.Cookies'"
    )
    assert _MintError.__dict__.keys() >= {"__module__"}
    assert _MintError("safe").__dict__ == {}
    assert OAuthMintError.__dict__.keys() >= {"__module__"}
    assert OAuthMintError("safe").__dict__ == {}


def test_oauth_value_shapes_are_immutable_and_secret_safe() -> None:
    spec = OAuthClientSpec("oauth2:scope", "example.app", "certificate")
    minted = MintedOAuthToken("ya29.MINTED-SECRET", 2_000_000_000)

    assert repr(spec) == (
        "OAuthClientSpec(service='oauth2:scope', app='example.app', client_sig='certificate')"
    )
    assert repr(minted) == "MintedOAuthToken(expires_at=2000000000)"
    assert "ya29.MINTED-SECRET" not in repr(minted)
    with pytest.raises(AttributeError):
        spec.app = "changed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        minted.token = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("1700000000", 1_700_000_000),
        ("0007", 7),
        (1700000000, None),
        (True, None),
        ("١٧٠٠٠٠٠٠٠٠", None),
        ("7.0", None),
        ("9" * 11, None),
        ("9" * 10_000, None),
    ],
)
def test_optional_oauth_expiry_parses_only_server_unix_seconds(
    raw: object, expected: int | None
) -> None:
    assert _parse_oauth_expiry(raw) == expected


def test_exchange_preserves_arguments_converts_truthy_secret_and_redacts(
    fake_gpsoauth: types.ModuleType,
) -> None:
    fake_gpsoauth.exchange_return = {"Token": 73}

    token = MintService().exchange("person@example.com", "oauth-secret", "android-1")

    assert token == MasterToken(email="person@example.com", android_id="android-1", secret="73")
    assert fake_gpsoauth.exchange_calls == [("person@example.com", "oauth-secret", "android-1")]
    assert "73" not in repr(token)
    assert "oauth-secret" not in repr(token)


@pytest.mark.parametrize("value", [None, "", 0, False])
def test_exchange_rejects_every_falsy_token_with_empty_chain(
    fake_gpsoauth: types.ModuleType, value: object
) -> None:
    fake_gpsoauth.exchange_return = {"Token": value, "Error": ["odd", 7]}

    with pytest.raises(_MintError) as captured:
        MintService().exchange("person@example.com", "oauth-secret", "android-1")

    assert str(captured.value) == (
        "exchange_token rejected the oauth_token (Error=['odd', 7]). "
        "The oauth_token is single-use and short-lived — re-capture it."
    )
    _assert_chain(captured.value, cause=None, context=None, suppress=False)
    assert "oauth-secret" not in str(captured.value)


def test_exchange_malformed_result_escapes_raw_attribute_error(
    fake_gpsoauth: types.ModuleType,
) -> None:
    fake_gpsoauth.exchange_return = object()

    with pytest.raises(AttributeError):
        MintService().exchange("person@example.com", "oauth-secret", "android-1")


def test_exchange_dependency_error_and_public_projection_preserve_parent_chain(
    fake_gpsoauth: types.ModuleType,
) -> None:
    original = RuntimeError("dependency failed safely")
    fake_gpsoauth.exchange_error = original

    with pytest.raises(_MintError) as lower:
        MintService().exchange("person@example.com", "oauth-secret", "android-1")
    assert str(lower.value) == "exchange_token failed (network or gpsoauth error)."
    _assert_chain(lower.value, cause=original, context=original, suppress=True)

    with pytest.raises(master_token.MasterTokenError) as public:
        master_token.exchange_master_token("person@example.com", "oauth-secret", "android-1")
    assert str(public.value) == str(lower.value)
    _assert_chain(public.value, cause=original, context=original, suppress=True)
    _assert_public_projection_has_no_private_error(public.value)
    assert "oauth-secret" not in str(public.value)


def test_sync_exchange_projection_ignores_active_outer_exception(
    fake_gpsoauth: types.ModuleType,
) -> None:
    original = RuntimeError("dependency failed safely")

    def fail_without_inheriting_caller(*_args: object) -> None:
        try:
            raise original
        except RuntimeError:
            original.__context__ = None
            raise

    fake_gpsoauth.exchange_token = fail_without_inheriting_caller
    outer = RuntimeError("OUTER-OAUTH-SECRET")

    try:
        raise outer
    except RuntimeError:
        with pytest.raises(master_token.MasterTokenError) as public:
            master_token.exchange_master_token("person@example.com", "oauth-secret", "android-1")

    _assert_chain(public.value, cause=original, context=original, suppress=True)
    _assert_public_projection_has_no_private_error(public.value)
    assert not _adapter_traceback_retains(public.value, outer, "exchange_master_token")
    rendered = "".join(traceback.format_exception(public.value))
    assert "OUTER-OAUTH-SECRET" not in rendered


def test_missing_dependency_bypasses_private_and_public_auth_error_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "gpsoauth", None)
    oauth_secret = "OAUTH-SECRET-FOR-TRACEBACK"

    with pytest.raises(MissingDependencyError) as lower:
        MintService().exchange("person@example.com", oauth_secret, "android-1")
    assert "pip install 'notebooklm-py[headless]'" in str(lower.value)
    assert isinstance(lower.value.__cause__, ImportError)
    _assert_chain(
        lower.value,
        cause=lower.value.__cause__,
        context=lower.value.__cause__,
        suppress=True,
    )
    _assert_traceback_does_not_retain_secret(lower.value, oauth_secret)

    with pytest.raises(MissingDependencyError) as public:
        master_token.exchange_master_token("person@example.com", oauth_secret, "android-1")
    assert "pip install 'notebooklm-py[headless]'" in str(public.value)
    assert isinstance(public.value.__cause__, ImportError)
    _assert_chain(
        public.value,
        cause=public.value.__cause__,
        context=public.value.__cause__,
        suppress=True,
    )
    assert classify(public.value).category is ErrorCategory.DEPENDENCY
    assert not isinstance(public.value, master_token.MasterTokenError | _MintError)
    _assert_traceback_does_not_retain_secret(public.value, oauth_secret)


@pytest.mark.parametrize("raises", [False, True], ids=("success", "failure"))
def test_exchange_logger_levels_are_warning_during_call_and_restored_exactly(
    fake_gpsoauth: types.ModuleType, raises: bool
) -> None:
    saved = {name: logging.getLogger(name).level for name in _LOGGER_NAMES}
    initial = {
        "urllib3": logging.DEBUG,
        "requests": logging.ERROR,
        "urllib3.connectionpool": logging.NOTSET,
    }
    observed: list[tuple[int, int, int]] = []

    def exchange_token(*_args: object) -> dict[str, str]:
        observed.append(tuple(logging.getLogger(name).level for name in _LOGGER_NAMES))
        if raises:
            raise RuntimeError("safe failure")
        return {"Token": "aas_et/MASTER"}

    fake_gpsoauth.exchange_token = exchange_token
    try:
        for name, level in initial.items():
            logging.getLogger(name).setLevel(level)
        if raises:
            with pytest.raises(_MintError):
                MintService().exchange("e@example.com", "oauth", "android")
        else:
            MintService().exchange("e@example.com", "oauth", "android")
        assert observed == [(logging.WARNING, logging.WARNING, logging.WARNING)]
        assert {name: logging.getLogger(name).level for name in _LOGGER_NAMES} == initial
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_logger_suppression_mutex_serializes_concurrent_exchanges(
    fake_gpsoauth: types.ModuleType,
) -> None:
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    call_lock = threading.Lock()
    failures: list[BaseException] = []

    def exchange_token(*_args: object) -> dict[str, str]:
        nonlocal calls
        with call_lock:
            calls += 1
            number = calls
        if number == 1:
            first_entered.set()
            assert release_first.wait(2.0)
        else:
            second_entered.set()
        assert all(logging.getLogger(name).level == logging.WARNING for name in _LOGGER_NAMES)
        return {"Token": f"token-{number}"}

    def run() -> None:
        try:
            MintService().exchange("e@example.com", "oauth", "android")
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            failures.append(exc)

    fake_gpsoauth.exchange_token = exchange_token
    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert first_entered.wait(2.0)
    second.start()
    try:
        assert not second_entered.wait(0.1)
    finally:
        release_first.set()
        first.join(2.0)
        second.join(2.0)
    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert failures == []


@pytest.mark.asyncio
async def test_mint_oauth_uses_exact_typed_spec_and_returns_sanitized_value(
    fake_gpsoauth: types.ModuleType,
) -> None:
    spec = OAuthClientSpec(
        service="oauth2:scope-one scope-two",
        app="example.android.app",
        client_sig="0123456789abcdef",
    )
    master_secret = "aas_et/MASTER-SECRET"
    bearer_secret = "ya29.MINTED-BEARER-SECRET-1234567890"
    fake_gpsoauth.oauth_return = {"Auth": bearer_secret, "Expiry": "2000000000"}

    minted = await MintService().mint_oauth(
        MasterToken("person@example.com", "android-1", master_secret), spec
    )

    assert minted.token == bearer_secret
    assert minted.expires_at == 2_000_000_000
    assert fake_gpsoauth.oauth_calls == [
        (
            ("person@example.com", master_secret, "android-1"),
            {
                "service": "oauth2:scope-one scope-two",
                "app": "example.android.app",
                "client_sig": "0123456789abcdef",
            },
        )
    ]
    assert master_secret not in repr(minted)
    assert bearer_secret not in repr(minted)


@pytest.mark.parametrize(
    "response", [{"Auth": "ya29.SECRET"}, {"Auth": "ya29.SECRET", "Expiry": "bad"}]
)
@pytest.mark.asyncio
async def test_mint_oauth_does_not_invent_expiry(
    fake_gpsoauth: types.ModuleType, response: dict[str, str]
) -> None:
    fake_gpsoauth.oauth_return = response

    minted = await MintService().mint_oauth(
        MasterToken("person@example.com", "android-1", "aas_et/MASTER"),
        OAuthClientSpec("oauth2:scope", "example.app", "certificate"),
    )

    assert minted.expires_at is None


@pytest.mark.asyncio
async def test_mint_oauth_errors_expose_no_input_or_returned_credentials(
    fake_gpsoauth: types.ModuleType,
) -> None:
    master_secret = "aas_et/MASTER-SECRET"
    bearer_secret = "ya29.MINTED-BEARER-SECRET-1234567890"
    fake_gpsoauth.oauth_return = {
        "Auth": "",
        "Error": f"rejected {master_secret} {bearer_secret}",
    }

    with pytest.raises(OAuthMintError) as rejected:
        await MintService().mint_oauth(
            MasterToken("person@example.com", "android-1", master_secret),
            OAuthClientSpec("oauth2:scope", "example.app", "certificate"),
        )

    _assert_error_graph_does_not_retain_secret(rejected.value, master_secret)
    _assert_error_graph_does_not_retain_secret(rejected.value, bearer_secret)
    assert str(rejected.value) == (
        "perform_oauth rejected the master token. "
        "Re-bootstrap with `notebooklm login --master-token`."
    )
    _assert_chain(rejected.value, cause=None, context=None, suppress=False)


@pytest.mark.asyncio
async def test_mint_oauth_discards_secret_bearing_dependency_error_and_traceback(
    fake_gpsoauth: types.ModuleType,
) -> None:
    master_secret = "aas_et/WORKER-MASTER-SECRET"
    bearer_secret = "ya29.WORKER-BEARER-SECRET-1234567890"
    fake_gpsoauth.oauth_error = RuntimeError(f"dependency echoed {master_secret} {bearer_secret}")

    with pytest.raises(OAuthMintError) as failed:
        await MintService().mint_oauth(
            MasterToken("person@example.com", "android-1", master_secret),
            OAuthClientSpec("oauth2:scope", "example.app", "certificate"),
        )

    assert str(failed.value) == "perform_oauth failed (network or gpsoauth error)."
    _assert_chain(failed.value, cause=None, context=None, suppress=False)
    _assert_error_graph_does_not_retain_secret(failed.value, master_secret)
    _assert_error_graph_does_not_retain_secret(failed.value, bearer_secret)


def test_oversized_oauth_expiry_is_unknown_when_integer_limit_is_disabled() -> None:
    setter = getattr(sys, "set_int_max_str_digits", None)
    getter = getattr(sys, "get_int_max_str_digits", None)
    if setter is None or getter is None:
        assert _parse_oauth_expiry("9" * 10_000) is None
        return
    original = getter()
    try:
        setter(0)
        assert _parse_oauth_expiry("9" * 10_000) is None
    finally:
        setter(original)


@pytest.mark.parametrize("layer", ["mint_oauth", "mint", "public"])
@pytest.mark.parametrize("failure", ["worker", "malformed", "rejected"])
@pytest.mark.asyncio
async def test_every_oauth_layer_discards_failure_secrets_from_error_graph_and_traceback(
    fake_gpsoauth: types.ModuleType,
    layer: str,
    failure: str,
) -> None:
    master_secret = "aas_et/LAYER-MASTER-SECRET"
    bearer_secret = "ya29.LAYER-BEARER-SECRET-1234567890"
    if failure == "worker":
        fake_gpsoauth.oauth_error = RuntimeError(
            f"dependency echoed {master_secret} {bearer_secret}"
        )
        expected_message = "perform_oauth failed (network or gpsoauth error)."
    elif failure == "malformed":
        fake_gpsoauth.oauth_return = object()
        expected_message = "perform_oauth returned a malformed response."
    else:
        fake_gpsoauth.oauth_return = {
            "Auth": "",
            "Error": f"server echoed {master_secret} {bearer_secret}",
        }
        expected_message = (
            "perform_oauth rejected the master token. "
            "Re-bootstrap with `notebooklm login --master-token`."
        )

    expected_type = master_token.MasterTokenError if layer == "public" else OAuthMintError
    with pytest.raises(expected_type) as raised:
        if layer == "mint_oauth":
            await MintService().mint_oauth(
                MasterToken("person@example.com", "android-1", master_secret),
                OAuthClientSpec("oauth2:scope", "example.app", "certificate"),
            )
        elif layer == "mint":
            await MintService().mint(MasterToken("person@example.com", "android-1", master_secret))
        else:
            await master_token.mint_cookies("person@example.com", master_secret, "android-1")

    assert str(raised.value) == expected_message
    _assert_chain(raised.value, cause=None, context=None, suppress=False)
    _assert_error_graph_does_not_retain_secret(raised.value, master_secret)
    _assert_error_graph_does_not_retain_secret(raised.value, bearer_secret)
    if layer == "public":
        _assert_public_projection_has_no_private_error(raised.value)


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_mint_exact_offload_http_order_configuration_and_cookie_fidelity(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_success_responses(httpx_mock, merge_status=503)
    offloads: list[tuple[Callable[..., object], tuple[object, ...], dict[str, object]]] = []
    real_to_thread = asyncio.to_thread
    real_async_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []
    client_kwargs: list[dict[str, object]] = []

    async def recording_to_thread(
        function: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        offloads.append((function, args, kwargs))
        return await real_to_thread(function, *args, **kwargs)

    def recording_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client_kwargs.append(dict(kwargs))
        client = real_async_client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    monkeypatch.setattr(httpx, "AsyncClient", recording_client)
    token = MasterToken(email="person@example.com", android_id="android-1", secret="master-secret")

    jar = await MintService().mint(token)

    assert client_kwargs == [{"follow_redirects": True, "timeout": 30.0}]
    assert len(clients) == 1 and clients[0].is_closed
    assert jar is not clients[0].cookies
    assert offloads == [
        (
            _perform_oauth,
            (
                fake_gpsoauth,
                "person@example.com",
                "master-secret",
                "android-1",
                _CHROMECAST_OAUTH_SPEC,
            ),
            {},
        )
    ]
    assert fake_gpsoauth.oauth_calls == [
        (
            ("person@example.com", "master-secret", "android-1"),
            {
                "service": "oauth2:https://www.google.com/accounts/OAuthLogin",
                "app": "com.google.android.apps.chromecast.app",
                "client_sig": "24bb24c05e47e0aefa68a58a766179d9b613a600",
            },
        )
    ]

    requests = httpx_mock.get_requests()
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert [request.url.path for request in requests] == [
        "/OAuthLogin",
        "/MergeSession",
        "/RotateCookies",
    ]
    assert requests[0].url.params.multi_items() == [
        ("source", "ChromiumBrowser"),
        ("issueuberauth", "1"),
    ]
    assert requests[1].url.params.multi_items() == [
        ("service", "mail"),
        ("continue", "https://www.google.com"),
        ("uberauth", "APh-UBERAUTH"),
    ]
    assert requests[0].headers["authorization"] == "Bearer ya29.FAKE"
    assert requests[1].headers["authorization"] == "Bearer ya29.FAKE"
    assert requests[2].headers["content-type"] == "application/json"
    assert requests[2].headers["origin"] == "https://accounts.google.com"
    assert requests[2].content == _KEEPALIVE_ROTATE_BODY.encode()

    cookies = {cookie.name: cookie for cookie in jar.jar}
    assert set(cookies) == {
        "SID",
        "APISID",
        "SAPISID",
        "HOSTONLY",
        "__Secure-1PSIDTS",
    }
    assert cookies["SID"].domain == ".google.com"
    assert cookies["SID"].domain_initial_dot is True
    assert cookies["SID"].secure is True
    assert cookies["SID"]._rest == {"HttpOnly": None}  # noqa: SLF001
    assert cookies["APISID"].path == "/auth"
    assert cookies["SAPISID"].expires is not None and cookies["SAPISID"].expires > 2_000_000_000
    assert cookies["HOSTONLY"].domain == "accounts.google.com"
    assert cookies["HOSTONLY"].domain_specified is False
    assert cookies["HOSTONLY"].path == "/host"
    assert "master-secret" not in repr(jar)


@pytest.mark.asyncio
async def test_missing_gpsoauth_happens_before_offload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "gpsoauth", None)
    offloaded = False

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        nonlocal offloaded
        offloaded = True
        pytest.fail("missing dependency must fail before the first offload")

    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    master_secret = "MASTER-SECRET-FOR-TRACEBACK"
    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[headless\]") as seam:
        await MintService().mint_oauth(
            MasterToken("e@example.com", "android", master_secret),
            OAuthClientSpec("oauth2:scope", "example.app", "certificate"),
        )
    assert classify(seam.value).category is ErrorCategory.DEPENDENCY
    assert not isinstance(seam.value, _MintError)
    _assert_traceback_does_not_retain_secret(seam.value, master_secret)

    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[headless\]") as raised:
        await MintService().mint(MasterToken("e@example.com", "android", master_secret))
    assert offloaded is False
    assert classify(raised.value).category is ErrorCategory.DEPENDENCY
    assert not isinstance(raised.value, _MintError)
    _assert_traceback_does_not_retain_secret(raised.value, master_secret)

    with pytest.raises(MissingDependencyError) as public:
        await master_token.mint_cookies("e@example.com", master_secret, "android")
    _assert_traceback_does_not_retain_secret(public.value, master_secret)


class _SecretCarrier:
    def __repr__(self) -> str:
        return "WORKER-SECRET"


@pytest.mark.asyncio
async def test_cancelled_mint_returns_before_worker_settles_and_worker_retains_only_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    module = types.ModuleType("gpsoauth")

    def perform_oauth(*_args: object, **_kwargs: object) -> dict[str, str]:
        started.set()
        try:
            assert release.wait(2.0)
            return {"Auth": "unused"}
        finally:
            finished.set()

    module.perform_oauth = perform_oauth
    monkeypatch.setitem(sys.modules, "gpsoauth", module)
    secret = _SecretCarrier()
    secret_ref = weakref.ref(secret)
    token = MasterToken("e@example.com", "android", secret)  # type: ignore[arg-type]
    service = MintService()
    task = asyncio.create_task(service.mint(token))
    del token
    del secret
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0)
    assert started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gc.collect()
    assert not finished.is_set()
    assert secret_ref() is not None
    assert vars(service) == {}
    release.set()
    assert finished.wait(2.0)


@pytest.mark.asyncio
async def test_perform_oauth_failure_and_rejection_error_matrices(
    fake_gpsoauth: types.ModuleType,
) -> None:
    original = RuntimeError("perform failed safely")
    fake_gpsoauth.oauth_error = original
    token = MasterToken("e@example.com", "android", "master-secret")

    with pytest.raises(_MintError) as failed:
        await MintService().mint(token)
    assert str(failed.value) == "perform_oauth failed (network or gpsoauth error)."
    _assert_chain(failed.value, cause=None, context=None, suppress=False)

    with pytest.raises(master_token.MasterTokenError) as public:
        await master_token.mint_cookies("e@example.com", "master-secret", "android")
    _assert_chain(public.value, cause=None, context=None, suppress=False)
    _assert_public_projection_has_no_private_error(public.value)

    fake_gpsoauth.oauth_error = None
    fake_gpsoauth.oauth_return = {"Auth": 0, "Error": {"code": 7}}
    with pytest.raises(_MintError) as rejected:
        await MintService().mint(token)
    assert str(rejected.value) == (
        "perform_oauth rejected the master token. "
        "Re-bootstrap with `notebooklm login --master-token`."
    )
    _assert_chain(rejected.value, cause=None, context=None, suppress=False)


@pytest.mark.asyncio
async def test_async_mint_rejection_projection_ignores_active_outer_exception(
    fake_gpsoauth: types.ModuleType,
) -> None:
    fake_gpsoauth.oauth_return = {"Auth": "", "Error": "rejected safely"}
    outer = RuntimeError("OUTER-MASTER-SECRET")

    try:
        raise outer
    except RuntimeError:
        with pytest.raises(master_token.MasterTokenError) as public:
            await master_token.mint_cookies("person@example.com", "master-secret", "android-1")

    assert str(public.value) == (
        "perform_oauth rejected the master token. "
        "Re-bootstrap with `notebooklm login --master-token`."
    )
    _assert_chain(public.value, cause=None, context=None, suppress=False)
    _assert_public_projection_has_no_private_error(public.value)
    assert not _adapter_traceback_retains(public.value, outer, "mint_cookies")
    rendered = "".join(traceback.format_exception(public.value))
    assert "OUTER-MASTER-SECRET" not in rendered


@pytest.mark.asyncio
async def test_malformed_perform_result_is_sanitized_oauth_mint_error(
    fake_gpsoauth: types.ModuleType,
) -> None:
    fake_gpsoauth.oauth_return = object()
    master_secret = "aas_et/MALFORMED-MASTER-SECRET"
    with pytest.raises(OAuthMintError) as raised:
        await MintService().mint(MasterToken("e@example.com", "android", master_secret))
    assert str(raised.value) == "perform_oauth returned a malformed response."
    _assert_chain(raised.value, cause=None, context=None, suppress=False)
    _assert_error_graph_does_not_retain_secret(raised.value, master_secret)


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize(
    ("status", "body"),
    [(500, "APh-UBERAUTH"), (200, ""), (200, "contains space")],
)
@pytest.mark.asyncio
async def test_oauth_login_validation_is_exact_and_has_empty_chain(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
    status: int,
    body: str,
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, status_code=status, text=body)

    with pytest.raises(_MintError) as captured:
        await MintService().mint(MasterToken("e@example.com", "android", "secret"))

    assert str(captured.value) == "OAuthLogin did not return a uberauth token."
    _assert_chain(captured.value, cause=None, context=None, suppress=False)


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_internal_tab_uberauth_and_merge_error_status_are_not_rejected(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh\tTOKEN")
    httpx_mock.add_response(url=_MERGESESSION_RE, status_code=503, headers=_valid_merge_headers())
    httpx_mock.add_response(url=_ROTATE_RE)

    jar = await MintService().mint(MasterToken("e@example.com", "android", "secret"))

    assert {cookie.name for cookie in jar.jar} >= {"SID", "APISID", "SAPISID"}


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize(
    ("status", "expected_args"),
    [
        (429, ("HTTPStatusError", 429)),
        (503, ("HTTPStatusError", 503)),
    ],
)
@pytest.mark.asyncio
async def test_rotate_http_status_is_best_effort_and_logs_only_safe_scalars(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
    status: int,
    expected_args: tuple[str, int],
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(url=_MERGESESSION_RE, headers=_valid_merge_headers())
    httpx_mock.add_response(url=_ROTATE_RE, status_code=status)

    logger = logging.getLogger("notebooklm.auth.master_token")
    prior_level = logger.level
    handler = _RawRecordHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        jar = await MintService().mint(MasterToken("e@example.com", "android", "master-secret"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    assert {cookie.name for cookie in jar.jar} >= {"SID", "APISID", "SAPISID"}
    entries = [
        entry
        for entry in handler.entries
        if entry[0] == "RotateCookies during mint failed (non-fatal): %s status=%s"
    ]
    assert entries == [
        (
            "RotateCookies during mint failed (non-fatal): %s status=%s",
            expected_args,
            "RotateCookies during mint failed "
            f"(non-fatal): {expected_args[0]} status={expected_args[1]}",
        )
    ]
    assert "master-secret" not in entries[0][2]
    assert all(not isinstance(arg, BaseException) for arg in entries[0][1])


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_rotate_connect_error_is_best_effort_and_logs_no_exception_or_url(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(url=_MERGESESSION_RE, headers=_valid_merge_headers())
    secret_url = "https://accounts.google.com/RotateCookies?cookie=COOKIE-SECRET"
    original = httpx.ConnectError("safe failure", request=httpx.Request("POST", secret_url))
    httpx_mock.add_exception(original, url=_ROTATE_RE)

    logger = logging.getLogger("notebooklm.auth.master_token")
    prior_level = logger.level
    handler = _RawRecordHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await MintService().mint(MasterToken("e@example.com", "android", "master-secret"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)

    entry = next(
        item
        for item in handler.entries
        if item[0] == "RotateCookies during mint failed (non-fatal): %s status=%s"
    )
    assert entry[1] == ("ConnectError", None)
    assert entry[2].endswith("ConnectError status=None")
    assert original not in entry[1]
    assert "COOKIE-SECRET" not in entry[2]


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_unexpected_rotate_error_escapes_raw(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(url=_MERGESESSION_RE, headers=_valid_merge_headers())
    original = ValueError("unexpected rotate failure")
    httpx_mock.add_exception(original, url=_ROTATE_RE)

    with pytest.raises(ValueError) as captured:
        await MintService().mint(MasterToken("e@example.com", "android", "secret"))
    assert captured.value is original


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_outer_http_error_preserves_suppressed_secret_context_exactly(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    secret = "master-secret"
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="UBER-SECRET")
    original = httpx.ConnectError(
        "safe merge failure",
        request=httpx.Request(
            "GET", "https://accounts.google.com/MergeSession?uberauth=UBER-SECRET"
        ),
    )
    httpx_mock.add_exception(original, url=_MERGESESSION_RE)

    with pytest.raises(_MintError) as lower:
        await MintService().mint(MasterToken("e@example.com", "android", secret))
    assert str(lower.value) == (
        "cookie minting failed (network error reaching accounts.google.com)."
    )
    _assert_chain(lower.value, cause=None, context=original, suppress=True)

    httpx_mock.reset()
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="UBER-SECRET")
    httpx_mock.add_exception(original, url=_MERGESESSION_RE)
    with pytest.raises(master_token.MasterTokenError) as public:
        await master_token.mint_cookies("e@example.com", secret, "android")
    _assert_chain(public.value, cause=None, context=original, suppress=True)
    _assert_public_projection_has_no_private_error(public.value)
    rendered = "".join(traceback.format_exception(public.value))
    assert "UBER-SECRET" not in str(public.value)
    assert "UBER-SECRET" not in rendered
    assert "master-secret" not in rendered


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_missing_required_names_are_sorted_and_osid_is_not_a_substitute(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(
        url=_MERGESESSION_RE,
        headers=_cookie_headers(
            "SID=sid; Domain=.google.com; Path=/", "OSID=osid; Domain=.google.com; Path=/"
        ),
    )
    httpx_mock.add_response(url=_ROTATE_RE)

    with pytest.raises(_MintError) as captured:
        await MintService().mint(MasterToken("e@example.com", "android", "secret"))

    assert str(captured.value) == (
        "Minted cookie jar is missing required cookies: ['APISID', 'SAPISID']. "
        "MergeSession may have changed; the session would fail PSIDTS recovery."
    )
    _assert_chain(captured.value, cause=None, context=None, suppress=False)


@pytest.mark.asyncio
async def test_async_and_sync_rotate_wire_functions_are_exact() -> None:
    async_calls: list[tuple[str, dict[str, object]]] = []
    sync_calls: list[tuple[str, dict[str, object]]] = []
    async_response = httpx.Response(200, request=httpx.Request("POST", KEEPALIVE_ROTATE_URL))
    sync_response = httpx.Response(200, request=httpx.Request("POST", KEEPALIVE_ROTATE_URL))

    class AsyncClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            async_calls.append((url, kwargs))
            return async_response

    class SyncClient:
        def post(self, url: str, **kwargs: object) -> httpx.Response:
            sync_calls.append((url, kwargs))
            return sync_response

    assert await _rotate_post(AsyncClient()) is async_response  # type: ignore[arg-type]
    assert _rotate_post_sync(SyncClient()) is sync_response  # type: ignore[arg-type]
    expected = {
        "headers": _KEEPALIVE_ROTATE_HEADERS,
        "content": _KEEPALIVE_ROTATE_BODY,
        "follow_redirects": True,
        "timeout": _KEEPALIVE_POKE_TIMEOUT,
    }
    assert async_calls == [(KEEPALIVE_ROTATE_URL, expected)]
    assert sync_calls == [(KEEPALIVE_ROTATE_URL, expected)]


@pytest.mark.no_default_keepalive_mock
@pytest.mark.asyncio
async def test_public_adapters_keep_facade_identity_signatures_and_typed_projection(
    fake_gpsoauth: types.ModuleType,
    httpx_mock: Any,
) -> None:
    _add_success_responses(httpx_mock)

    assert auth.exchange_master_token is master_token.exchange_master_token
    assert auth.mint_cookies is master_token.mint_cookies
    assert str(inspect.signature(master_token.exchange_master_token)) == (
        "(email: 'str', oauth_token: 'str', android_id: 'str') -> 'str'"
    )
    assert str(inspect.signature(master_token.mint_cookies)) == (
        "(email: 'str', master_token: 'str', android_id: 'str') -> 'httpx.Cookies'"
    )
    assert (
        master_token.exchange_master_token("person@example.com", "oauth-secret", "android-1")
        == "aas_et/MASTER"
    )
    jar = await master_token.mint_cookies("person@example.com", "master-secret", "android-1")
    assert isinstance(jar, httpx.Cookies)
    assert fake_gpsoauth.exchange_calls == [("person@example.com", "oauth-secret", "android-1")]
    assert fake_gpsoauth.oauth_calls[0][0] == (
        "person@example.com",
        "master-secret",
        "android-1",
    )
