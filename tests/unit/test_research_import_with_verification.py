"""Tests for ``WebResearchAPI.import_sources_with_verification``.

The one-send import and bounded read-only inspection logic lives on
``WebResearchAPI``. These tests were originally in
``tests/unit/cli/test_helpers.py::TestImportWithRetry`` (the logic used to
live in ``cli/research_import.py``); they were moved here when the policy
became a library-layer concern so Python API users get the same fix the
CLI does.

The CLI wrapper ``cli.research_import.import_with_retry`` is now a thin
delegate — its tests cover only the wiring (still in ``test_helpers.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import notebooklm._research as research_module
from notebooklm._web.research import WebResearchAPI
from notebooklm.exceptions import (
    NetworkError,
    ResearchTaskMismatchError,
    RPCError,
    RPCTimeoutError,
)
from tests._fixtures.fake_core import make_fake_core


class _RecordingRpc:
    """A minimal injected ``RpcCaller`` that records each call's read timeout.

    Constructor injection rather than assigning an ``AsyncMock`` onto a
    duck-typed fake's RPC attribute — ADR-0007 forbids exactly that. Queued
    outcomes are replayed in order.

    Two details keep the double honest rather than merely convenient:

    * a queued :class:`RPCTimeoutError` gets its ``timeout_seconds`` from the
      window this call was actually handed, exactly as the real executor
      derives it (``_web/transport/executor.py``). Hardcoding it would hide a defect
      where the clamp reaches the wire but the raised error still reports the
      unclamped window;
    * ``advance`` moves the injected clock *while the call is in flight*, so a
      retry is late because time passed during a failed attempt — the real
      causal chain — rather than because a positional clock stub said so.
    """

    def __init__(self, outcomes: list[object], clock: dict[str, float] | None = None) -> None:
        self._outcomes = list(outcomes)
        self._clock = clock
        self.read_timeouts: list[float | None] = []

    async def rpc_call(
        self,
        method: object,
        params: object,
        source_path: str = "/",
        allow_null: bool = False,
        **kwargs: object,
    ) -> object:
        read_timeout = kwargs.get("read_timeout")
        self.read_timeouts.append(read_timeout)  # type: ignore[arg-type]
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, _Advance):
            if self._clock is not None:
                self._clock["now"] += outcome.seconds
            outcome = outcome.then
        if isinstance(outcome, RPCTimeoutError):
            # Mirror the executor: the error reports the window actually used.
            outcome = RPCTimeoutError(str(outcome), timeout_seconds=read_timeout)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Advance:
    """Queue entry: advance the injected clock, then produce ``then``."""

    def __init__(self, seconds: float, then: object) -> None:
        self.seconds = seconds
        self.then = then


def _make_research() -> tuple[WebResearchAPI, MagicMock, MagicMock]:
    """Build a ``WebResearchAPI`` with a mocked source-lister seam.

    Returns ``(research, mock_rpc, mock_source_lister)``. Override
    ``research.import_sources`` / ``mock_source_lister.list`` per test.

    WebResearchAPI mirrors ``NotebooksAPI``'s default-builder pattern, so
    injecting a mock lister bypasses the cross-API dependency entirely —
    the test does not need a SourcesAPI handle.
    """
    mock_rpc = MagicMock()
    mock_source_lister = MagicMock()
    research = WebResearchAPI(
        mock_rpc,
        supervisor=make_fake_core(),
        source_lister=mock_source_lister,
    )
    return research, mock_rpc, mock_source_lister


class TestImportSourcesWithVerification:
    @pytest.mark.asyncio
    async def test_empty_sources_returns_without_network_calls(self) -> None:
        research, _, source_lister = _make_research()
        source_lister.list = AsyncMock()
        research.import_sources = AsyncMock()

        assert await research.import_sources_with_verification("nb", "task", []) == []
        source_lister.list.assert_not_awaited()
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_loss_is_inspected_but_never_replayed(self) -> None:
        research, _, source_lister = _make_research()
        error = NetworkError("response lost")
        source_lister.list = AsyncMock(
            side_effect=[
                [],
                [MagicMock(id="possible", url="https://a.example", title="A")],
            ]
        )
        research.import_sources = AsyncMock(side_effect=error)

        with pytest.raises(NetworkError) as raised:
            await research.import_sources_with_verification(
                "nb", "task", [{"url": "https://a.example", "title": "A"}]
            )

        assert raised.value is error
        assert getattr(error, "unconfirmed", False) is True
        assert error.reconciliation_candidates == ("possible",)  # type: ignore[attr-defined]
        assert research.import_sources.await_count == 1

    @pytest.mark.asyncio
    async def test_candidate_inspection_reprobes_empty_and_failed_reads_with_capped_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 100.0}
        delays: list[float] = []

        async def advance(delay: float) -> None:
            delays.append(delay)
            clock["now"] += delay

        monkeypatch.setattr(research_module.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(research_module.asyncio, "sleep", advance)
        research, _, source_lister = _make_research()
        error = NetworkError("response lost")
        source_lister.list = AsyncMock(
            side_effect=[
                [],
                [],
                NetworkError("inspection unavailable"),
                [MagicMock(id="possible", url="https://a.example", title="A")],
            ]
        )
        research.import_sources = AsyncMock(side_effect=error)

        with pytest.raises(NetworkError) as raised:
            await research.import_sources_with_verification(
                "nb",
                "task",
                [{"url": "https://a.example", "title": "A"}],
                max_elapsed=10,
                initial_delay=1,
                backoff_factor=3,
                max_delay=2,
            )

        assert raised.value is error
        assert research.import_sources.await_count == 1
        assert source_lister.list.await_count == 4
        assert delays == [1, 2]
        assert error.reconciliation_candidates == ("possible",)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_decoded_success_returns_only_decoded_rows(self) -> None:
        research, _, source_lister = _make_research()
        concurrent = MagicMock(id="foreign", url="https://b.example", title="B")
        source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(return_value=[{"id": "returned", "title": "A"}])

        result = await research.import_sources_with_verification(
            "nb",
            "task",
            [
                {"url": "https://a.example", "title": "A"},
                {"url": "https://b.example", "title": "B"},
            ],
        )

        assert result == [{"id": "returned", "title": "A"}]
        assert concurrent.id not in {entry["id"] for entry in result}
        source_lister.list.assert_awaited_once_with("nb", strict=False)

    @pytest.mark.asyncio
    async def test_positive_rejection_evidence_is_not_reclassified_or_inspected(self) -> None:
        from notebooklm._idempotency import mark_commit_state
        from notebooklm.outcomes import CommitState

        research, _, source_lister = _make_research()
        refusal = mark_commit_state(RPCError("request refused", rpc_code=9), CommitState.REJECTED)
        source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(side_effect=refusal)

        with pytest.raises(RPCError) as raised:
            await research.import_sources_with_verification(
                "nb", "task", [{"url": "https://a.example", "title": "A"}]
            )

        assert raised.value is refusal
        assert getattr(refusal, "unconfirmed", False) is False
        source_lister.list.assert_awaited_once_with("nb", strict=False)
        research.import_sources.assert_awaited_once()


class TestImportSourcesIdempotency:
    """#1961: pre-filter already-present URLs up front on every attempt.

    The timeout-retry path already drops already-present URLs; these tests
    cover the generalization to the happy path (no timeout), plus the
    ``already_present`` side channel and the ``allow_duplicate`` opt-out.
    """

    @pytest.mark.asyncio
    async def test_repeat_import_all_present_imports_nothing(self):
        existing = [
            MagicMock(id="src_a", title="A", url="https://a.example.com"),
            MagicMock(id="src_b", title="B", url="https://b.example.com"),
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                {"url": "https://b.example.com", "title": "B"},
            ],
        )

        assert list(imported) == []
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"},
            {"id": "src_b", "title": "B", "url": "https://b.example.com"},
        ]
        # Everything already present → no import RPC at all.
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_present_imports_only_absent(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "src_b", "title": "B"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                {"url": "https://b.example.com", "title": "B"},
            ],
        )

        assert list(imported) == [{"id": "src_b", "title": "B"}]
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        # Only the genuinely-absent source B was handed to import_sources.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://b.example.com", "title": "B"}
        ]

    @pytest.mark.asyncio
    async def test_allow_duplicate_reimports_all(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "src_a2", "title": "A"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}],
            allow_duplicate=True,
        )

        assert list(imported) == [{"id": "src_a2", "title": "A"}]
        assert imported.already_present == []
        # allow_duplicate → no pre-filter, the present URL is re-sent.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://a.example.com", "title": "A"}
        ]

    @pytest.mark.asyncio
    async def test_report_entry_preserved_when_url_already_present(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        report_entry = {"title": "Report", "report_markdown": "# R", "result_type": 5}
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "rep_1", "title": "Report"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}, report_entry],
        )

        assert list(imported) == [{"id": "rep_1", "title": "Report"}]
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        # Report entry has no dedupable URL → kept; the present URL is dropped.
        assert research.import_sources.await_args.args[2] == [report_entry]

    @pytest.mark.asyncio
    async def test_snapshot_failure_imports_all_without_filter(self):
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(side_effect=NetworkError("snapshot down"))
        research.import_sources = AsyncMock(return_value=[{"id": "src_a", "title": "A"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}],
        )

        assert list(imported) == [{"id": "src_a", "title": "A"}]
        # No baseline → can't tell what's present → import everything (fallback).
        assert imported.already_present == []
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://a.example.com", "title": "A"}
        ]

    @pytest.mark.asyncio
    async def test_provenance_validated_before_filter_when_all_present(self):
        """A wrong ``research_task_id`` raises even when every requested URL is
        already present — provenance is validated before the idempotency
        pre-filter can drop the entries (coderabbit review on #1961)."""
        from notebooklm.exceptions import ResearchTaskMismatchError

        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(
            side_effect=AssertionError("import_sources must not be called")
        )

        with pytest.raises(ResearchTaskMismatchError):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    {
                        "url": "https://a.example.com",
                        "title": "A",
                        "research_task_id": "wrong-task",
                    }
                ],
            )
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_present_reported_once_for_repeated_url(self):
        """A request repeating the same (normalized) already-present URL reports
        that existing source once, not once per duplicate input (coderabbit)."""
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(
            side_effect=AssertionError("import_sources must not be called")
        )

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                # Same normalized URL (trailing slash stripped) — a duplicate input.
                {"url": "https://a.example.com/", "title": "A again"},
            ],
        )

        assert list(imported) == []
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_source_list_returns_without_touching_the_backend(self) -> None:
        research, mock_rpc, mock_source_lister = _make_research()

        result = await research._import_sources_with_verification("nb1", "task-1", [])

        assert result == []
        mock_rpc.rpc_call.assert_not_called()
        mock_source_lister.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_source_from_another_research_task_is_refused(self) -> None:
        research, mock_rpc, _lister = _make_research()
        sources = [{"url": "https://a.example/x", "research_task_id": "task-OTHER"}]

        with pytest.raises(ResearchTaskMismatchError) as caught:
            await research._import_sources_with_verification("nb1", "task-1", sources)

        assert caught.value.task_id == "task-1"
        assert caught.value.source_research_task_id == "task-OTHER"
        mock_rpc.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_batch_spanning_two_research_tasks_is_refused(self) -> None:
        """Each source matches the caller's id individually is not enough."""
        research, mock_rpc, _lister = _make_research()
        sources = [
            {"url": "https://a.example/x", "research_task_id": "task-1"},
            {"url": "https://b.example/y", "research_task_id": "task-2"},
        ]

        with pytest.raises(ResearchTaskMismatchError):
            await research._import_sources_with_verification("nb1", "task-1", sources)

        mock_rpc.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_sources_without_provenance_are_admitted(self) -> None:
        """An unstamped source inherits the caller's task id rather than failing."""
        research, _rpc, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(return_value=[])

        await research._import_sources_with_verification(
            "nb1", "task-1", [{"url": "https://a.example/x"}]
        )

        research.import_sources.assert_awaited()
        # ``assert_awaited`` alone would pass if a different task id were sent.
        assert research.import_sources.await_args.args[1] == "task-1"


class TestResearchPublicHelperDelegation:
    """The API surfaces the module-level helpers under stable names."""

    def test_normalize_url_matches_the_public_helper(self) -> None:
        from notebooklm import research as research_pub

        raw = "HTTPS://Example.COM/a/../b?utm_source=x"

        assert WebResearchAPI._normalize_url(raw) == research_pub.normalize_url(raw)

    def test_extract_report_urls_matches_the_public_helper(self) -> None:
        from notebooklm import research as research_pub

        report = "See https://a.example/one and https://b.example/two for detail."

        assert WebResearchAPI.extract_report_urls(report) == research_pub.extract_report_urls(
            report
        )
        assert WebResearchAPI.extract_report_urls(report) == {
            "https://a.example/one",
            "https://b.example/two",
        }
