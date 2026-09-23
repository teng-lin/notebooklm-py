"""Regression tests for #2432 through the real Web client and mock HTTP replies."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import (
    ArtifactInProgressTimeoutError,
    ArtifactPendingTimeoutError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)
from notebooklm.rpc import RPCMethod
from tests._helpers.client_factory import build_client_shell_for_tests

NOTEBOOK = "investigation-notebook"
TARGET = "requested-audio"
SIBLING = "other-session-audio"


def _audio(artifact_id: str, status: int = 3) -> list:
    return [
        artifact_id,
        "Audio Overview",
        1,
        None,
        status,
        None,
        [None, None, None, None, None, [[f"https://example.com/{artifact_id}.mp4"]]],
    ]


def _body(method: RPCMethod, payload: object, status: int | None = None) -> str:
    frame = [
        "wrb.fr",
        method.value,
        json.dumps(payload) if payload is not None else None,
        None,
        None,
        [status] if status is not None else None,
    ]
    chunk = json.dumps([frame])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _listed(*rows: list) -> str:
    return _body(RPCMethod.LIST_ARTIFACTS, [list(rows)])


@dataclass
class _Wire:
    now: float = 0.0
    replies: list[str] = field(default_factory=list)
    poll_times: list[float] = field(default_factory=list)

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    def respond(self, request: httpx.Request) -> httpx.Response:
        method = request.url.params.get("rpcids")
        if method == RPCMethod.CREATE_ARTIFACT.value:
            body = _body(RPCMethod.CREATE_ARTIFACT, [_audio(TARGET, status=1)])
        else:
            assert method == RPCMethod.LIST_ARTIFACTS.value
            self.poll_times.append(self.now)
            assert self.replies, "Unexpected additional list request"
            body = self.replies.pop(0)
        return httpx.Response(200, text=body)


@pytest.fixture
async def harness(auth_tokens: AuthTokens) -> AsyncIterator[tuple[NotebookLMClient, _Wire]]:
    wire = _Wire()

    def factory(**kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(wire.respond))

    async with build_client_shell_for_tests(auth_tokens, async_client_factory=factory) as client:
        client.artifacts._polling._sleep = wire.sleep
        client.artifacts._polling._monotonic = lambda: wire.now
        yield client, wire


@pytest.mark.parametrize(
    "missing_reply",
    [
        pytest.param(_listed(), id="empty-list"),
        pytest.param(_listed(_audio(SIBLING)), id="only-completed-sibling"),
        pytest.param(_body(RPCMethod.LIST_ARTIFACTS, None), id="null-payload"),
        pytest.param(_body(RPCMethod.LIST_ARTIFACTS, None, 0), id="null-ok"),
    ],
)
@pytest.mark.parametrize("previously_seen", [False, True])
async def test_wait_observes_same_id_after_prolonged_absence(
    harness, missing_reply: str, previously_seen: bool, recwarn
) -> None:
    client, wire = harness
    started = await client.artifacts.generate_audio(NOTEBOOK, source_ids=["source"])
    wire.replies = (
        ([_listed(_audio(TARGET, status=2))] if previously_seen else [])
        + [missing_reply] * 10
        + [_listed(_audio(TARGET), _audio(SIBLING))]
    )
    seen = []

    result = await client.artifacts.wait_for_completion(
        NOTEBOOK, started.task_id, on_status_change=seen.append
    )

    assert result.is_complete
    assert result.task_id == TARGET
    assert result.url == f"https://example.com/{TARGET}.mp4"
    assert not result.is_rate_limited
    assert all(not status.is_removed for status in seen)
    assert wire.now > 24.0
    assert not wire.replies
    assert not recwarn


@pytest.mark.parametrize("previously_seen", [False, True])
async def test_permanent_absence_times_out_without_accepting_a_completed_sibling(
    harness, previously_seen: bool
) -> None:
    client, wire = harness
    wire.replies = ([_listed(_audio(TARGET, status=2))] if previously_seen else []) + [
        _listed(_audio(SIBLING))
    ] * 20
    error_type = ArtifactInProgressTimeoutError if previously_seen else ArtifactPendingTimeoutError
    seen = []

    with pytest.raises(error_type) as caught:
        await client.artifacts.wait_for_completion(
            NOTEBOOK, TARGET, timeout=35.0, on_status_change=seen.append
        )

    assert wire.now == 35.0
    assert caught.value.task_id == TARGET
    assert caught.value.last_status == "not_found"
    assert caught.value.status_history == (
        ("in_progress", "not_found") if previously_seen else ("not_found",)
    )
    assert all(not status.is_removed and not status.is_rate_limited for status in seen)


@pytest.mark.parametrize(
    "code,error_type",
    [(4, RPCTimeoutError), (8, RateLimitError), (13, ServerError), (14, ServerError)],
)
async def test_transient_listing_rejections_retry_without_emitting_absence(
    harness, code: int, error_type: type[RPCError | RPCTimeoutError]
) -> None:
    client, wire = harness
    wire.replies = [_body(RPCMethod.LIST_ARTIFACTS, None, code)] * 2 + [_listed(_audio(TARGET))]
    seen = []

    result = await client.artifacts.wait_for_completion(
        NOTEBOOK, TARGET, on_status_change=seen.append
    )

    assert result.is_complete
    assert [status.status for status in seen] == ["completed"]
    assert wire.poll_times == [0.0, 2.0, 6.0]

    # A single read exposes the typed error and original wire evidence.
    wire.replies = [_body(RPCMethod.LIST_ARTIFACTS, None, code)]
    with pytest.raises(error_type) as caught:
        await client.artifacts.poll_status(NOTEBOOK, TARGET)
    assert caught.value.method_id == RPCMethod.LIST_ARTIFACTS.value
    assert isinstance(caught.value.__cause__, RPCError)
    assert caught.value.__cause__.rpc_code == code
    assert caught.value.__cause__.raw_response
    if isinstance(caught.value, RPCTimeoutError):
        assert caught.value.original_error is caught.value.__cause__
    else:
        assert caught.value.rpc_code == code
        assert caught.value.raw_response == caught.value.__cause__.raw_response


@pytest.mark.parametrize(
    "code,error_type",
    [(4, RPCTimeoutError), (8, RateLimitError), (13, ServerError), (14, ServerError)],
)
async def test_persistent_listing_rejections_exhaust_the_retry_budget(
    harness, code: int, error_type: type[RPCError | RPCTimeoutError]
) -> None:
    client, wire = harness
    wire.replies = [_body(RPCMethod.LIST_ARTIFACTS, None, code)] * 4
    seen = []

    with pytest.raises(error_type) as caught:
        await client.artifacts.wait_for_completion(NOTEBOOK, TARGET, on_status_change=seen.append)

    assert isinstance(caught.value.__cause__, RPCError)
    assert caught.value.__cause__.rpc_code == code
    assert wire.poll_times == [0.0, 2.0, 6.0, 14.0]
    assert seen == []


@pytest.mark.parametrize("code", [3, 5, 7])
async def test_permanent_listing_rejection_propagates_without_retry(harness, code: int) -> None:
    client, wire = harness
    wire.replies = [_body(RPCMethod.LIST_ARTIFACTS, None, code)]

    with pytest.raises(RPCError) as caught:
        await client.artifacts.wait_for_completion(NOTEBOOK, TARGET)

    assert caught.value.rpc_code == code
    assert wire.poll_times == [0.0]


@pytest.mark.parametrize("code,error_type", [(4, RPCTimeoutError), (14, ServerError)])
async def test_read_error_backoff_is_clamped_by_the_wait_deadline(
    harness, code: int, error_type: type[RPCError | RPCTimeoutError]
) -> None:
    client, wire = harness
    wire.replies = [_body(RPCMethod.LIST_ARTIFACTS, None, code)] * 2

    with pytest.raises(ArtifactPendingTimeoutError) as caught:
        await client.artifacts.wait_for_completion(NOTEBOOK, TARGET, timeout=3.0)

    assert isinstance(caught.value.__cause__, error_type)
    assert caught.value.status_history == ()
    assert wire.now == 3.0
    assert wire.poll_times == [0.0, 2.0]


async def test_distinct_concurrent_waiters_keep_their_ids(harness) -> None:
    client, wire = harness
    wire.replies = [_listed(_audio(SIBLING), _audio(TARGET))] * 2

    target, sibling = await asyncio.gather(
        client.artifacts.wait_for_completion(NOTEBOOK, TARGET),
        client.artifacts.wait_for_completion(NOTEBOOK, SIBLING),
    )

    assert target.is_complete and sibling.is_complete
    assert target.task_id == TARGET
    assert sibling.task_id == SIBLING
    assert target.url != sibling.url
    assert len(wire.poll_times) == 2


async def test_missing_id_diagnostics_preserve_siblings_without_content(harness, caplog) -> None:
    client, wire = harness
    wire.replies = [_listed(_audio(SIBLING))]

    with caplog.at_level(logging.DEBUG, logger="notebooklm._artifact.listing"):
        result = await client.artifacts.poll_status(NOTEBOOK, TARGET)

    assert result.is_not_found
    messages = [record.message for record in caplog.records if "listed_ids=" in record.message]
    assert len(messages) == 1
    assert TARGET in messages[0] and SIBLING in messages[0] and NOTEBOOK in messages[0]
    assert "Audio Overview" not in messages[0]
    assert "https://" not in messages[0]


async def test_legacy_thresholds_warn_at_caller_and_do_not_end_the_wait(harness) -> None:
    client, wire = harness
    wire.replies = [_listed()] * 10 + [_listed(_audio(TARGET))]

    with pytest.warns(
        DeprecationWarning, match="max_not_found.*deprecated and ignored"
    ) as warnings:
        result = await client.artifacts.wait_for_completion(
            NOTEBOOK, TARGET, max_not_found=1, min_not_found_window=0.0
        )

    assert result.is_complete
    assert len(warnings) == 1
    assert warnings[0].filename == str(Path(__file__))


async def test_legacy_threshold_warning_honors_quiet_switch(harness, monkeypatch, recwarn) -> None:
    client, wire = harness
    wire.replies = [_listed(_audio(TARGET))]
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")

    assert (
        await client.artifacts.wait_for_completion(NOTEBOOK, TARGET, max_not_found=1)
    ).is_complete
    assert not recwarn
