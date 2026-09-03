"""Unit coverage for the neutral root and web-owned resource lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._auth.storage import snapshot_cookie_jar
from notebooklm._runtime.config import CORE_LOGGER_NAME
from notebooklm._runtime.helpers import _resolve_keepalive_interval
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm._web.transport.kernel import Kernel
from notebooklm._web.transport.lifecycle import (
    WebTransportLifecycle,
    _default_cookie_rotator,
)
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits
from tests._helpers.client_factory import build_client_shell_for_tests


@dataclass
class _Supervisor:
    events: list[str] = field(default_factory=list)

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events.append("bind:supervisor")

    def reset_after_open(self) -> None:
        self.events.append("reset:supervisor")

    def prepare_generation(self, epoch: int) -> None:
        self.events.append(f"prepare-generation:{epoch}")

    def start_accepting(self, epoch: int) -> None:
        self.events.append(f"accept:{epoch}")

    async def stop_accepting(self, epoch: int) -> None:
        self.events.append(f"stop:{epoch}")

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None:
        self.events.append(f"idle:{epoch}:{timeout}")

    async def begin_closing(self, epoch: int) -> None:
        self.events.append(f"closing:{epoch}")

    def mark_closed(self, epoch: int) -> None:
        self.events.append(f"closed:{epoch}")

    async def run_drain_hooks(self) -> None:
        self.events.append("hooks")


@dataclass
class _Participant:
    name: str
    events: list[str]

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events.append(f"bind:{self.name}")

    def reset_after_open(self) -> None:
        self.events.append(f"reset:{self.name}")


@dataclass
class _Transport:
    name: str
    events: list[str]
    opens: list[int] = field(default_factory=list)

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        self.opens.append(epoch)
        self.events.append(f"open:{self.name}:{epoch}")

    async def prepare_close(self) -> None:
        self.events.append(f"prepare:{self.name}")

    async def close_resources(self) -> None:
        self.events.append(f"close:{self.name}")


def _make_root(
    *,
    supervisor: _Supervisor | None = None,
    transports: tuple[_Transport, ...] = (),
    participants: tuple[_Participant, ...] = (),
) -> ClientLifecycle:
    owner = supervisor or _Supervisor()
    return ClientLifecycle(
        supervisor=owner,
        transports=transports,
        loop_participants=(owner, *participants),
    )


@dataclass
class _WebFixture:
    lifecycle: WebTransportLifecycle
    auth: AuthTokens
    auth_coord: MagicMock
    persistence: MagicMock
    kernel: Kernel


def _make_web(
    *,
    auth: AuthTokens | None = None,
    keepalive_interval: float | None = None,
    keepalive_storage_path: Path | None = None,
    cookie_persistence_path: Path | None = None,
    cookie_saver: Any = None,
    cookie_rotator: Any = _default_cookie_rotator,
    canonical_save_error: BaseException | None = None,
    async_client_factory: Any = httpx.AsyncClient,
) -> _WebFixture:
    resolved_auth = auth or AuthTokens(
        csrf_token="CSRF",
        session_id="SID",
        cookies={"SID": "v1"},
    )
    auth_coord = MagicMock()
    auth_coord.cancel_inflight_refresh = AsyncMock()
    persistence = MagicMock()
    persistence._prepare_open_baseline = AsyncMock()
    persistence.capture_open_snapshot = MagicMock()
    persistence._save_canonical = AsyncMock(side_effect=canonical_save_error)
    persistence._save_v0_callback = AsyncMock()
    persistence.loaded_cookie_snapshot = None
    kernel = Kernel(auth=resolved_auth, async_client_factory=async_client_factory)
    lifecycle = WebTransportLifecycle(
        auth=resolved_auth,
        auth_coord=auth_coord,
        cookie_persistence=persistence,
        kernel=kernel,
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=keepalive_interval,
        keepalive_storage_path=keepalive_storage_path,
        cookie_persistence_path=cookie_persistence_path,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )
    return _WebFixture(lifecycle, resolved_auth, auth_coord, persistence, kernel)


async def _close_web(fixture: _WebFixture) -> None:
    await fixture.lifecycle.prepare_close()
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_root_open_is_idempotent_and_preserves_transport_generation() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events)
    transport = _Transport("web", events)
    lifecycle = _make_root(supervisor=supervisor, transports=(transport,))

    await lifecycle.open()
    await lifecycle.open()

    assert lifecycle.is_open()
    assert transport.opens == [1]
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_root_open_binds_and_resets_every_frozen_participant() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events)
    reqid = _Participant("reqid", events)
    chat = _Participant("chat", events)
    lifecycle = _make_root(supervisor=supervisor, participants=(reqid, chat))

    assert lifecycle.get_bound_loop() is None
    await lifecycle.open()

    assert lifecycle.get_bound_loop() is asyncio.get_running_loop()
    assert events[:6] == [
        "bind:supervisor",
        "reset:supervisor",
        "bind:reqid",
        "reset:reqid",
        "bind:chat",
        "reset:chat",
    ]
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_root_close_runs_hooks_before_transport_resource_teardown() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events)
    transport = _Transport("web", events)
    lifecycle = _make_root(supervisor=supervisor, transports=(transport,))

    await lifecycle.open()
    await lifecycle.close(drain=False)

    assert events.index("prepare:web") < events.index("hooks")
    assert events.index("hooks") < events.index("close:web")
    assert events.index("close:web") < events.index("closed:1")
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_root_close_before_open_is_noop() -> None:
    supervisor = _Supervisor()
    lifecycle = _make_root(supervisor=supervisor)

    await lifecycle.close()

    assert supervisor.events == []
    assert not lifecycle.is_open()


def test_root_construction_is_loop_agnostic_and_freezes_ownership_graph() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events)
    transport = _Transport("web", events)
    participant = _Participant("reqid", events)
    transports = [transport]
    participants = [supervisor, participant]

    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=transports,
        loop_participants=participants,
    )
    transports.clear()
    participants.clear()

    assert lifecycle._transports == (transport,)
    assert lifecycle._loop_participants == (supervisor, participant)
    assert lifecycle.get_bound_loop() is None
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_web_open_is_idempotent_and_preserves_http_client() -> None:
    fixture = _make_web()

    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)
    first_client = fixture.kernel.http_client
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)

    assert first_client is not None
    assert fixture.kernel.http_client is first_client
    fixture.persistence._prepare_open_baseline.assert_awaited_once()
    await _close_web(fixture)


@pytest.mark.asyncio
async def test_web_open_captures_normalized_live_cookie_snapshot() -> None:
    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    fixture = _make_web(auth=auth)
    mirrored = snapshot_cookie_jar(httpx.Cookies({"SID": "v2"}))
    fixture.persistence.loaded_cookie_snapshot = mirrored

    await fixture.lifecycle.open(asyncio.get_running_loop(), 7)
    try:
        fixture.persistence.capture_open_snapshot.assert_called_once()
        passed_jar = fixture.persistence.capture_open_snapshot.call_args.args[0]
        assert fixture.kernel.http_client is not None
        assert passed_jar is fixture.kernel.http_client.cookies
        assert auth.cookie_snapshot is mirrored
        fixture.auth_coord.activate_epoch.assert_called_once_with(7)
    finally:
        await _close_web(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("injection_mode", [None, "429"])
async def test_web_open_always_uses_default_httpx_transport(
    monkeypatch: pytest.MonkeyPatch,
    injection_mode: str | None,
) -> None:
    from notebooklm._web.transport import error_injection as error_injection

    monkeypatch.setattr(error_injection, "_get_error_injection_mode", lambda: injection_mode)
    fixture = _make_web()

    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)
    try:
        client = fixture.kernel.http_client
        assert client is not None
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
    finally:
        await _close_web(fixture)


@pytest.mark.asyncio
async def test_web_prepare_close_fences_generation_and_cancels_keepalive() -> None:
    rotator = AsyncMock()
    fixture = _make_web(keepalive_interval=60.0, cookie_rotator=rotator)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 3)
    task = fixture.lifecycle._keepalive_task
    assert task is not None and not task.done()

    await fixture.lifecycle.prepare_close()

    assert fixture.lifecycle._keepalive_task is None
    assert task.done()
    fixture.auth_coord.fence_epoch.assert_called_once_with(3)
    fixture.auth_coord.cancel_inflight_refresh.assert_awaited_once()
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_web_prepare_close_re_raises_captured_keepalive_process_exit() -> None:
    process_exit = KeyboardInterrupt("keepalive shutdown")

    async def rotator(client: httpx.AsyncClient, path: Path | None) -> None:
        del client, path
        raise process_exit

    fixture = _make_web(keepalive_interval=0.001, cookie_rotator=rotator)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 3)
    task = fixture.lifecycle._keepalive_task
    assert task is not None
    for _ in range(100):
        if task.done():
            break
        await asyncio.sleep(0.001)

    with pytest.raises(KeyboardInterrupt, match="keepalive shutdown") as raised:
        await fixture.lifecycle.prepare_close()

    assert raised.value is process_exit
    assert task.done()
    assert task.result().error is process_exit
    fixture.auth_coord.cancel_inflight_refresh.assert_awaited_once()
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_web_close_resources_saves_then_closes_kernel() -> None:
    fixture = _make_web()
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)
    assert fixture.kernel.http_client is not None

    await _close_web(fixture)

    fixture.persistence._save_canonical.assert_awaited_once()
    assert fixture.kernel.http_client is None


@pytest.mark.asyncio
async def test_web_close_resources_preserves_cookie_save_process_exit_over_close_failure() -> None:
    process_exit = SystemExit("cookie save shutdown")
    close_failure = RuntimeError("kernel close failed")

    class _FailingCloseClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            await super().aclose()
            raise close_failure

    fixture = _make_web(
        canonical_save_error=process_exit,
        async_client_factory=_FailingCloseClient,
    )
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)

    with pytest.raises(SystemExit, match="cookie save shutdown") as raised:
        await fixture.lifecycle.close_resources()

    assert raised.value is process_exit
    assert raised.value.__cause__ is close_failure
    assert fixture.lifecycle._active_epoch is None


@pytest.mark.asyncio
async def test_web_save_cookies_uses_canonical_owner_and_mirrors_snapshot(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "storage.json"
    fixture = _make_web(cookie_persistence_path=path)
    mirrored = snapshot_cookie_jar(httpx.Cookies({"SID": "v2"}))
    fixture.persistence.loaded_cookie_snapshot = mirrored
    secret = "canonical-cookie-secret-sentinel"
    jar = httpx.Cookies({"SID": secret})

    with caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME):
        await fixture.lifecycle.save_cookies(jar)

    fixture.persistence._save_canonical.assert_awaited_once_with(
        jar,
        path,
        to_thread=asyncio.to_thread,
    )
    fixture.persistence._save_v0_callback.assert_not_awaited()
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Cookie persistence route:")
    ]
    assert messages == [
        f"Cookie persistence route: type=canonical_store status=dispatch path={path}"
    ]
    assert secret not in "\n".join(messages)
    assert fixture.auth.cookie_snapshot is mirrored


@pytest.mark.asyncio
async def test_web_save_cookies_uses_explicit_callback_without_logging_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "storage.json"
    saver = MagicMock(return_value=True)
    fixture = _make_web(cookie_persistence_path=path, cookie_saver=saver)
    secret = "explicit-cookie-secret-sentinel"
    jar = httpx.Cookies({"SID": secret})

    with caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME):
        await fixture.lifecycle.save_cookies(jar)

    fixture.persistence._save_canonical.assert_not_awaited()
    fixture.persistence._save_v0_callback.assert_awaited_once_with(
        jar,
        path,
        save_cookies_to_storage=saver,
        to_thread=asyncio.to_thread,
    )
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Cookie persistence route:")
    ]
    assert messages == [
        f"Cookie persistence route: type=explicit_v0_callback status=dispatch path={path}"
    ]
    assert secret not in "\n".join(messages)


@pytest.mark.asyncio
async def test_web_save_cookies_rejects_retired_expected_epoch() -> None:
    fixture = _make_web()
    await fixture.lifecycle.open(asyncio.get_running_loop(), 4)
    await fixture.lifecycle.prepare_close()

    with pytest.raises(RuntimeError, match="generation is retired"):
        await fixture.lifecycle.save_cookies(httpx.Cookies(), expected_epoch=4)

    fixture.persistence._save_canonical.assert_not_awaited()
    await fixture.lifecycle.close_resources()


def test_bound_loop_mismatch_via_client_raises_runtime_error() -> None:
    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    client = build_client_shell_for_tests(auth=auth)

    async def open_on_loop_a() -> None:
        await client.__aenter__()

    async def open_on_loop_b() -> Exception | None:
        try:
            await client.__aenter__()
        except RuntimeError as exc:
            return exc
        return None

    asyncio.run(open_on_loop_a())
    exc = asyncio.run(open_on_loop_b())
    assert isinstance(exc, RuntimeError)
    assert "loop" in str(exc).lower()


def test_resolve_keepalive_interval_clamps_to_min_floor() -> None:
    assert _resolve_keepalive_interval(keepalive=1.0, min_interval=60.0) == 60.0


def test_resolve_keepalive_interval_passes_through_above_floor() -> None:
    assert _resolve_keepalive_interval(keepalive=120.0, min_interval=60.0) == 120.0


def test_resolve_keepalive_interval_none_disables() -> None:
    assert _resolve_keepalive_interval(keepalive=None, min_interval=60.0) is None


def test_resolve_keepalive_interval_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=0, min_interval=60.0)
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=-1.0, min_interval=60.0)
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=1.0, min_interval=0)


def test_web_construction_is_event_loop_agnostic_and_owns_resource_config() -> None:
    path = Path("/tmp/storage.json")
    fixture = _make_web(
        keepalive_interval=60.0,
        keepalive_storage_path=path,
        cookie_persistence_path=path,
    )

    assert fixture.kernel.http_client is None
    assert fixture.lifecycle._keepalive_task is None
    assert fixture.lifecycle._keepalive_interval == 60.0
    assert fixture.lifecycle._keepalive_storage_path == path
    assert fixture.lifecycle._cookie_persistence_path == path
    assert fixture.lifecycle._active_epoch is None


@pytest.mark.asyncio
async def test_default_cookie_rotator_late_binds_to_canonical_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notebooklm._auth import keepalive as keepalive_module

    sentinel = AsyncMock(return_value=None)
    monkeypatch.setattr(keepalive_module, "_rotate_cookies", sentinel)
    client = MagicMock(spec=httpx.AsyncClient)
    path = Path("/tmp/storage.json")

    await _default_cookie_rotator(client, path)

    sentinel.assert_awaited_once_with(client, path)


def test_assembly_selects_default_and_custom_web_rotators() -> None:
    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    default_client = build_client_shell_for_tests(auth)
    custom_rotator = AsyncMock(return_value=None)
    custom_client = build_client_shell_for_tests(auth, cookie_rotator=custom_rotator)

    assert default_client._web_runtime.web_transport._cookie_rotator is _default_cookie_rotator
    assert custom_client._web_runtime.web_transport._cookie_rotator is custom_rotator


def test_production_assembly_freezes_exact_root_ownership_graph() -> None:
    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    client = build_client_shell_for_tests(auth)
    collaborators = client._collaborators
    web = client._web_runtime
    lifecycle = collaborators.lifecycle

    assert lifecycle._supervisor is collaborators.call_supervisor
    assert lifecycle._transports == (
        web.web_transport,
        web.source_uploader,
    )
    assert lifecycle._loop_participants == (
        collaborators.call_supervisor,
        web.reqid,
        web.auth_coord,
        client.chat,
    )
    assert lifecycle._transports.count(web.source_uploader) == 1
    assert web.source_uploader not in lifecycle._loop_participants
    assert client.sources._supervisor is collaborators.call_supervisor
    assert web.source_uploader._supervisor is collaborators.call_supervisor
    assert web.source_uploader._rpc is web.executor
    assert web.source_uploader._kernel is web.kernel
    assert web.composed.runtime_collaborators is collaborators
    assert web.composed.runtime_collaborators.lifecycle is lifecycle
