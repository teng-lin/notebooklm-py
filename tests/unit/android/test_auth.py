from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

import notebooklm._android.auth as android_auth
from notebooklm._android.auth import NOTEBOOKLM_OAUTH_SPEC, BearerProvider
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import MintedOAuthToken, OAuthMintError
from notebooklm.exceptions import AuthError, ConfigurationError, MissingDependencyError

MASTER_SECRET = "aas_et/never-render-this-master"
BEARER_SECRET = "ya29.never-render-this-bearer"


@dataclass
class _Profile:
    record: MasterToken | None
    reads: int = 0
    thread_id: int | None = None

    def read_master_token(self) -> MasterToken | None:
        self.reads += 1
        self.thread_id = threading.get_ident()
        return self.record


class _Minter:
    def __init__(self, results: list[MintedOAuthToken | BaseException]) -> None:
        self.results = results
        self.calls = 0
        self.specs = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def mint_oauth(self, master_token, spec):
        assert master_token.secret == MASTER_SECRET
        self.calls += 1
        self.specs.append(spec)
        self.started.set()
        if self.block:
            await self.release.wait()
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _record() -> MasterToken:
    return MasterToken(email="person@example.com", android_id="1234", secret=MASTER_SECRET)


async def _activate(provider: BearerProvider, epoch: int = 1) -> None:
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()
    await provider.activate(epoch)


@pytest.mark.asyncio
async def test_activate_reads_off_loop_and_uses_exact_notebooklm_spec() -> None:
    profile = _Profile(_record())
    minter = _Minter([MintedOAuthToken(BEARER_SECRET, int(time.time()) + 3600)])
    provider = BearerProvider(profile, minter)

    assert profile.reads == 0
    await _activate(provider)
    assert profile.thread_id != threading.get_ident()
    credential = await provider.get(1)

    assert credential.token == BEARER_SECRET
    assert minter.specs == [NOTEBOOKLM_OAUTH_SPEC]
    assert NOTEBOOKLM_OAUTH_SPEC.app == "com.google.android.apps.labs.language.tailwind"
    assert NOTEBOOKLM_OAUTH_SPEC.client_sig == "a3382adf91991e6ef1e7e7de309c1febfedf3283"
    assert NOTEBOOKLM_OAUTH_SPEC.service.startswith("oauth2:")
    assert len(NOTEBOOKLM_OAUTH_SPEC.service.removeprefix("oauth2:").split(" ")) == 10


@pytest.mark.asyncio
async def test_single_flight_survives_one_cancelled_waiter() -> None:
    minter = _Minter([MintedOAuthToken(BEARER_SECRET, int(time.time()) + 3600)])
    minter.block = True
    provider = BearerProvider(_Profile(_record()), minter)
    await _activate(provider)

    cancelled = asyncio.create_task(provider.get(1))
    survivor = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    minter.release.set()

    assert (await survivor).token == BEARER_SECRET
    assert minter.calls == 1


@pytest.mark.asyncio
async def test_failure_is_shared_by_identity_then_later_call_retries() -> None:
    minter = _Minter(
        [
            OAuthMintError("dependency included unsafe detail"),
            MintedOAuthToken(BEARER_SECRET, None),
        ]
    )
    minter.block = True
    provider = BearerProvider(_Profile(_record()), minter)
    await _activate(provider)

    first = asyncio.create_task(provider.get(1))
    second = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    minter.release.set()
    failures = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(failures[0], AuthError)
    assert failures[0] is failures[1]
    assert "unsafe detail" not in str(failures[0])
    assert failures[0].__cause__ is None
    assert failures[0].__context__ is None
    traceback = failures[0].__traceback__
    while traceback is not None:
        local_values = traceback.tb_frame.f_locals
        assert "self" not in local_values
        assert "provider" not in local_values
        assert all(not isinstance(value, MasterToken) for value in local_values.values())
        assert MASTER_SECRET not in repr(local_values)
        assert BEARER_SECRET not in repr(local_values)
        traceback = traceback.tb_next
    assert (await provider.get(1)).token == BEARER_SECRET
    assert minter.calls == 2


@pytest.mark.asyncio
async def test_token_without_expiry_is_shared_only_with_current_waiter_wave() -> None:
    minter = _Minter(
        [
            MintedOAuthToken("ya29.wave-one", None),
            MintedOAuthToken("ya29.wave-two", None),
        ]
    )
    minter.block = True
    provider = BearerProvider(_Profile(_record()), minter)
    await _activate(provider)

    one = asyncio.create_task(provider.get(1))
    two = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    minter.release.set()
    wave = await asyncio.gather(one, two)
    later = await provider.get(1)

    assert {item.token for item in wave} == {"ya29.wave-one"}
    assert later.token == "ya29.wave-two"
    assert minter.calls == 2


@pytest.mark.asyncio
async def test_expiry_cache_and_compare_and_clear_generation() -> None:
    wall = 1_000.0
    monotonic = 50.0
    minter = _Minter(
        [
            MintedOAuthToken("ya29.first", 2_000),
            MintedOAuthToken("ya29.second", 2_000),
        ]
    )
    provider = BearerProvider(
        _Profile(_record()),
        minter,
        wall_clock=lambda: wall,
        monotonic=lambda: monotonic,
    )
    await _activate(provider)

    first = await provider.get(1)
    assert await provider.get(1) is first
    provider.invalidate(first.generation)
    second = await provider.get(1)
    provider.invalidate(first.generation)

    assert second.token == "ya29.second"
    assert await provider.get(1) is second
    assert minter.calls == 2


@pytest.mark.asyncio
async def test_close_fences_cancel_resistant_late_mint_and_redacts_repr() -> None:
    class _CancelResistantMinter:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def mint_oauth(self, master_token, spec):
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return MintedOAuthToken(BEARER_SECRET, int(time.time()) + 3600)

    minter = _CancelResistantMinter()
    provider = BearerProvider(_Profile(_record()), minter)
    await _activate(provider)
    waiter = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    task_repr = repr(provider._mint_task)

    await provider.prepare_close()
    result = await asyncio.gather(waiter, return_exceptions=True)

    assert isinstance(result[0], RuntimeError)
    assert MASTER_SECRET not in repr(provider)
    assert BEARER_SECRET not in repr(provider)
    assert MASTER_SECRET not in task_repr
    assert BEARER_SECRET not in task_repr
    with pytest.raises(RuntimeError, match="not active"):
        await provider.get(1)


@pytest.mark.asyncio
async def test_missing_token_and_dependency_have_sanitized_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_dependency() -> None:
        raise MissingDependencyError("wrong import detail and secret")

    monkeypatch.setattr(android_auth, "_require_gpsoauth", missing_dependency)
    dependency = BearerProvider(_Profile(_record()), _Minter([]))
    dependency.set_bound_loop(asyncio.get_running_loop())
    dependency.reset_after_open()
    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[android\]") as captured:
        await dependency.activate(1)
    assert "wrong import detail" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())
    missing = BearerProvider(_Profile(None), _Minter([]))
    missing.set_bound_loop(asyncio.get_running_loop())
    missing.reset_after_open()
    with pytest.raises(ConfigurationError, match="master-token profile"):
        await missing.activate(1)

    minter = _Minter([MissingDependencyError("wrong extra and secret")])
    provider = BearerProvider(_Profile(_record()), minter)
    await _activate(provider)
    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[android\]") as captured:
        await provider.get(1)
    assert "wrong extra" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_close_discards_late_profile_thread_result() -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingProfile:
        def read_master_token(self):
            started.set()
            release.wait(timeout=2)
            return _record()

    provider = BearerProvider(_BlockingProfile(), _Minter([]))
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()
    activation = asyncio.create_task(provider.activate(1))
    await asyncio.to_thread(started.wait, 2)
    await provider.prepare_close()
    release.set()
    await activation

    with pytest.raises(RuntimeError, match="not active"):
        await provider.get(1)
