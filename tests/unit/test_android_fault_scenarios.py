"""Contract checks for the Android fault scenario entry point."""

from __future__ import annotations

import asyncio

import grpc
import pytest

from tests._fault_server.android_scenarios import SCENARIOS, run_scenario
from tests._fault_server.common import ScenarioResult
from tests._fault_server.grpc import GENERATE_STREAMED, GET_PROJECT, GrpcAction, GrpcFaultServer


class _LifecycleServer:
    def __init__(
        self,
        *,
        bind_error: BaseException | None = None,
        block_start: bool = False,
        stop_error: BaseException | None = None,
    ) -> None:
        self.bind_error = bind_error
        self.block_start = block_start
        self.stop_error = stop_error
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.stop_calls = 0
        self.termination_waits = 0

    def add_generic_rpc_handlers(self, _handlers: object) -> None:
        return None

    def add_insecure_port(self, _address: str) -> int:
        if self.bind_error is not None:
            raise self.bind_error
        return 12345

    async def start(self) -> None:
        self.start_entered.set()
        if self.block_start:
            await self.release_start.wait()

    async def stop(self, _grace: float) -> None:
        self.stop_calls += 1
        self.release_start.set()
        if self.stop_error is not None:
            raise self.stop_error

    async def wait_for_termination(self) -> None:
        self.termination_waits += 1


@pytest.mark.asyncio
async def test_android_fault_scenarios_reject_unknown_name_before_opening_resources() -> None:
    with pytest.raises(ValueError, match="unknown Android fault scenario"):
        await run_scenario("not-a-scenario", operation_id="unit-unknown")


@pytest.mark.asyncio
async def test_android_fault_scenarios_require_matching_supplied_result_identity() -> None:
    result = ScenarioResult("android", SCENARIOS[0], "different-operation")

    with pytest.raises(ValueError, match="identity"):
        await run_scenario(SCENARIOS[0], operation_id="unit-operation", result=result)


@pytest.mark.parametrize(
    ("method", "action", "match"),
    [
        (GET_PROJECT, GrpcAction("bogus"), "invalid"),
        (GET_PROJECT, GrpcAction("abort"), "status"),
        (GET_PROJECT, GrpcAction("reply", status=grpc.StatusCode.INTERNAL), "status"),
        (GET_PROJECT, GrpcAction("wait_reply", gate=""), "gate"),
        (GENERATE_STREAMED, GrpcAction("reply"), "invalid"),
        (GET_PROJECT, GrpcAction("stream", response=()), "invalid"),
    ],
)
def test_grpc_fault_plan_rejects_invalid_action_before_server_open(
    method: str, action: GrpcAction, match: str
) -> None:
    server = GrpcFaultServer()

    with pytest.raises(ValueError, match=match):
        server.plan(method, action)

    assert not server.actions[method]


@pytest.mark.asyncio
async def test_grpc_fault_server_cleans_up_when_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _LifecycleServer(bind_error=RuntimeError("synthetic bind failure"))
    monkeypatch.setattr(grpc.aio, "server", lambda: lifecycle)

    with pytest.raises(RuntimeError, match="synthetic bind failure"):
        await GrpcFaultServer().__aenter__()

    assert lifecycle.stop_calls == 1
    assert lifecycle.termination_waits == 1


@pytest.mark.asyncio
async def test_grpc_fault_server_cleans_up_when_start_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _LifecycleServer(block_start=True)
    monkeypatch.setattr(grpc.aio, "server", lambda: lifecycle)
    task = asyncio.create_task(GrpcFaultServer().__aenter__())
    await lifecycle.start_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lifecycle.stop_calls == 1
    assert lifecycle.termination_waits == 1


@pytest.mark.asyncio
async def test_grpc_fault_server_still_waits_for_termination_when_stop_fails() -> None:
    lifecycle = _LifecycleServer(stop_error=RuntimeError("synthetic stop failure"))
    server = GrpcFaultServer()
    server._server = lifecycle  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="synthetic stop failure"):
        await server.__aexit__(None, None, None)

    assert lifecycle.stop_calls == 1
    assert lifecycle.termination_waits == 1


@pytest.mark.asyncio
async def test_unknown_grpc_path_is_journaled_as_a_harness_failure() -> None:
    server = GrpcFaultServer()
    async with server:
        channel = grpc.aio.insecure_channel(server.target)
        call = channel.unary_unary(
            "/fault.Unknown/Method",
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await call(b"request")
            assert raised.value.code() is grpc.StatusCode.UNIMPLEMENTED
        finally:
            await channel.close()

        with pytest.raises(AssertionError, match="unexpected request: /fault.Unknown/Method"):
            server.assert_consumed()


@pytest.mark.asyncio
async def test_unexpected_handler_exception_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    server = GrpcFaultServer()
    server.plan(GET_PROJECT, GrpcAction("reply"))

    def explode(_method: str) -> GrpcAction:
        raise RuntimeError("synthetic handler bug")

    monkeypatch.setattr(server, "_next", explode)
    async with server:
        channel = grpc.aio.insecure_channel(server.target)
        call = channel.unary_unary(GET_PROJECT)
        try:
            with pytest.raises(grpc.aio.AioRpcError):
                await call(b"")
        finally:
            await channel.close()

        with pytest.raises(AssertionError, match="synthetic handler bug"):
            server.assert_consumed()
