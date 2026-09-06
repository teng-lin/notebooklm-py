"""Production-assembly Android client bound to :mod:`.grpc` loopback servers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import grpc

from notebooklm._android import assembly as android_assembly
from notebooklm._android.auth import NOTEBOOKLM_OAUTH_SPEC
from notebooklm._android.session import ANDROID_GRPC_TARGET, AndroidSession
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import MintedOAuthToken
from notebooklm.auth import AuthTokens
from tests._helpers.client_factory import build_client_shell_for_tests

from .grpc import GrpcFaultServer


@dataclass
class SyntheticMasterTokenReader:
    """A real BearerProvider input with no disk or user-profile dependency."""

    reads: int = 0

    def read_master_token(self) -> MasterToken:
        self.reads += 1
        return MasterToken(
            email="fault@example.test", android_id="fault-device", secret="fault-master"
        )


@dataclass
class SyntheticOAuthMinter:
    tokens: list[str] = field(default_factory=lambda: ["fault-bearer-1", "fault-bearer-2"])
    error: Exception | None = None
    block_after: int | None = None
    release: asyncio.Event | None = None
    calls: int = 0

    async def mint_oauth(self, token: MasterToken, spec: object) -> MintedOAuthToken:
        assert token.email == "fault@example.test"
        assert spec == NOTEBOOKLM_OAUTH_SPEC
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.block_after is not None and self.calls >= self.block_after:
            assert self.release is not None
            await self.release.wait()
        value = self.tokens[min(self.calls - 1, len(self.tokens) - 1)]
        return MintedOAuthToken(value, int(time.time()) + 3600)


@dataclass
class AndroidHarness:
    client: Any
    minter: SyntheticOAuthMinter
    bearer: Any
    channels: list[Any]
    targets: list[str]


async def no_sleep(_seconds: float) -> None:
    """Keep retry-count scenarios bounded while retaining real socket attempts."""


def build_android_client(
    server: GrpcFaultServer,
    *,
    timeout: float = 0.5,
    rate_limit_max_retries: int = 1,
    server_error_max_retries: int = 1,
    minter: SyntheticOAuthMinter | None = None,
    sleep: Callable[[float], Awaitable[Any]] = no_sleep,
) -> AndroidHarness:
    """Synchronously assemble a public client with its real bearer provider.

    The temporary class patch lasts only for synchronous production assembly;
    each constructed session captures its local-channel loader before the
    module binding is restored.  This keeps concurrent scenario cohorts from
    sharing global test state or changing production endpoint routing.
    """

    channels: list[Any] = []
    targets: list[str] = []

    def secure_channel(target: str, _credentials: object, *, options: Any) -> Any:
        targets.append(target)
        if target != ANDROID_GRPC_TARGET:
            raise AssertionError(f"production Android target changed: {target}")
        channel = grpc.aio.insecure_channel(server.target, options=options)
        channels.append(channel)
        return channel

    grpc_loader = SimpleNamespace(
        ssl_channel_credentials=lambda: object(),
        aio=SimpleNamespace(secure_channel=secure_channel),
    )
    production_session = AndroidSession

    def local_session(
        bearer_provider: Any,
        supervisor: Any,
        *,
        timeout: float | None,
        rate_limit_max_retries: int,
        server_error_max_retries: int,
        refresh_retry_delay: float,
        metrics: Any,
        sleep: Any,
    ) -> AndroidSession:
        return production_session(
            bearer_provider,
            supervisor,
            timeout=timeout,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            refresh_retry_delay=refresh_retry_delay,
            metrics=metrics,
            sleep=sleep,
            grpc_loader=lambda: grpc_loader,
        )

    selected_minter = minter or SyntheticOAuthMinter()
    with patch.object(android_assembly, "AndroidSession", local_session):
        client = build_client_shell_for_tests(
            AuthTokens(
                cookies={("SID", ".google.com", "/"): "fault-cookie"},
                csrf_token="fault-csrf",
                session_id="fault",
            ),
            backend="android",
            timeout=timeout,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            sleep=sleep,
            master_token_reader=SyntheticMasterTokenReader(),
            oauth_minter=selected_minter,
        )
    assert client._android_runtime is not None
    return AndroidHarness(
        client=client,
        minter=selected_minter,
        bearer=client._android_runtime.bearer_provider,
        channels=channels,
        targets=targets,
    )


__all__ = [
    "AndroidHarness",
    "SyntheticMasterTokenReader",
    "SyntheticOAuthMinter",
    "build_android_client",
    "no_sleep",
]
