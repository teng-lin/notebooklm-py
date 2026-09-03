"""Guard-branch coverage for the Android Research adapter.

``tests/unit/android/test_research.py`` covers the wire shapes and the
lifecycle. These cases cover the guards that run *before* or *instead of* an
RPC: argument validation, the canonical-id rule, the deep-start response that
succeeded without its diagnostic id, and the two ways ``import_sources``
declines to send anything at all.

Every case asserts on the transport as well as the outcome -- for a guard, the
RPC that never happened is the contract.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    research_pb2,
)
from notebooklm._android.research import (
    CANCEL_JOB_METHOD,
    FINISH_RUN_METHOD,
    LIST_JOBS_METHOD,
    START_DEEP_METHOD,
    START_FAST_METHOD,
    AndroidResearchAPI,
)
from notebooklm._types.research import RESEARCH_RESULT_TYPE_REPORT
from notebooklm.exceptions import DecodingError, RateLimitError, ValidationError
from notebooklm.types import Source

RUN_ID = "11111111-1111-4111-8111-111111111111"


@dataclass(frozen=True)
class _Lease:
    epoch: int


class _Transport:
    """Records every scope and RPC so a guard can be shown to have short-circuited."""

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.responses = {method: deque(values) for method, values in (responses or {}).items()}
        self.calls: list[tuple[str, Any]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **_: Any) -> AsyncIterator[_Lease]:
        self.scopes.append(label)
        yield _Lease(17)

    async def unary(self, method: str, request: Any, **_kwargs: Any) -> Any:
        self.calls.append((method, request))
        value = self.responses[method].popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class _Lister:
    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        raise AssertionError("no research guard should reach the source lister")


def _api(responses: dict[str, list[Any]] | None = None) -> tuple[AndroidResearchAPI, _Transport]:
    transport = _Transport(responses)
    return AndroidResearchAPI(transport, _Lister()), transport  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# start argument validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "mode", "query", "message"),
    [
        pytest.param("ftp", "fast", "q", "Invalid source 'ftp'", id="unknown-source"),
        pytest.param("web", "turbo", "q", "Invalid mode 'turbo'", id="unknown-mode"),
        pytest.param(
            "drive",
            "deep",
            "q",
            "Deep Research only supports Web sources",
            id="deep-drive-unsupported",
        ),
        pytest.param("web", "fast", "", "query must not be empty", id="empty-query"),
        pytest.param("web", "fast", "   \n", "query must not be empty", id="whitespace-query"),
    ],
)
async def test_start_rejects_bad_arguments_before_opening_a_scope(
    source: str, mode: str, query: str, message: str
) -> None:
    """A rejected start must cost nothing: no lease, no RPC, no server-side run.

    ``start`` is a mutation, so validating after the call would leave a real
    discovery job behind for an argument the client already knew was wrong.
    """
    api, transport = _api()

    with pytest.raises(ValidationError, match=message):
        await api.start("nb", query, source, mode)

    assert transport.scopes == []
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "mode"),
    [
        pytest.param("WEB", "FAST", id="fast"),
        pytest.param("Web", "Deep", id="deep"),
    ],
)
async def test_start_accepts_case_insensitive_arguments(source: str, mode: str) -> None:
    """The validator lowercases before comparing, so casing is not a rejection."""
    api, transport = _api(
        {
            START_FAST_METHOD: [
                research_pb2.DiscoverSourcesManifoldResponse(source_discovery_job_id=RUN_ID)
            ],
            START_DEEP_METHOD: [
                research_pb2.DiscoverSourcesAsyncResponse(
                    source_discovery_job_id=RUN_ID, start_session_id="diagnostic"
                )
            ],
        }
    )

    started = await api.start("nb", "q", source, mode)

    assert started.mode == mode.lower()
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_deep_start_without_its_diagnostic_session_id_is_unconfirmed() -> None:
    """The deep run id is the session id, so a missing one loses the started run.

    The job was created server-side -- the response carried a canonical job id
    -- but without the session id the caller cannot poll it, so the failure has
    to say the write may have committed rather than read as a clean rejection.
    """
    api, transport = _api(
        {
            START_DEEP_METHOD: [
                research_pb2.DiscoverSourcesAsyncResponse(source_discovery_job_id=RUN_ID)
            ]
        }
    )

    with pytest.raises(DecodingError, match="omitted its diagnostic session id") as raised:
        await api.start("nb", "q", "web", "deep")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# canonical run-id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("AAAAAAAA-1111-4111-8111-111111111111", id="uppercase-hex"),
        pytest.param(f"{{{RUN_ID}}}", id="braced"),
        pytest.param(RUN_ID.replace("-", ""), id="unhyphenated"),
        pytest.param("urn:uuid:" + RUN_ID, id="urn-prefixed"),
    ],
)
async def test_a_parseable_but_non_canonical_run_id_is_rejected(run_id: str) -> None:
    """``uuid.UUID`` accepts these spellings; the backend keys on the exact string.

    Normalizing them silently would send a cancel for an id the caller never
    named, so the codec requires the canonical form it would echo back.
    """
    api, transport = _api()

    with pytest.raises(ValidationError, match="run_id must be a canonical UUID"):
        await api.cancel("nb", run_id)

    with pytest.raises(ValidationError, match="run_id must be a canonical UUID"):
        await api.import_sources("nb", run_id, [{"url": "https://example.com/a", "title": "A"}])

    assert transport.scopes == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_import_validates_run_id_before_materializing_source_rows() -> None:
    """Android's canonical-id guard wins even when a later row cannot be coerced."""
    api, transport = _api()

    with pytest.raises(ValidationError, match="run_id must be a canonical UUID"):
        await api.import_sources(
            "nb",
            "not-a-run-id",
            [object()],  # type: ignore[list-item]
        )

    assert transport.scopes == []
    assert transport.calls == []


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_rate_limited_propagates_without_a_read_back_probe() -> None:
    """A throttled cancel never reached the server, so there is nothing to probe.

    Probing would spend a second call against the same quota, and marking the
    error unconfirmed would wrongly suggest the run might already be cancelled.

    Today this holds because ``RateLimitError`` is a *sibling* of
    ``NetworkError``/``ServerError`` under ``RPCError`` rather than a subclass,
    which is exactly the assumption worth pinning: were it ever reparented, the
    probe would start running for throttled cancels and this case would fail.
    """
    error = RateLimitError("throttled")
    api, transport = _api({CANCEL_JOB_METHOD: [error]})

    with pytest.raises(RateLimitError) as raised:
        await api.cancel("nb", RUN_ID)

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is False
    assert [method for method, _request in transport.calls] == [CANCEL_JOB_METHOD]
    assert LIST_JOBS_METHOD not in {method for method, _request in transport.calls}


# ---------------------------------------------------------------------------
# import_sources: nothing to send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_of_an_empty_selection_sends_nothing() -> None:
    """Finishing a run with no content would end the run server-side for free."""
    api, transport = _api()

    assert await api.import_sources("nb", RUN_ID, []) == []
    assert transport.scopes == []
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        pytest.param({"title": "No url"}, id="no-url"),
        pytest.param(
            {"title": "Report", "result_type": RESEARCH_RESULT_TYPE_REPORT},
            id="report-without-markdown",
        ),
        pytest.param(
            {"title": "Report", "result_type": RESEARCH_RESULT_TYPE_REPORT, "url": ""},
            id="report-without-markdown-or-url",
        ),
    ],
)
async def test_import_of_only_unusable_sources_sends_nothing(
    source: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A row with neither a URL nor report text has no wire form, so none is sent.

    The selection is non-empty here, so the guard has to be the *entry* count
    rather than the input count -- sending an empty ``FinishDiscoverSourcesRun``
    would close the run and discard the results the caller still wanted.
    """
    api, transport = _api()

    with caplog.at_level(logging.DEBUG, logger="notebooklm._research"):
        assert await api.import_sources("nb", RUN_ID, [source]) == []
    assert transport.scopes == []
    assert transport.calls == []
    assert caplog.record_tuples == []


@pytest.mark.asyncio
async def test_android_import_accepts_normalized_untitled_report() -> None:
    """Unlike Web, Android historically accepts a report dict with no title key."""
    api, transport = _api({FINISH_RUN_METHOD: [research_pb2.FinishDiscoverSourcesRunResponse()]})

    await api.import_sources(
        "nb",
        RUN_ID,
        [{"result_type": RESEARCH_RESULT_TYPE_REPORT, "report_markdown": "# Report"}],
    )

    [(method, request)] = transport.calls
    assert method == FINISH_RUN_METHOD
    assert request.user_content[0].text_content.source_name == "Untitled"
    assert request.user_content[0].text_content.content == "# Report"


@pytest.mark.asyncio
async def test_unusable_sources_are_dropped_from_a_mixed_selection() -> None:
    """The skip is per row: one unusable entry must not drop its usable siblings."""
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                research_pb2.FinishDiscoverSourcesRunResponse(
                    sources=[
                        research_pb2.ImportedSourceHeader(
                            source_id=read_pb2.SourceId(id="src-1"), title="A"
                        ),
                        # A header the backend returned without an id is not a
                        # usable result and must not appear as one.
                        research_pb2.ImportedSourceHeader(title="unnamed"),
                    ]
                )
            ]
        }
    )

    imported = await api.import_sources(
        "nb",
        RUN_ID,
        [
            {"title": "No url"},
            {"url": "https://example.com/a", "title": "A"},
        ],
    )

    assert imported == [{"id": "src-1", "title": "A"}]
    [(method, request)] = transport.calls
    assert method == FINISH_RUN_METHOD
    assert [entry.web_content.url for entry in request.user_content] == ["https://example.com/a"]
