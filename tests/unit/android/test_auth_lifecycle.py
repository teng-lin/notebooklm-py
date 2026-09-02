"""Lifecycle, loop-binding, and failure branches of the Android bearer provider.

``tests/unit/android/test_auth.py`` covers the single-flight mint wave and the
happy caching path. These cases target what surrounds it: the loop-binding
assertions, ``reset_after_open``'s task disposal, the sanitized failure
classification in ``_mint_once``, and the expiry-deadline arithmetic. Every one
of these guards a credential path, so an unexercised branch here is the kind
that fails open.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from notebooklm._android.auth import (
    BearerCredential,
    BearerProvider,
    _make_bearer_provider,
    _NoMasterTokenProfile,
)
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import MintedOAuthToken, OAuthMintError
from notebooklm._auth.profile_store import ProfileStore
from notebooklm.exceptions import AuthError, ConfigurationError, MissingDependencyError

MASTER_SECRET = "aas_et/never-render-this-master"
BEARER_SECRET = "ya29.never-render-this-bearer"


def _record() -> MasterToken:
    return MasterToken(email="person@example.com", android_id="1234", secret=MASTER_SECRET)


@dataclass
class _Profile:
    """Profile reader whose ``read_master_token`` can be scripted to fail."""

    record: MasterToken | None = None
    error: BaseException | None = None

    def read_master_token(self) -> MasterToken | None:
        if self.error is not None:
            raise self.error
        return self.record


class _Minter:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.calls = 0

    async def mint_oauth(self, master_token, spec):  # noqa: ANN001, ANN202
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _BlockingMinter:
    """Holds the mint open so a close can be raced against an in-flight wave."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def mint_oauth(self, master_token, spec):  # noqa: ANN001, ANN202
        self.started.set()
        await self.release.wait()
        return self.result


def _provider(
    *,
    profile: object | None = None,
    minter: object | None = None,
    wall_clock=lambda: 1_000.0,  # noqa: ANN001
    monotonic=lambda: 500.0,  # noqa: ANN001
) -> BearerProvider:
    return BearerProvider(
        profile if profile is not None else _Profile(record=_record()),
        minter if minter is not None else _Minter(),
        wall_clock=wall_clock,
        monotonic=monotonic,
    )


async def _activate(provider: BearerProvider, epoch: int = 1) -> None:
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()
    await provider.activate(epoch)


# ---------------------------------------------------------------------------
# Credential value + profile reader
# ---------------------------------------------------------------------------


def test_bearer_credential_repr_never_renders_the_token() -> None:
    rendered = repr(BearerCredential(token=BEARER_SECRET, generation=3))

    assert BEARER_SECRET not in rendered
    assert rendered == "BearerCredential(token=<redacted>, generation=3)"


def test_the_profile_less_reader_reports_no_master_token() -> None:
    """Direct clients with no profile path get ``None``, not an I/O attempt."""
    assert _NoMasterTokenProfile().read_master_token() is None


# ---------------------------------------------------------------------------
# Loop binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_loop_reports_the_lifecycle_assigned_loop() -> None:
    provider = _provider()
    loop = asyncio.get_running_loop()

    assert provider.bound_loop is None
    provider.set_bound_loop(loop)
    assert provider.bound_loop is loop


@pytest.mark.asyncio
async def test_operations_refuse_to_run_before_the_lifecycle_binds_a_loop() -> None:
    provider = _provider()

    with pytest.raises(RuntimeError, match="not bound by the client lifecycle"):
        await provider.get(1)


@pytest.mark.asyncio
async def test_reset_after_open_cancels_a_retained_mint_task() -> None:
    """A rebind must not leave the previous loop's mint task running."""
    provider = _provider()
    provider.set_bound_loop(asyncio.get_running_loop())

    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never())
    provider._mint_task = task

    provider.reset_after_open()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert provider._mint_task is None
    assert provider._mint_waiters == 0


@pytest.mark.asyncio
async def test_a_cancelled_mint_task_result_is_consumed_without_raising() -> None:
    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    BearerProvider._consume_task_result(task)


@pytest.mark.asyncio
async def test_a_failed_mint_task_result_is_consumed_without_raising() -> None:
    """Otherwise the discarded task logs 'exception was never retrieved'."""

    async def _boom() -> None:
        raise OAuthMintError("mint failed")

    task = asyncio.create_task(_boom())
    await asyncio.gather(task, return_exceptions=True)

    BearerProvider._consume_task_result(task)


# ---------------------------------------------------------------------------
# activate(): profile read failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_rejects_a_profile_without_a_master_token() -> None:
    provider = _provider(profile=_Profile(record=None))
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()

    with pytest.raises(ConfigurationError) as caught:
        await provider.activate(1)

    assert MASTER_SECRET not in str(caught.value)


@pytest.mark.asyncio
async def test_a_failing_profile_read_is_treated_as_no_token() -> None:
    """An unreadable profile must not leak its OSError into the auth path."""
    provider = _provider(profile=_Profile(error=OSError("/profile unreadable")))
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()

    with pytest.raises(ConfigurationError) as caught:
        await provider.activate(1)

    assert "unreadable" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_interpreter_exits_during_the_profile_read_propagate(
    error: BaseException,
) -> None:
    """These are not 'no token available' — they must not be reclassified."""
    provider = _provider(profile=_Profile(error=error))
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()

    with pytest.raises(type(error)):
        await provider.activate(1)


@pytest.mark.asyncio
async def test_a_non_master_token_record_is_rejected() -> None:
    """``type(record) is not MasterToken`` — a subclass is not admitted either."""

    class _Subclass(MasterToken):
        pass

    provider = _provider(
        profile=_Profile(
            record=_Subclass(email="p@example.com", android_id="1", secret=MASTER_SECRET)
        )
    )
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()

    with pytest.raises(ConfigurationError):
        await provider.activate(1)


# ---------------------------------------------------------------------------
# _mint_once(): failure classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        pytest.param(OAuthMintError("upstream said no"), AuthError, id="mint-error"),
        pytest.param(MissingDependencyError("gpsoauth"), MissingDependencyError, id="missing-dep"),
        pytest.param(ValueError("unexpected internal state"), AuthError, id="unexpected-error"),
    ],
)
async def test_mint_failures_are_classified_without_echoing_the_cause(
    raised: BaseException, expected: type[BaseException]
) -> None:
    provider = _provider(minter=_Minter(raised))
    await _activate(provider)

    with pytest.raises(expected) as caught:
        await provider.get(1)

    assert "upstream said no" not in str(caught.value)
    assert "unexpected internal state" not in str(caught.value)


def _mint_frame_locals(error: BaseException) -> dict:
    """Return ``_mint_once``'s frame locals from an escaping exception."""
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "_mint_once":
            return tb.tb_frame.f_locals
        tb = tb.tb_next
    raise AssertionError("_mint_once frame not found on the traceback")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_interpreter_exits_during_mint_drop_the_master_token(
    error: BaseException,
) -> None:
    """The exit propagates, and the master token is not left on the traceback.

    Driven through ``_mint_once`` directly rather than ``get``: a
    ``KeyboardInterrupt`` escaping an ``asyncio.Task`` reaches the event loop
    and tears down the whole pytest session instead of being caught by
    ``pytest.raises``.

    The assertion inspects ``_mint_once``'s surviving frame locals, because
    that is what the handler's ``record = None`` exists to clear: an escaping
    exception keeps its frames alive, so the master token would otherwise stay
    reachable through the traceback.
    """
    provider = _provider(minter=_Minter(error))
    await _activate(provider)

    with pytest.raises(type(error)) as caught:
        await provider._mint_once(provider._provider_epoch)

    assert _mint_frame_locals(caught.value).get("record") is None
    assert MASTER_SECRET not in repr(_mint_frame_locals(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "minted",
    [
        pytest.param(None, id="nothing-returned"),
        pytest.param(MintedOAuthToken(token="", expires_at=None), id="empty-token"),
        pytest.param(object(), id="wrong-type"),
    ],
)
async def test_an_unusable_mint_result_is_an_auth_error(minted: object) -> None:
    provider = _provider(minter=_Minter(minted))
    await _activate(provider)

    with pytest.raises(AuthError):
        await provider.get(1)


@pytest.mark.asyncio
async def test_minting_after_deactivation_reports_the_inactive_generation() -> None:
    provider = _provider()
    await _activate(provider)
    provider._master_token = None

    with pytest.raises(RuntimeError, match="not active for this client generation"):
        await provider.get(1)


# ---------------------------------------------------------------------------
# Expiry deadline arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        pytest.param(None, None, id="no-expiry"),
        pytest.param("1200", None, id="non-integer-expiry"),
        pytest.param(1_000, None, id="already-expired"),
        pytest.param(900, None, id="expired-in-the-past"),
        # 600s remaining, margin = min(60, 10%) = 60 -> 500 + 600 - 60
        pytest.param(1_600, 1_040.0, id="margin-capped-at-60s"),
        # 100s remaining, margin = min(60, 10) = 10 -> 500 + 100 - 10
        pytest.param(1_100, 590.0, id="margin-is-ten-percent"),
    ],
)
async def test_expiry_deadline_leaves_a_refresh_margin(
    expires_at: object, expected: float | None
) -> None:
    provider = _provider()

    assert provider._expiry_deadline(expires_at) == expected


@pytest.mark.asyncio
async def test_a_token_expiring_entirely_within_the_margin_is_not_cached() -> None:
    """Caching it would hand callers a bearer that dies mid-request."""
    provider = _provider(
        minter=_Minter(MintedOAuthToken(token=BEARER_SECRET, expires_at=1_000)),
    )
    await _activate(provider)

    first = await provider.get(1)
    second = await provider.get(1)

    assert provider._cached is None
    assert second.generation == first.generation + 1


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_ignores_a_stale_generation() -> None:
    """A 401 from an already-replaced bearer must not clear the fresh one."""
    provider = _provider(
        minter=_Minter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000)),
    )
    await _activate(provider)
    credential = await provider.get(1)

    provider.invalidate(credential.generation - 1)
    assert provider._cached is credential

    provider.invalidate(credential.generation)
    assert provider._cached is None


@pytest.mark.asyncio
async def test_consuming_a_still_pending_task_result_does_not_raise() -> None:
    """``reset_after_open`` attaches this callback before the task settles."""

    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never())
    try:
        BearerProvider._consume_task_result(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_the_profile_read_propagates_rather_than_clearing_the_token() -> None:
    """A cancelled ``activate`` is not 'this profile has no token'."""
    provider = _provider(profile=_Profile(error=asyncio.CancelledError()))
    provider.set_bound_loop(asyncio.get_running_loop())
    provider.reset_after_open()

    with pytest.raises(asyncio.CancelledError):
        await provider.activate(1)


@pytest.mark.asyncio
async def test_a_cancelled_mint_propagates_and_retains_nothing() -> None:
    provider = _provider(minter=_Minter(asyncio.CancelledError()))
    await _activate(provider)

    with pytest.raises(asyncio.CancelledError) as caught:
        await provider._mint_once(provider._provider_epoch)

    assert _mint_frame_locals(caught.value).get("record") is None


@pytest.mark.asyncio
async def test_minting_for_a_superseded_provider_epoch_is_refused() -> None:
    provider = _provider()
    await _activate(provider)

    with pytest.raises(RuntimeError, match="not active for this client generation"):
        await provider._mint_once(provider._provider_epoch + 1)


@pytest.mark.asyncio
async def test_a_bearer_minted_across_a_close_is_never_published() -> None:
    """The mint itself refuses once the provider generation has moved on."""
    minter = _BlockingMinter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000))
    provider = _provider(minter=minter)
    await _activate(provider)

    pending = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    provider._provider_epoch += 1
    minter.release.set()

    with pytest.raises(RuntimeError, match="not active for this client generation"):
        await pending
    assert provider._cached is None


@pytest.mark.asyncio
async def test_a_successful_mint_is_dropped_if_the_session_ended_while_waiting() -> None:
    """Covers the waiter's post-lock re-check, distinct from the mint's own.

    The mint completes cleanly under an unchanged provider generation; the
    session is deactivated while the waiter is blocked re-acquiring the lock,
    so the credential must be discarded rather than published or cached.
    """
    minter = _BlockingMinter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000))
    provider = _provider(minter=minter)
    await _activate(provider)

    pending = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    # Hold the lock so the waiter parks on ``lock.acquire()`` after its mint.
    lock = provider._get_lock()
    await lock.acquire()
    minter.release.set()
    await asyncio.sleep(0)
    provider._active_session_epoch = 999
    lock.release()

    with pytest.raises(RuntimeError, match="not active for this client generation"):
        await pending
    assert provider._cached is None


@pytest.mark.asyncio
async def test_cancelling_a_waiter_blocked_on_the_lock_releases_its_slot() -> None:
    """Otherwise the waiter count never drops and the task is retained forever."""
    minter = _BlockingMinter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000))
    provider = _provider(minter=minter)
    await _activate(provider)

    pending = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    lock = provider._get_lock()
    await lock.acquire()
    mint_task = provider._mint_task
    assert mint_task is not None
    minter.release.set()
    # Let the mint settle, then let the waiter resume and park on ``acquire``.
    await asyncio.wait_for(asyncio.shield(mint_task), timeout=5)
    for _ in range(3):
        await asyncio.sleep(0)
    assert not pending.done(), "waiter should be blocked re-acquiring the lock"

    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    lock.release()

    assert provider._mint_waiters == 0


@pytest.mark.asyncio
async def test_a_mint_finishing_after_its_last_waiter_left_is_not_retained() -> None:
    """``_mint_done`` clears the task when the wave has already drained."""
    minter = _BlockingMinter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000))
    provider = _provider(minter=minter)
    await _activate(provider)

    pending = asyncio.create_task(provider.get(1))
    await minter.started.wait()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert provider._mint_waiters == 0

    minter.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert provider._mint_task is None


@pytest.mark.asyncio
async def test_a_settled_mint_task_is_released_when_no_waiter_remains() -> None:
    provider = _provider(
        minter=_Minter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000)),
    )
    await _activate(provider)

    await provider.get(1)

    assert provider._mint_task is None
    assert provider._mint_waiters == 0


@pytest.mark.asyncio
async def test_prepare_close_fences_credentials_and_drains_the_mint_task() -> None:
    minter = _BlockingMinter(MintedOAuthToken(token=BEARER_SECRET, expires_at=2_000))
    provider = _provider(minter=minter)
    await _activate(provider)
    pending = asyncio.create_task(provider.get(1))
    await minter.started.wait()

    await provider.prepare_close()

    assert provider._mint_task is None
    assert provider._mint_waiters == 0
    assert provider._master_token is None
    assert provider._cached is None
    assert provider._active_session_epoch is None
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_prepare_close_requires_a_bound_loop() -> None:
    provider = _provider()

    with pytest.raises(RuntimeError, match="not bound by the client lifecycle"):
        await provider.prepare_close()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_a_provider_without_a_storage_path_reads_no_profile() -> None:
    provider = _make_bearer_provider(None)

    assert isinstance(provider._profile_store, _NoMasterTokenProfile)
    assert provider._profile_store.read_master_token() is None


def test_a_provider_with_a_storage_path_uses_the_profile_store(tmp_path) -> None:
    provider = _make_bearer_provider(tmp_path)

    assert isinstance(provider._profile_store, ProfileStore)
