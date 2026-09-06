"""Commit-evidence and shared-bearer Android socket scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import grpc

from notebooklm.exceptions import AuthError
from notebooklm.outcomes import CommitState

from .android import SyntheticOAuthMinter, build_android_client
from .common import ScenarioResult
from .grpc import ADD_TENTATIVE_SOURCES, GET_PROJECT, GrpcFaultServer, abort, reply

SCENARIOS = ("bearer_shared_failure_recovery", "tentative_registration_refused")

PLANS: dict[str, tuple[str, ...]] = {
    "bearer_shared_failure_recovery": (
        "two reads:shared bearer mint",
        "mint:fail once",
        "same minter:repair",
        "read:reply",
    ),
    "tentative_registration_refused": (
        "AddTentativeSources:UNAUTHENTICATED",
        "GetProject:recovery",
    ),
}


async def _run_server(
    result: ScenarioResult,
    body: Callable[[GrpcFaultServer], Awaitable[None]],
) -> None:
    server = GrpcFaultServer()
    try:
        async with server:
            await body(server)
            await server.wait_for_idle()
            server.assert_consumed()
    finally:
        result.record(
            "grpc_journal",
            methods=[request.method.rpartition("/")[2] for request in server.requests],
            handler_error_types=list(server.handler_errors),
        )
        result.record(
            "cleanup",
            requests=len(server.requests),
            active_handlers=len(server._active),
        )
        result.require("resilience_grpc_handlers_settled", not server._active)


async def tentative_registration_refused(result: ScenarioResult) -> None:
    async def body(server: GrpcFaultServer) -> None:
        server.plan(ADD_TENTATIVE_SOURCES, abort(grpc.StatusCode.UNAUTHENTICATED))
        server.plan(GET_PROJECT, reply())
        harness = build_android_client(server, server_error_max_retries=3)
        error: BaseException | None = None
        async with harness.client as client:
            try:
                await client.sources.add_text("notebook-1", "Title", "Body")
            except BaseException as caught:
                error = caught
            probe = await client.notebooks.get("notebook-1")
        registrations = [
            request for request in server.requests if request.method == ADD_TENTATIVE_SOURCES
        ]
        result.require("registration_refusal_auth_error", isinstance(error, AuthError))
        result.require(
            "registration_refusal_positive_evidence",
            getattr(error, "commit_state", None) is CommitState.REJECTED
            and not bool(getattr(error, "unconfirmed", False)),
        )
        result.require("registration_refusal_sent_once", len(registrations) == 1)
        result.require("registration_refusal_no_later_stage", len(server.requests) == 2)
        result.require("registration_refusal_recovery_remint", harness.minter.calls == 2)
        result.require("registration_refusal_recovery", probe.id == "notebook-1")

    await _run_server(result, body)


async def bearer_shared_failure_recovery(result: ScenarioResult) -> None:
    async def body(server: GrpcFaultServer) -> None:
        server.plan(GET_PROJECT, reply())
        release = asyncio.Event()
        failure = AuthError("synthetic bearer unavailable")
        minter = SyntheticOAuthMinter(error=failure, block_after=1, release=release)
        harness = build_android_client(server, minter=minter)
        async with harness.client as client:
            first = asyncio.create_task(client.notebooks.get("notebook-1"))
            second = asyncio.create_task(client.notebooks.get("notebook-1"))
            for _ in range(100):
                if harness.bearer._mint_waiters == 2:
                    break
                await asyncio.sleep(0)
            result.require("shared_mint_two_waiters", harness.bearer._mint_waiters == 2)
            release.set()
            failures = await asyncio.gather(first, second, return_exceptions=True)
            minter.error = None
            minter.block_after = None
            probe = await client.notebooks.get("notebook-1")
        result.require(
            "shared_mint_same_failure",
            len(failures) == 2
            and isinstance(failures[0], AuthError)
            and failures[0] is failures[1],
        )
        result.require("shared_mint_failed_once", minter.calls == 2)
        result.require("shared_mint_no_failed_wave_dispatch", len(server.requests) == 1)
        result.require("shared_mint_recovery", probe.id == "notebook-1")

    await _run_server(result, body)


IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "bearer_shared_failure_recovery": bearer_shared_failure_recovery,
    "tentative_registration_refused": tentative_registration_refused,
}

__all__ = ["IMPLEMENTATIONS", "PLANS", "SCENARIOS"]
