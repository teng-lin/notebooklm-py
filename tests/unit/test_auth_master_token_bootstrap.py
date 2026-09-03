"""Direct behavior matrix for the path-owned master-token bootstrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback
from http.cookiejar import Cookie as HttpCookie
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from filelock import FileLock, Timeout

from notebooklm._auth import cookies as cookies_mod
from notebooklm._auth import master_token as mt
from notebooklm._auth import storage as storage_mod
from notebooklm._auth.master_token_bootstrap import (
    BootstrapOutcome,
    MasterTokenBootstrapper,
    _BootstrapError,
)
from notebooklm._auth.master_token_types import MasterToken, _MasterTokenRecordError
from notebooklm._auth.mint_service import MintService, _MintError
from notebooklm._auth.profile_store import (
    MintedSessionWriteRequest,
    ProfileStore,
    _MintedSessionOwnershipRefused,
)


def _bootstrapper(
    tmp_path: Path,
    *,
    verifier: AsyncMock | None = None,
) -> tuple[MasterTokenBootstrapper, MintService, ProfileStore, AsyncMock]:
    store = ProfileStore(tmp_path / "storage_state.json")
    mint_service = MintService()
    verify = verifier or AsyncMock(return_value=17)
    return (
        MasterTokenBootstrapper(
            mint_service=mint_service,
            store=store,
            bootstrap_lock=FileLock(str(tmp_path / "bootstrap.lock")),
            verifier=verify,
        ),
        mint_service,
        store,
        verify,
    )


def _raw_cookie(
    *,
    name: str = "SID",
    value: str = "cookie-secret",
    domain: str = ".google.com",
    path: str = "/app",
    expires: int = 1_900_000_000,
    secure: bool = True,
    http_only: bool = True,
) -> HttpCookie:
    return HttpCookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if http_only else {},
        rfc2109=False,
    )


def _jar(*cookies: HttpCookie) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in cookies or (_raw_cookie(),):
        jar.jar.set_cookie(cookie)
    return jar


def _traceback_frame_locals(
    error: BaseException,
    function_name: str,
    *,
    module_name: str | None = None,
) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == function_name and (
            module_name is None or traceback.tb_frame.f_globals.get("__name__") == module_name
        ):
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return frames


@pytest.mark.asyncio
async def test_oauth_bootstrap_exact_order_projection_and_verifier(tmp_path, monkeypatch):
    bootstrapper, mint_service, store, verifier = _bootstrapper(tmp_path)
    events: list[str] = []
    reads = iter((None, None))
    token = MasterToken("owner@example.com", "generated-id", "master-secret")
    live_jar = _jar(_raw_cookie())
    captured: list[MintedSessionWriteRequest] = []

    def read_token():
        events.append("token-read")
        return next(reads)

    def owner_reader(path):
        assert path == store.path
        events.append("session-owner")
        return None

    def generate():
        events.append("android-generate")
        return "generated-id"

    def exchange(email, oauth_token, android_id):
        assert (email, oauth_token, android_id) == (
            "owner@example.com",
            "oauth-secret",
            "generated-id",
        )
        events.append("exchange")
        return token

    async def mint(received):
        assert received is token
        events.append("mint")
        return live_jar

    def replace(request):
        events.append("session-write")
        captured.append(request)

    def write(received):
        assert received is token
        events.append("token-write")

    async def verify(path):
        assert path == store.path
        events.append("verify")
        return 17

    monkeypatch.setattr(store, "read_master_token", read_token)
    monkeypatch.setattr(mint_service, "exchange", exchange)
    monkeypatch.setattr(mint_service, "mint", mint)
    monkeypatch.setattr(store, "replace_minted_session", replace)
    monkeypatch.setattr(store, "write_master_token", write)
    verifier.side_effect = verify

    result = await bootstrapper.bootstrap_from_oauth_token(
        email="owner@example.com",
        oauth_token="oauth-secret",
        verify=True,
        session_owner_reader=owner_reader,
        android_id_generator=generate,
    )

    assert result == 17
    assert events == [
        "token-read",
        "android-generate",
        "token-read",
        "session-owner",
        "exchange",
        "mint",
        "session-write",
        "token-write",
        "verify",
    ]
    assert len(captured) == 1
    request = captured[0]
    assert (request.email, request.force, request.refuse_unknown_owner) == (
        "owner@example.com",
        False,
        True,
    )
    assert len(tuple(request.cookies)) == 1
    cookie = tuple(request.cookies)[0]
    assert (
        cookie.name,
        cookie.value,
        cookie.domain,
        cookie.path,
        cookie.expires,
        cookie.secure,
        cookie.http_only,
        cookie.same_site,
    ) == (
        "SID",
        "cookie-secret",
        ".google.com",
        "/app",
        1_900_000_000,
        True,
        True,
        "None",
    )


@pytest.mark.parametrize(
    ("explicit", "stored", "expected", "generated_calls"),
    [
        (
            "explicit-id",
            MasterToken("e@example.com", "stored-id", "secret"),
            "explicit-id",
            0,
        ),
        (None, MasterToken("e@example.com", "stored-id", "secret"), "stored-id", 0),
        (None, None, "fresh-id", 1),
    ],
)
@pytest.mark.asyncio
async def test_android_id_precedence(
    tmp_path,
    monkeypatch,
    explicit,
    stored,
    expected,
    generated_calls,
):
    bootstrapper, mint_service, store, verifier = _bootstrapper(tmp_path)
    token = MasterToken("e@example.com", expected, "new-secret")
    generator = Mock(return_value="fresh-id")
    exchange = Mock(return_value=token)
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=(stored, stored)))
    monkeypatch.setattr(mint_service, "exchange", exchange)
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))
    monkeypatch.setattr(store, "replace_minted_session", Mock())
    monkeypatch.setattr(store, "write_master_token", Mock())

    assert (
        await bootstrapper.bootstrap_from_oauth_token(
            email="e@example.com",
            oauth_token="oauth",
            android_id=explicit,
            verify=False,
            session_owner_reader=lambda _path: None,
            android_id_generator=generator,
        )
        == -1
    )

    assert exchange.call_args.args[2] == expected
    assert generator.call_count == generated_calls
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_android_id_still_rejects_corrupt_token_before_exchange(
    tmp_path, monkeypatch
):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    exchange = Mock()
    owner_reader = Mock()
    generator = Mock()
    monkeypatch.setattr(
        store,
        "read_master_token",
        Mock(side_effect=_MasterTokenRecordError("corrupt")),
    )
    monkeypatch.setattr(mint_service, "exchange", exchange)

    with pytest.raises(_BootstrapError, match="malformed or an unsupported version"):
        await bootstrapper.bootstrap_from_oauth_token(
            email="e@example.com",
            oauth_token="oauth-secret",
            android_id="explicit-id",
            verify=False,
            session_owner_reader=owner_reader,
            android_id_generator=generator,
        )

    exchange.assert_not_called()
    owner_reader.assert_not_called()
    generator.assert_not_called()


@pytest.mark.parametrize(
    ("session_owner", "token_owner", "requested", "conflict"),
    [
        (" Other@Example.com ", None, "owner@example.com", True),
        (None, " Other@Example.com ", "owner@example.com", True),
        (" Owner@Example.com ", "OWNER@example.com", "owner@example.com", False),
        ("   ", "", "owner@example.com", False),
        ("owner@example.com", None, " owner@example.com ", True),
        (42, object(), "owner@example.com", False),
    ],
)
def test_advisory_owner_matrix(
    tmp_path,
    monkeypatch,
    session_owner,
    token_owner,
    requested,
    conflict,
):
    bootstrapper, _, store, _ = _bootstrapper(tmp_path)
    token = None if token_owner is None else MasterToken(token_owner, "aid", "secret")
    calls: list[str] = []
    monkeypatch.setattr(store, "read_master_token", lambda: calls.append("token") or token)

    def owner_reader(path):
        assert path == store.path
        calls.append("session")
        return session_owner

    if conflict:
        with pytest.raises(_BootstrapError, match="already belongs to"):
            bootstrapper.assert_account_writable(
                email=requested,
                session_owner_reader=owner_reader,
            )
    else:
        bootstrapper.assert_account_writable(
            email=requested,
            session_owner_reader=owner_reader,
        )
    assert calls == ["token", "session"]


def test_advisory_ignores_only_typed_token_error_then_reads_session(tmp_path, monkeypatch):
    bootstrapper, _, store, _ = _bootstrapper(tmp_path)
    owner_reader = Mock(return_value=None)
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=OSError("unreadable")))

    bootstrapper.assert_account_writable(
        email="owner@example.com",
        session_owner_reader=owner_reader,
    )

    owner_reader.assert_called_once_with(store.path)


def test_advisory_unicode_failure_escapes_before_session_reader(tmp_path, monkeypatch):
    bootstrapper, _, store, _ = _bootstrapper(tmp_path)
    error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    owner_reader = Mock()
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=error))

    with pytest.raises(UnicodeDecodeError) as raised:
        bootstrapper.assert_account_writable(
            email="owner@example.com",
            session_owner_reader=owner_reader,
        )

    assert raised.value is error
    owner_reader.assert_not_called()


def test_public_force_fast_path_precedes_path_derivation_and_composition(monkeypatch):
    composition = Mock(side_effect=AssertionError("must not compose"))
    monkeypatch.setattr(mt, "_bootstrapper", composition)

    assert (
        mt.assert_account_writable(
            email="",
            storage_path=Path("must/not/be/derived.json"),
            force=True,
        )
        is None
    )

    composition.assert_not_called()


@pytest.mark.asyncio
async def test_oauth_force_keeps_android_sample_but_skips_advisory_owner_io(
    tmp_path,
    monkeypatch,
):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "stored-id", "secret")
    read = Mock(return_value=token)
    owner_reader = Mock(side_effect=AssertionError("force must skip owner lookup"))
    monkeypatch.setattr(store, "read_master_token", read)
    monkeypatch.setattr(mint_service, "exchange", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))
    monkeypatch.setattr(store, "replace_minted_session", Mock())
    monkeypatch.setattr(store, "write_master_token", Mock())

    assert (
        await bootstrapper.bootstrap_from_oauth_token(
            email="",
            oauth_token="oauth",
            verify=False,
            force=True,
            session_owner_reader=owner_reader,
            android_id_generator=Mock(),
        )
        == -1
    )
    read.assert_called_once_with()
    owner_reader.assert_not_called()


@pytest.mark.parametrize(
    "lower",
    [
        OSError("disk failure"),
        json.JSONDecodeError("bad json", "{", 1),
    ],
)
@pytest.mark.asyncio
async def test_public_token_read_io_projection_without_outer_exception(tmp_path, lower):
    with (
        patch.object(ProfileStore, "read_master_token", autospec=True, side_effect=lower),
        pytest.raises(mt.MasterTokenError, match="Unreadable master_token.json") as raised,
    ):
        await mt.remint_from_stored_token(tmp_path / "storage_state.json")

    error = raised.value
    assert error.__cause__ is lower
    assert error.__context__ is lower
    assert error.__suppress_context__ is True
    assert not isinstance(error.__cause__, _BootstrapError)
    assert not isinstance(error.__context__, _BootstrapError)


@pytest.mark.parametrize(
    "lower",
    [
        OSError("disk failure"),
        json.JSONDecodeError("bad json", "{", 1),
    ],
)
@pytest.mark.asyncio
async def test_public_token_read_io_projection_with_outer_exception(tmp_path, lower):
    outer = RuntimeError("active caller")
    try:
        raise outer
    except RuntimeError:
        with (
            patch.object(ProfileStore, "read_master_token", autospec=True, side_effect=lower),
            pytest.raises(mt.MasterTokenError, match="Unreadable master_token.json") as raised,
        ):
            await mt.remint_from_stored_token(tmp_path / "storage_state.json")

    error = raised.value
    # ``__cause__`` and ``__suppress_context__`` are set explicitly by the
    # projection (``raise ... from lower`` semantics) and ARE the contract.
    assert error.__cause__ is lower
    assert error.__suppress_context__ is True
    assert not isinstance(error.__cause__, _BootstrapError)
    assert lower.__context__ is None
    # The public-chain sanitization contract, asserted directly rather than
    # inferred from the rendered traceback (#2229 review). *Which* exception
    # ``__context__`` holds is ambient — that is the whole point of #2224 — but
    # it must never be the private ``_BootstrapError`` in any of them. A
    # regression that re-chained the private error here would satisfy every
    # other assertion above: ``__suppress_context__`` is true, so
    # ``format_exception`` follows ``__cause__`` and omits the context entirely,
    # leaving the rendered-output check blind to it.
    assert not isinstance(error.__context__, _BootstrapError)

    # ``__context__`` is deliberately NOT asserted here (#2224). CPython
    # re-chains an exception as it propagates out of a coroutine, so the value
    # a caller observes depends on ambient exception state and await depth --
    # not on the code under test. Measured: it flips between the first and a
    # later invocation *within one process on one interpreter*, which is why
    # the old ``sys.version_info >= (3, 13)`` branch could not stabilise it.
    #
    # What the projection actually owes the user is asserted instead, and is
    # invariant: because the context is suppressed, the rendered traceback
    # shows the real cause and never leaks the caller's unrelated exception.
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert str(lower) in rendered
    assert "active caller" not in rendered
    assert "_BootstrapError" not in rendered


@pytest.mark.parametrize("active_outer", [False, True])
@pytest.mark.asyncio
async def test_public_malformed_projection_preserves_ordinary_context(
    tmp_path,
    active_outer,
):
    lower = _MasterTokenRecordError("private shape")
    outer = RuntimeError("active caller")

    async def invoke():
        with pytest.raises(mt.MasterTokenError, match="malformed or an unsupported") as raised:
            await mt.remint_from_stored_token(tmp_path / "storage_state.json")
        return raised.value

    with patch.object(ProfileStore, "read_master_token", autospec=True, side_effect=lower):
        if active_outer:
            try:
                raise outer
            except RuntimeError:
                error = await invoke()
        else:
            error = await invoke()

    assert error.__cause__ is None
    assert error.__suppress_context__ is False
    # Same reasoning as #2224 above: the *identity* of ``__context__`` under an
    # active outer exception is ambient, so only the leak contract is asserted
    # -- no private bootstrap/record shape may reach the caller, by type or by
    # rendered text. With no outer exception active there is nothing ambient to
    # pick up, so that arm still pins the exact value.
    assert not isinstance(error.__context__, (_BootstrapError, _MasterTokenRecordError))
    if not active_outer:
        assert error.__context__ is None
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert "private shape" not in rendered
    assert "_MasterTokenRecordError" not in rendered
    assert "_BootstrapError" not in rendered


@pytest.mark.asyncio
async def test_unlisted_token_read_exception_escapes_unchanged(tmp_path):
    lower = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    with (
        patch.object(ProfileStore, "read_master_token", autospec=True, side_effect=lower),
        pytest.raises(UnicodeDecodeError) as raised,
    ):
        await mt.remint_from_stored_token(tmp_path / "storage_state.json")
    assert raised.value is lower


@pytest.mark.parametrize("stage", ["exchange", "mint"])
@pytest.mark.asyncio
async def test_private_mint_errors_never_escape_public_chain(tmp_path, monkeypatch, stage):
    lower = LookupError("lower cause")
    private = _MintError("safe rejection")
    private.__cause__ = lower
    private.__context__ = lower
    private.__suppress_context__ = True
    exchange = (
        Mock(side_effect=private)
        if stage == "exchange"
        else Mock(return_value=MasterToken("owner@example.com", "aid", "secret"))
    )
    mint = AsyncMock(side_effect=private if stage == "mint" else _jar())
    monkeypatch.setattr(MintService, "exchange", exchange)
    monkeypatch.setattr(MintService, "mint", mint)
    monkeypatch.setattr(ProfileStore, "read_master_token", Mock(return_value=None))
    monkeypatch.setattr(ProfileStore, "replace_minted_session", Mock())
    monkeypatch.setattr(ProfileStore, "write_master_token", Mock())

    with pytest.raises(mt.MasterTokenError, match="safe rejection") as raised:
        await mt.bootstrap_from_oauth_token(
            email="owner@example.com",
            oauth_token="oauth-secret",
            storage_path=tmp_path / "storage_state.json",
            verify=False,
        )

    error = raised.value
    assert error.__cause__ is lower
    assert error.__context__ is lower
    assert error.__suppress_context__ is True
    assert not isinstance(error.__cause__, (_MintError, _BootstrapError))
    assert not isinstance(error.__context__, (_MintError, _BootstrapError))


@pytest.mark.asyncio
async def test_session_refusal_is_sanitized_and_prevents_token_write(tmp_path, monkeypatch):
    bootstrapper, mint_service, store, verifier = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    write = Mock()
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=(None, None)))
    monkeypatch.setattr(mint_service, "exchange", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))
    monkeypatch.setattr(
        store,
        "replace_minted_session",
        Mock(side_effect=_MintedSessionOwnershipRefused("safe refusal")),
    )
    monkeypatch.setattr(store, "write_master_token", write)

    with pytest.raises(_BootstrapError, match="safe refusal") as raised:
        await bootstrapper.bootstrap_from_oauth_token(
            email="owner@example.com",
            oauth_token="oauth",
            session_owner_reader=lambda _path: None,
            android_id_generator=lambda: "aid",
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is False
    write.assert_not_called()
    verifier.assert_not_awaited()


@pytest.mark.parametrize("failure_stage", ["session", "token", "verify"])
@pytest.mark.asyncio
async def test_oauth_failure_boundaries_preserve_committed_prefix(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    bootstrapper, mint_service, store, verifier = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    events: list[str] = []
    failure = OSError(f"{failure_stage} failed")
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=(None, None)))
    monkeypatch.setattr(mint_service, "exchange", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))

    def replace(_request):
        events.append("session")
        if failure_stage == "session":
            raise failure

    def write(_token):
        events.append("token")
        if failure_stage == "token":
            raise failure

    async def verify(_path):
        events.append("verify")
        if failure_stage == "verify":
            raise failure
        return 1

    monkeypatch.setattr(store, "replace_minted_session", replace)
    monkeypatch.setattr(store, "write_master_token", write)
    verifier.side_effect = verify

    with pytest.raises(OSError) as raised:
        await bootstrapper.bootstrap_from_oauth_token(
            email="owner@example.com",
            oauth_token="oauth",
            session_owner_reader=lambda _path: None,
            android_id_generator=lambda: "aid",
        )

    assert raised.value is failure
    assert (
        events
        == {
            "session": ["session"],
            "token": ["session", "token"],
            "verify": ["session", "token", "verify"],
        }[failure_stage]
    )


@pytest.mark.parametrize("stage", ["session", "token"])
@pytest.mark.asyncio
async def test_oauth_to_thread_cancellation_can_finish_only_current_write(
    tmp_path,
    monkeypatch,
    stage,
):
    bootstrapper, mint_service, store, verifier = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    events: list[str] = []
    monkeypatch.setattr(store, "read_master_token", Mock(side_effect=(None, None)))
    monkeypatch.setattr(mint_service, "exchange", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))

    def replace(_request):
        events.append("session-start")
        if stage == "session":
            started.set()
            assert release.wait(timeout=5)
            events.append("session-commit")
            settled.set()
        else:
            events.append("session-commit")

    def write(_token):
        events.append("token-start")
        if stage == "token":
            started.set()
            assert release.wait(timeout=5)
            events.append("token-commit")
            settled.set()

    monkeypatch.setattr(store, "replace_minted_session", replace)
    monkeypatch.setattr(store, "write_master_token", write)

    task = asyncio.create_task(
        bootstrapper.bootstrap_from_oauth_token(
            email="owner@example.com",
            oauth_token="oauth",
            session_owner_reader=lambda _path: None,
            android_id_generator=lambda: "aid",
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if stage == "session":
        assert events == ["session-start"]
    else:
        assert events == ["session-start", "session-commit", "token-start"]
    verifier.assert_not_awaited()

    release.set()
    assert await asyncio.to_thread(settled.wait, 5)
    if stage == "session":
        assert events == ["session-start", "session-commit"]
    else:
        assert events == [
            "session-start",
            "session-commit",
            "token-start",
            "token-commit",
        ]


@pytest.mark.asyncio
async def test_mint_cancellation_traceback_does_not_retain_master_token(tmp_path, monkeypatch):
    bootstrapper, mint_service, _, _ = _bootstrapper(tmp_path)
    secret = "traceback-master-secret"
    token = MasterToken("owner@example.com", "aid", secret)
    started = asyncio.Event()

    async def mint(_token):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(mint_service, "mint", mint)
    target = asyncio.current_task()
    assert target is not None

    async def cancel_after_mint_starts():
        await started.wait()
        target.cancel()

    killer = asyncio.create_task(cancel_after_mint_starts())
    outer = RuntimeError("active caller")
    try:
        raise outer
    except RuntimeError:
        with pytest.raises(asyncio.CancelledError) as raised:
            await bootstrapper._mint(token)
    await killer

    mint_frames = _traceback_frame_locals(raised.value, "_mint")
    assert len(mint_frames) == 1
    assert mint_frames[0]["caller_exception"] is None
    assert "token" not in mint_frames[0]
    assert token not in mint_frames[0].values()
    assert secret not in mint_frames[0].values()


def test_exchange_base_exception_traceback_does_not_retain_oauth_token(tmp_path, monkeypatch):
    bootstrapper, mint_service, _, _ = _bootstrapper(tmp_path)
    oauth_token = "traceback-oauth-secret"

    class ExchangeAbort(BaseException):
        pass

    failure = ExchangeAbort("abort exchange")
    monkeypatch.setattr(mint_service, "exchange", Mock(side_effect=failure))

    outer = RuntimeError("active caller")
    try:
        raise outer
    except RuntimeError:
        with pytest.raises(ExchangeAbort) as raised:
            bootstrapper._exchange("owner@example.com", oauth_token, "aid")

    exchange_frames = _traceback_frame_locals(raised.value, "_exchange")
    assert len(exchange_frames) == 1
    assert exchange_frames[0]["caller_exception"] is None
    assert "oauth_token" not in exchange_frames[0]
    assert oauth_token not in exchange_frames[0].values()


@pytest.mark.asyncio
async def test_public_oauth_mint_cancellation_traceback_does_not_retain_oauth_token(
    tmp_path,
    monkeypatch,
):
    oauth_token = "public-cancellation-oauth-secret"
    token = MasterToken("owner@example.com", "aid", "master-secret")
    started = asyncio.Event()

    monkeypatch.setattr(ProfileStore, "read_master_token", Mock(return_value=None))
    monkeypatch.setattr(MintService, "exchange", Mock(return_value=token))

    async def mint(_service, _token):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(MintService, "mint", mint)
    target = asyncio.current_task()
    assert target is not None

    async def cancel_after_mint_starts():
        await started.wait()
        target.cancel()

    killer = asyncio.create_task(cancel_after_mint_starts())
    outer = RuntimeError("active caller")
    try:
        raise outer
    except RuntimeError:
        with pytest.raises(asyncio.CancelledError) as raised:
            await mt.bootstrap_from_oauth_token(
                email="owner@example.com",
                oauth_token=oauth_token,
                storage_path=tmp_path / "storage_state.json",
                android_id="aid",
                verify=False,
                force=True,
            )
    await killer

    public_frames = _traceback_frame_locals(
        raised.value,
        "bootstrap_from_oauth_token",
        module_name="notebooklm._auth.master_token",
    )
    assert len(public_frames) == 1
    assert public_frames[0]["caller_exception"] is None
    assert "oauth_token" not in public_frames[0]
    assert oauth_token not in public_frames[0].values()


@pytest.mark.asyncio
async def test_public_oauth_exchange_base_exception_traceback_does_not_retain_oauth_token(
    tmp_path,
    monkeypatch,
):
    oauth_token = "public-base-exception-oauth-secret"

    class ExchangeAbort(BaseException):
        pass

    failure = ExchangeAbort("abort public exchange")
    monkeypatch.setattr(ProfileStore, "read_master_token", Mock(return_value=None))
    monkeypatch.setattr(MintService, "exchange", Mock(side_effect=failure))

    outer = RuntimeError("active caller")
    try:
        raise outer
    except RuntimeError:
        with pytest.raises(ExchangeAbort) as raised:
            await mt.bootstrap_from_oauth_token(
                email="owner@example.com",
                oauth_token=oauth_token,
                storage_path=tmp_path / "storage_state.json",
                android_id="aid",
                verify=False,
                force=True,
            )

    public_frames = _traceback_frame_locals(
        raised.value,
        "bootstrap_from_oauth_token",
        module_name="notebooklm._auth.master_token",
    )
    assert len(public_frames) == 1
    assert public_frames[0]["caller_exception"] is None
    assert "oauth_token" not in public_frames[0]
    assert oauth_token not in public_frames[0].values()


def test_public_advisory_unlisted_exception_drops_active_caller(tmp_path, monkeypatch):
    failure = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    monkeypatch.setattr(ProfileStore, "read_master_token", Mock(side_effect=failure))
    outer = RuntimeError("active caller")

    try:
        raise outer
    except RuntimeError:
        with pytest.raises(UnicodeDecodeError) as raised:
            mt.assert_account_writable(
                email="owner@example.com",
                storage_path=tmp_path / "storage_state.json",
            )

    public_frames = _traceback_frame_locals(
        raised.value,
        "assert_account_writable",
        module_name="notebooklm._auth.master_token",
    )
    assert len(public_frames) == 1
    assert public_frames[0]["caller_exception"] is None


@pytest.mark.parametrize(
    ("adapter_name", "coordinator_method"),
    [
        ("remint_from_stored_token", "remint_from_stored_token"),
        ("bootstrap_storage_from_master_token", "bootstrap_storage"),
    ],
)
@pytest.mark.asyncio
async def test_public_coarse_cancellation_drops_active_caller(
    tmp_path,
    monkeypatch,
    adapter_name,
    coordinator_method,
):
    bootstrapper = Mock()
    setattr(
        bootstrapper,
        coordinator_method,
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    monkeypatch.setattr(mt, "_bootstrapper", Mock(return_value=bootstrapper))
    outer = RuntimeError("active caller")

    try:
        raise outer
    except RuntimeError:
        with pytest.raises(asyncio.CancelledError) as raised:
            await getattr(mt, adapter_name)(tmp_path / "storage_state.json")

    public_frames = _traceback_frame_locals(
        raised.value,
        adapter_name,
        module_name="notebooklm._auth.master_token",
    )
    assert len(public_frames) == 1
    assert public_frames[0]["caller_exception"] is None


@pytest.mark.asyncio
async def test_remint_exact_order_returns_strict_reload_not_minted_jar(tmp_path, monkeypatch):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    minted = _jar(_raw_cookie(value="minted"))
    strict = _jar(_raw_cookie(value="strict"))
    events: list[str] = []
    captured: list[MintedSessionWriteRequest] = []

    def read():
        events.append("read")
        return token

    async def mint(received):
        assert received is token
        events.append("mint")
        return minted

    def replace(request):
        events.append("persist")
        captured.append(request)

    def load(path):
        assert path == store.path
        events.append("strict-load")
        return strict

    monkeypatch.setattr(store, "read_master_token", read)
    monkeypatch.setattr(mint_service, "mint", mint)
    monkeypatch.setattr(store, "replace_minted_session", replace)

    result = await bootstrapper.remint_from_stored_token(strict_loader=load)

    assert result is strict
    assert result is not minted
    assert events == ["read", "mint", "persist", "strict-load"]
    assert len(captured) == 1
    assert (
        captured[0].email,
        captured[0].force,
        captured[0].refuse_unknown_owner,
    ) == ("owner@example.com", False, False)


@pytest.mark.asyncio
async def test_remint_missing_token_uses_derived_sibling_path(tmp_path, monkeypatch):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    monkeypatch.setattr(store, "read_master_token", Mock(return_value=None))
    mint = AsyncMock()
    monkeypatch.setattr(mint_service, "mint", mint)

    with pytest.raises(_BootstrapError) as raised:
        await bootstrapper.remint_from_stored_token(strict_loader=Mock())

    assert str(raised.value) == (
        f"No master token at {tmp_path / 'master_token.json'}. "
        "Run `notebooklm login --master-token` first."
    )
    mint.assert_not_awaited()


@pytest.mark.parametrize("failure", [OSError("disk"), ValueError("invalid session")])
@pytest.mark.asyncio
async def test_remint_reload_failure_keeps_committed_session_and_original_cause(
    tmp_path,
    monkeypatch,
    failure,
):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    persisted = Mock()
    monkeypatch.setattr(store, "read_master_token", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=_jar()))
    monkeypatch.setattr(store, "replace_minted_session", persisted)

    with pytest.raises(_BootstrapError, match="persisted a session but reloading") as raised:
        await bootstrapper.remint_from_stored_token(strict_loader=Mock(side_effect=failure))

    persisted.assert_called_once()
    assert raised.value.__cause__ is failure
    assert raised.value.__context__ is failure
    assert raised.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_remint_strict_fidelity_for_duplicate_and_unusable_rows(tmp_path, monkeypatch):
    bootstrapper, mint_service, store, _ = _bootstrapper(tmp_path)
    token = MasterToken("owner@example.com", "aid", "secret")
    store.path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "first", "domain": ".google.com", "path": "/"},
                    {"name": "SID", "value": "scoped", "domain": ".google.com", "path": "/x"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {"name": "NID", "value": None, "domain": ".google.com", "path": "/"},
                ]
            }
        ),
        encoding="utf-8",
    )
    expected = cookies_mod._build_httpx_cookies_from_storage_strict(store.path)
    minted = _jar(_raw_cookie(value="not-the-return-value"))
    monkeypatch.setattr(store, "read_master_token", Mock(return_value=token))
    monkeypatch.setattr(mint_service, "mint", AsyncMock(return_value=minted))
    monkeypatch.setattr(store, "replace_minted_session", Mock())

    result = await bootstrapper.remint_from_stored_token(
        strict_loader=cookies_mod._build_httpx_cookies_from_storage_strict
    )

    observed_rows = [(c.name, c.value, c.domain, c.path) for c in result.jar]
    expected_rows = [(c.name, c.value, c.domain, c.path) for c in expected.jar]
    assert observed_rows == expected_rows
    assert observed_rows == [
        ("SID", "first", ".google.com", "/"),
        ("__Secure-1PSIDTS", "ts", ".google.com", "/"),
        ("SID", "scoped", ".google.com", "/x"),
    ]
    assert result is not minted


@pytest.mark.asyncio
async def test_oauth_call_time_bridges_observe_midflight_rebinding(tmp_path, monkeypatch):
    storage = tmp_path / "storage_state.json"
    read_started = threading.Event()
    read_release = threading.Event()
    session_started = threading.Event()
    session_release = threading.Event()
    read_count = 0
    late_generator = Mock(return_value="late-id")
    late_owner = Mock(return_value=None)
    late_verifier = AsyncMock(return_value=23)
    token = MasterToken("owner@example.com", "late-id", "secret")

    def read(_store):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            assert read_release.wait(timeout=5)
        return None

    def exchange(_service, email, oauth_token, android_id):
        assert (email, oauth_token, android_id) == (
            "owner@example.com",
            "oauth",
            "late-id",
        )
        return token

    async def mint(_service, received):
        assert received is token
        return _jar()

    def replace(_store, _request):
        session_started.set()
        assert session_release.wait(timeout=5)

    monkeypatch.setattr(ProfileStore, "read_master_token", read)
    monkeypatch.setattr(MintService, "exchange", exchange)
    monkeypatch.setattr(MintService, "mint", mint)
    monkeypatch.setattr(ProfileStore, "replace_minted_session", replace)
    monkeypatch.setattr(ProfileStore, "write_master_token", Mock())

    task = asyncio.create_task(
        mt.bootstrap_from_oauth_token(
            email="owner@example.com",
            oauth_token="oauth",
            storage_path=storage,
        )
    )
    assert await asyncio.to_thread(read_started.wait, 5)
    monkeypatch.setattr(mt, "generate_android_id", late_generator)
    monkeypatch.setattr(storage_mod, "get_account_email_for_storage", late_owner)
    read_release.set()
    assert await asyncio.to_thread(session_started.wait, 5)
    monkeypatch.setattr(mt, "_verify_by_listing_notebooks", late_verifier)
    session_release.set()

    assert await task == 23
    late_generator.assert_called_once_with()
    late_owner.assert_called_once_with(storage)
    late_verifier.assert_awaited_once_with(storage)


@pytest.mark.asyncio
async def test_strict_loader_bridge_observes_midflight_rebinding(tmp_path, monkeypatch):
    storage = tmp_path / "storage_state.json"
    token = MasterToken("owner@example.com", "aid", "secret")
    persist_started = threading.Event()
    persist_release = threading.Event()
    late_result = _jar(_raw_cookie(value="late-strict"))
    late_loader = Mock(return_value=late_result)

    monkeypatch.setattr(ProfileStore, "read_master_token", Mock(return_value=token))
    monkeypatch.setattr(MintService, "mint", AsyncMock(return_value=_jar()))

    def replace(_store, _request):
        persist_started.set()
        assert persist_release.wait(timeout=5)

    monkeypatch.setattr(ProfileStore, "replace_minted_session", replace)
    task = asyncio.create_task(mt.remint_from_stored_token(storage))
    assert await asyncio.to_thread(persist_started.wait, 5)
    monkeypatch.setattr(cookies_mod, "_build_httpx_cookies_from_storage_strict", late_loader)
    persist_release.set()

    assert await task is late_result
    late_loader.assert_called_once_with(storage)


class _ScriptedLock:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.acquire_calls: list[bool] = []
        self.released = False

    def acquire(self, *, blocking):
        self.acquire_calls.append(blocking)
        action = next(self.actions, None)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            action()

    def release(self):
        self.released = True


def _bootstrapper_with_lock(tmp_path, lock):
    store = ProfileStore(tmp_path / "storage_state.json")
    return (
        MasterTokenBootstrapper(
            mint_service=MintService(),
            store=store,
            bootstrap_lock=lock,
            verifier=AsyncMock(),
        ),
        store,
    )


@pytest.mark.asyncio
async def test_missing_storage_probe_priority_and_all_four_outcomes(tmp_path, monkeypatch):
    # Storage on entry wins without a token probe, lock, or re-mint.
    present_dir = tmp_path / "present"
    present_dir.mkdir()
    present_lock = _ScriptedLock()
    present, present_store = _bootstrapper_with_lock(present_dir, present_lock)
    present_store.path.write_text("{}", encoding="utf-8")
    present_remint = AsyncMock()
    monkeypatch.setattr(present, "remint_from_stored_token", present_remint)
    assert (
        await present.bootstrap_storage(strict_loader=Mock()) is BootstrapOutcome.PRESENT_ON_ENTRY
    )
    assert present_lock.acquire_calls == []
    present_remint.assert_not_awaited()

    # No sibling token wins without taking the lock.
    absent_dir = tmp_path / "absent"
    absent_dir.mkdir()
    absent_lock = _ScriptedLock()
    absent, _ = _bootstrapper_with_lock(absent_dir, absent_lock)
    absent_remint = AsyncMock()
    monkeypatch.setattr(absent, "remint_from_stored_token", absent_remint)
    assert await absent.bootstrap_storage(strict_loader=Mock()) is BootstrapOutcome.NO_TOKEN
    assert absent_lock.acquire_calls == []
    absent_remint.assert_not_awaited()

    # A waiter rechecks storage first after acquiring the lock.
    waited_dir = tmp_path / "waited"
    waited_dir.mkdir()
    waited_storage = waited_dir / "storage_state.json"
    (waited_dir / "master_token.json").write_text("{}", encoding="utf-8")
    waited_lock = _ScriptedLock(lambda: waited_storage.write_text("{}", encoding="utf-8"))
    waited, _ = _bootstrapper_with_lock(waited_dir, waited_lock)
    waited_remint = AsyncMock()
    monkeypatch.setattr(waited, "remint_from_stored_token", waited_remint)
    assert (
        await waited.bootstrap_storage(strict_loader=Mock()) is BootstrapOutcome.PRESENT_AFTER_WAIT
    )
    assert waited_lock.acquire_calls == [False]
    assert waited_lock.released is True
    waited_remint.assert_not_awaited()

    # If the token disappears while waiting, it is rechecked after storage.
    removed_dir = tmp_path / "removed"
    removed_dir.mkdir()
    removed_token = removed_dir / "master_token.json"
    removed_token.write_text("{}", encoding="utf-8")
    removed_lock = _ScriptedLock(removed_token.unlink)
    removed, _ = _bootstrapper_with_lock(removed_dir, removed_lock)
    removed_remint = AsyncMock()
    monkeypatch.setattr(removed, "remint_from_stored_token", removed_remint)
    assert await removed.bootstrap_storage(strict_loader=Mock()) is BootstrapOutcome.NO_TOKEN
    assert removed_lock.released is True
    removed_remint.assert_not_awaited()

    # Only the final state creates one re-mint task.
    minted_dir = tmp_path / "minted"
    minted_dir.mkdir()
    (minted_dir / "master_token.json").write_text("{}", encoding="utf-8")
    minted_lock = _ScriptedLock(None)
    minted, _ = _bootstrapper_with_lock(minted_dir, minted_lock)
    minted_remint = AsyncMock(return_value=_jar())
    monkeypatch.setattr(minted, "remint_from_stored_token", minted_remint)
    loader = Mock()
    assert await minted.bootstrap_storage(strict_loader=loader) is BootstrapOutcome.MINTED
    minted_remint.assert_awaited_once_with(strict_loader=loader)
    assert minted_lock.released is True


@pytest.mark.asyncio
async def test_bootstrap_lock_polls_nonblocking_at_exact_50ms(tmp_path, monkeypatch):
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")
    lock = _ScriptedLock(Timeout("busy"), None)
    bootstrapper, _ = _bootstrapper_with_lock(tmp_path, lock)
    remint = AsyncMock(return_value=_jar())
    sleep = AsyncMock()
    monkeypatch.setattr(bootstrapper, "remint_from_stored_token", remint)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert await bootstrapper.bootstrap_storage(strict_loader=Mock()) is BootstrapOutcome.MINTED

    assert lock.acquire_calls == [False, False]
    sleep.assert_awaited_once_with(0.05)
    assert lock.released is True


@pytest.mark.asyncio
async def test_bootstrap_lock_timeout_is_bounded_typed_and_never_releases(tmp_path, monkeypatch):
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")
    lock = _ScriptedLock(Timeout("busy"))
    bootstrapper, _ = _bootstrapper_with_lock(tmp_path, lock)
    remint = AsyncMock(return_value=_jar())
    loop = Mock()
    loop.time.side_effect = [0.0, 90.0]
    monkeypatch.setattr(bootstrapper, "remint_from_stored_token", remint)
    monkeypatch.setattr(asyncio, "get_running_loop", Mock(return_value=loop))

    with pytest.raises(_BootstrapError, match="Timed out waiting") as raised:
        await bootstrapper.bootstrap_storage(strict_loader=Mock())

    assert isinstance(raised.value.__cause__, Timeout)
    assert lock.acquire_calls == [False]
    assert lock.released is False
    remint.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_remint_settlement_before_release(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")
    events: list[str] = []

    class OrderedLock(_ScriptedLock):
        def release(self):
            events.append("release")
            super().release()

    lock = OrderedLock(None)
    bootstrapper, _ = _bootstrapper_with_lock(tmp_path, lock)
    started = asyncio.Event()
    release = asyncio.Event()

    async def remint(*, strict_loader):
        del strict_loader
        events.append("remint-start")
        started.set()
        await release.wait()
        events.append("remint-settled")
        return _jar()

    monkeypatch.setattr(bootstrapper, "remint_from_stored_token", remint)
    task = asyncio.create_task(bootstrapper.bootstrap_storage(strict_loader=Mock()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert lock.released is False
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert lock.released is False
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["remint-start", "remint-settled", "release"]
    assert lock.released is True


@pytest.mark.asyncio
async def test_cancellation_before_lock_acquisition_leaks_no_lock(tmp_path):
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")

    class BusyLock(_ScriptedLock):
        def acquire(self, *, blocking):
            self.acquire_calls.append(blocking)
            raise Timeout("busy")

    lock = BusyLock()
    bootstrapper, _ = _bootstrapper_with_lock(tmp_path, lock)
    task = asyncio.create_task(bootstrapper.bootstrap_storage(strict_loader=Mock()))
    while not lock.acquire_calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock.acquire_calls == [False]
    assert lock.released is False


@pytest.mark.asyncio
async def test_bootstrap_logs_only_successfully_resolved_outcomes(tmp_path, monkeypatch, caplog):
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")
    lock = _ScriptedLock(None)
    bootstrapper, _ = _bootstrapper_with_lock(tmp_path, lock)
    failure = RuntimeError("failed remint")
    monkeypatch.setattr(
        bootstrapper,
        "remint_from_stored_token",
        AsyncMock(side_effect=failure),
    )

    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth.master_token"),
        pytest.raises(RuntimeError) as raised,
    ):
        await bootstrapper.bootstrap_storage(strict_loader=Mock())

    assert raised.value is failure
    assert not [record for record in caplog.records if "bootstrap_storage" in record.message]
    assert lock.released is True


@pytest.mark.asyncio
async def test_bootstrap_success_log_keeps_exact_format(tmp_path, caplog):
    bootstrapper, _, _, _ = _bootstrapper(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth.master_token"):
        outcome = await bootstrapper.bootstrap_storage(strict_loader=Mock())

    assert outcome is BootstrapOutcome.NO_TOKEN
    messages = [
        record.getMessage()
        for record in caplog.records
        if "bootstrap_storage_from_master_token" in record.getMessage()
    ]
    assert messages == [
        f"bootstrap_storage_from_master_token({tmp_path / 'storage_state.json'}) -> "
        "BootstrapOutcome.NO_TOKEN"
    ]
