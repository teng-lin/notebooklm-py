"""Leader/follower and not-found paths of ``_artifact.polling``.

These cover the branches the existing polling suites do not reach: the
follower's ``on_status_change`` fan-out, the leader done-callback's
cancellation and invariant guards, and unresolved-absence timeouts
including reset-on-reappearance diagnostics.

The service is driven directly (rather than through ``WebArtifactsAPI``)
with an injected clock and sleep, so every assertion is about the polling
loop's own decisions rather than about wire decoding.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from notebooklm._artifact.polling import (
    ArtifactPollingService,
    _get_artifact_type_name,
    _is_media_ready,
)
from notebooklm._polling_registry import PollRegistry
from notebooklm._types.enums import ArtifactStatus, ArtifactTypeCode
from notebooklm.exceptions import ArtifactPendingTimeoutError, RateLimitError
from notebooklm.rpc import NetworkError
from notebooklm.types import Artifact, GenerationState, GenerationStatus


class _FakeSupervisor:
    """Minimal ``CallSupervisor`` stand-in for the polling service.

    ``bound_loop = None`` is the documented silent no-op for the loop-affinity
    helper, so the stub stays correct without binding to a real loop.
    """

    bound_loop = None

    def __init__(self) -> None:
        self.poll_registry = PollRegistry()
        self.drain_hooks: dict[str, object] = {}
        self.spawn_labels: list[str] = []

    def assert_bound_loop(self) -> None:
        return None

    def register_drain_hook(self, name: str, hook: object) -> None:
        self.drain_hooks[name] = hook

    async def spawn_child(self, label, factory, *, inherit_operation=True):
        assert inherit_operation is False
        self.spawn_labels.append(label)
        return asyncio.create_task(factory(), name=label)

    def operation_scope(self, log_label: str):
        class _Scope:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        return _Scope()


class _Clock:
    """An injectable monotonic clock, advanced by the injected sleep.

    Tests that need an *attempt* (rather than a backoff) to burn budget move
    ``now`` directly.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_service(
    clock: _Clock | None = None,
) -> tuple[ArtifactPollingService, _FakeSupervisor, _Clock]:
    supervisor = _FakeSupervisor()
    resolved_clock = _Clock() if clock is None else clock
    service = ArtifactPollingService(
        supervisor=supervisor,
        poll_registry=supervisor.poll_registry,
        sleep=resolved_clock.sleep,
        monotonic=resolved_clock.monotonic,
    )
    return service, supervisor, resolved_clock


def _not_found() -> GenerationStatus:
    return GenerationStatus(task_id="task1", status=GenerationState.NOT_FOUND)


def _pending() -> GenerationStatus:
    return GenerationStatus(task_id="task1", status=GenerationState.PENDING)


async def _await_attached_poll_task(
    supervisor: _FakeSupervisor, key: tuple[str, str]
) -> asyncio.Task[GenerationStatus]:
    """Spin until the leader's registry entry carries its spawned poll task."""
    for _ in range(100):
        entry = supervisor.poll_registry.get(key)
        if entry is not None and entry[1] is not None:
            return entry[1]
        await asyncio.sleep(0)
    raise AssertionError(f"poll task never attached for {key!r}")


# ---------------------------------------------------------------------------
# Service construction / wiring
# ---------------------------------------------------------------------------


def test_the_service_exposes_the_injected_registry_and_registers_its_drain_hook() -> None:
    """Followers attach through this registry, and close() drains through the hook.

    Both are wiring the owner (``ArtifactsAPI``) depends on: handing back a
    *different* registry would silently split leaders from followers, and a
    missing drain hook would leak poll tasks past client close.
    """
    supervisor = _FakeSupervisor()
    registry = PollRegistry()

    service = ArtifactPollingService(supervisor=supervisor, poll_registry=registry)

    assert service.poll_registry is registry
    assert supervisor.drain_hooks["artifacts.polls"] == service.drain


def test_the_service_builds_its_own_registry_when_none_is_injected() -> None:
    """The default is a real registry, not ``None`` — followers must still attach."""
    service = ArtifactPollingService(supervisor=_FakeSupervisor())

    assert isinstance(service.poll_registry, PollRegistry)


# ---------------------------------------------------------------------------
# Leader / follower fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:ArtifactsAPI\\.wait_for_completion follower:DeprecationWarning")
async def test_a_follower_receives_the_shared_result_through_its_own_status_callback() -> None:
    """A late waiter attaches to the leader's poll and still gets its callback.

    The follower never runs the poll loop, so the *only* way it can observe a
    status change is the explicit fan-out on the follower path. Without it a
    second waiter would silently receive the result with no callback at all.
    """
    service, supervisor, _clock = _make_service()
    release = asyncio.Event()
    done = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return done

    leader_seen: list[GenerationStatus] = []
    follower_seen: list[GenerationStatus] = []

    leader = asyncio.create_task(
        service.wait_for_completion(
            "nb1", "task1", poll_status=poll_status, on_status_change=leader_seen.append
        )
    )
    while supervisor.poll_registry.get(("nb1", "task1")) is None:
        await asyncio.sleep(0)

    follower = asyncio.create_task(
        service.wait_for_completion(
            "nb1", "task1", poll_status=poll_status, on_status_change=follower_seen.append
        )
    )
    await asyncio.sleep(0)
    release.set()

    assert await leader is done
    assert await follower is done
    # The leader emits from inside the poll loop's transition tracking; the
    # follower emits from the follower path. Both must fire exactly once.
    assert leader_seen == [done]
    assert follower_seen == [done]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:ArtifactsAPI\\.wait_for_completion follower:DeprecationWarning")
async def test_an_async_status_callback_is_awaited_on_the_follower_path() -> None:
    """``maybe_await_callback`` must await a coroutine callback, not drop it."""
    service, supervisor, _clock = _make_service()
    release = asyncio.Event()
    done = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    awaited: list[GenerationStatus] = []

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return done

    async def on_status_change(status: GenerationStatus) -> None:
        await asyncio.sleep(0)
        awaited.append(status)

    leader = asyncio.create_task(
        service.wait_for_completion("nb1", "task1", poll_status=poll_status)
    )
    while supervisor.poll_registry.get(("nb1", "task1")) is None:
        await asyncio.sleep(0)
    follower = asyncio.create_task(
        service.wait_for_completion(
            "nb1", "task1", poll_status=poll_status, on_status_change=on_status_change
        )
    )
    await asyncio.sleep(0)
    release.set()

    await leader
    assert await follower is done
    assert awaited == [done]


@pytest.mark.asyncio
async def test_cancelling_the_poll_task_cancels_every_waiter_rather_than_hanging() -> None:
    """A cancelled leader task must resolve the shared future as cancelled.

    The done-callback is the only place that can unblock waiters once the poll
    task dies. If it skipped the cancelled case, both the leader and any
    follower would await a future nobody ever resolves.
    """
    service, supervisor, _clock = _make_service()
    key = ("nb1", "task1")
    release = asyncio.Event()

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return _pending()

    leader = asyncio.create_task(
        service.wait_for_completion("nb1", "task1", poll_status=poll_status)
    )
    while supervisor.poll_registry.get(key) is None:
        await asyncio.sleep(0)
    poll_task = await _await_attached_poll_task(supervisor, key)

    follower = asyncio.create_task(
        service.wait_for_completion("nb1", "task1", poll_status=poll_status)
    )
    await asyncio.sleep(0)

    poll_task.cancel()

    # ``wait_for`` bounds the assertion: the regression this guards against is
    # a wait that never returns, and an unbounded ``await`` would hang the
    # suite instead of reporting it.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(leader, timeout=5.0)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(follower, timeout=5.0)
    # The key is released so a later waiter starts a fresh poll instead of
    # attaching to the dead one.
    assert supervisor.poll_registry.get(key) is None


@pytest.mark.asyncio
async def test_a_prematurely_resolved_shared_future_is_reported_as_a_bug_not_swallowed() -> None:
    """The done-callback's invariant guard must be loud.

    Nothing but a defect can resolve the shared future before the poll task
    finishes. Silently returning there would hand waiters a result the poll
    loop never produced; the guard converts that into a visible ``RuntimeError``
    on the loop's exception handler.
    """
    service, supervisor, _clock = _make_service()
    key = ("nb1", "task1")
    release = asyncio.Event()
    polled = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    smuggled = GenerationStatus(task_id="task1", status=GenerationState.FAILED)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return polled

    handled: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    # The handler is process-wide loop state. Save the previous one and restore
    # it in a ``finally``: unconditionally clearing to ``None`` at the end both
    # discards a handler the runner installed and leaks this one to later tests
    # whenever an assertion below fails first.
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: handled.append(ctx))
    try:
        leader = asyncio.create_task(
            service.wait_for_completion("nb1", "task1", poll_status=poll_status)
        )
        entry = None
        while entry is None:
            await asyncio.sleep(0)
            entry = supervisor.poll_registry.get(key)
        shared_future = entry[0]
        poll_task = await _await_attached_poll_task(supervisor, key)

        # Resolve the shared future out from under the still-running poll task.
        shared_future.set_result(smuggled)
        assert await leader is smuggled

        release.set()
        assert await poll_task is polled
        await asyncio.sleep(0)

        assert [type(ctx.get("exception")) for ctx in handled] == [RuntimeError]
        assert "BUG: future resolved before poll task done-callback" in str(
            handled[0].get("exception")
        )
    finally:
        loop.set_exception_handler(previous_handler)


# ---------------------------------------------------------------------------
# Transient-error deadline guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_error_that_already_burned_the_budget_never_sleeps_first() -> None:
    """An expired deadline turns a transient failure straight into a timeout.

    Sleeping before checking would push the call past the caller's ``timeout``
    for no benefit: there is no budget left to retry into.
    """
    clock = _Clock()
    service, _supervisor, _clock_out = _make_service(clock)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        clock.now += 30.0  # the attempt itself consumed the whole budget
        raise NetworkError("transient net")

    with pytest.raises(ArtifactPendingTimeoutError) as exc_info:
        await service.wait_for_completion("nb1", "task1", timeout=10.0, poll_status=poll_status)

    assert isinstance(exc_info.value.__cause__, NetworkError)
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# Listing absence stays unresolved until the caller's deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_absence_reaches_the_deadline_without_removal() -> None:
    service, _supervisor, clock = _make_service()
    seen: list[GenerationStatus] = []
    poll_status = AsyncMock(side_effect=lambda *_: _not_found())

    with pytest.raises(ArtifactPendingTimeoutError) as caught:
        await service.wait_for_completion(
            "nb1", "task1", timeout=40.0, poll_status=poll_status, on_status_change=seen.append
        )

    assert clock.now == 40.0
    assert poll_status.await_count > 5
    assert caught.value.last_status == GenerationState.NOT_FOUND
    assert [status.status for status in seen] == [GenerationState.NOT_FOUND]


@pytest.mark.asyncio
async def test_a_fast_poll_cadence_cannot_fabricate_removal() -> None:
    service, _supervisor, clock = _make_service()
    done = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    poll_status = AsyncMock(side_effect=[_not_found() for _ in range(10)] + [done])

    result = await service.wait_for_completion(
        "nb1", "task1", initial_interval=0.1, max_interval=0.1, poll_status=poll_status
    )

    assert result is done
    assert clock.now == pytest.approx(1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after,timeout,expected_sleep", [(7, 20.0, 7.0), (30, 5.0, 5.0)])
async def test_poll_rate_limit_retry_after_respects_the_deadline(
    retry_after: int, timeout: float, expected_sleep: float
) -> None:
    service, _supervisor, clock = _make_service()
    done = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    error = RateLimitError("Read throttled", retry_after=retry_after)
    poll_status = AsyncMock(side_effect=[error, done])

    if retry_after < timeout:
        result = await service.wait_for_completion(
            "nb1", "task1", timeout=timeout, poll_status=poll_status
        )
        assert result is done
    else:
        with pytest.raises(ArtifactPendingTimeoutError) as caught:
            await service.wait_for_completion(
                "nb1", "task1", timeout=timeout, poll_status=poll_status
            )
        assert caught.value.__cause__ is error
        assert poll_status.await_count == 1
    assert clock.sleeps == [expected_sleep]


@pytest.mark.asyncio
async def test_an_artifact_that_keeps_reappearing_never_accumulates_toward_removal() -> None:
    """Repeated visibility changes must not abandon the original generation."""
    clock = _Clock()
    service, _supervisor, _ = _make_service(clock)
    completed = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    poll_status = AsyncMock(
        side_effect=[
            _not_found(),
            _not_found(),
            _pending(),  # back in the listing — resets the run
            _not_found(),
            _not_found(),
            _pending(),  # resets again
            _not_found(),
            _not_found(),
            completed,
        ]
    )

    result = await service.wait_for_completion(
        "nb1",
        "task1",
        timeout=600.0,
        initial_interval=1.0,
        max_interval=1.0,
        poll_status=poll_status,
        on_status_change=None,
    )

    assert result is completed
    assert poll_status.await_count == 9


@pytest.mark.asyncio
async def test_absence_diagnostics_reset_when_the_artifact_reappears(caplog) -> None:
    """Each diagnostic reports only the current uninterrupted absence."""
    caplog.set_level(logging.DEBUG, logger="notebooklm._artifact.polling")
    clock = _Clock()
    service, _supervisor, _ = _make_service(clock)
    completed = GenerationStatus(task_id="task1", status=GenerationState.COMPLETED)
    poll_status = AsyncMock(
        side_effect=[
            _not_found(),  # t=0   run #1 starts here
            _pending(),  # t=10  run #1 ends
            _not_found(),  # t=20  run #2 starts here
            _not_found(),  # t=30  current absence is 10 seconds old
            completed,  # t=40
        ]
    )

    result = await service.wait_for_completion(
        "nb1",
        "task1",
        timeout=600.0,
        initial_interval=10.0,
        max_interval=10.0,
        poll_status=poll_status,
    )

    assert "absent for 2 polls (10.0s)" in caplog.text
    assert "absent for 3 polls" not in caplog.text
    assert result is completed
    assert poll_status.await_count == 5


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact_type", "expected"),
    [
        pytest.param(ArtifactTypeCode.AUDIO.value, "AUDIO", id="known-code-uses-enum-name"),
        pytest.param(-1, "-1", id="unknown-code-degrades-to-its-number"),
    ],
)
def test_the_artifact_type_name_helper_never_raises_on_an_unmapped_code(
    artifact_type: int, expected: str
) -> None:
    """This name only ever feeds a log line and a metadata dict.

    Letting an unmapped code raise ``ValueError`` would abort a poll over
    nothing but a label, so an unknown code degrades to its own number.
    """
    assert _get_artifact_type_name(artifact_type) == expected


@pytest.mark.parametrize(
    ("artifact_type", "url", "expected"),
    [
        pytest.param(ArtifactTypeCode.AUDIO.value, None, False, id="audio-without-url"),
        pytest.param(ArtifactTypeCode.AUDIO.value, "https://a", True, id="audio-with-url"),
        pytest.param(ArtifactTypeCode.VIDEO.value, None, False, id="video-without-url"),
        pytest.param(ArtifactTypeCode.INFOGRAPHIC.value, None, False, id="infographic-without-url"),
        pytest.param(ArtifactTypeCode.SLIDE_DECK.value, None, False, id="slide-deck-without-url"),
        pytest.param(ArtifactTypeCode.REPORT.value, None, True, id="report-needs-no-media-url"),
        pytest.param(ArtifactTypeCode.QUIZ.value, None, True, id="quiz-needs-no-media-url"),
        pytest.param(-1, None, True, id="unknown-code-is-not-held-back"),
    ],
)
def test_media_readiness_holds_back_only_url_bearing_artifact_kinds(
    artifact_type: int, url: str | None, expected: bool
) -> None:
    """Only the four media kinds can be COMPLETED-but-not-yet-downloadable.

    A text artifact has no media URL to wait for, so gating it on ``url``
    would wait forever; an unrecognised kind is treated the same way rather
    than being blocked on a URL nobody promised.
    """
    artifact = Artifact(
        id="a1",
        title="t",
        _artifact_type=artifact_type,
        status=ArtifactStatus.COMPLETED,
        url=url,
    )

    assert _is_media_ready(artifact, artifact_type) is expected
