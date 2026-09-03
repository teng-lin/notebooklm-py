"""Reconciliation and retry policy of ``BaseResearchAPI``.

``IMPORT_RESEARCH`` is a ``NON_IDEMPOTENT_NO_RETRY`` mutation: a client-side
failure leaves three genuinely different outcomes on the table, and the whole
point of this ladder is to keep them apart.

* **confirmed** — the read-back proves exactly which rows landed;
* **refused** — the server answered ``FAILED_PRECONDITION``; the call was
  rejected, so it is raised bare;
* **unconfirmed** — nobody can say whether the write committed. These carry
  ``mark_unconfirmed``'s marker (read back with
  ``getattr(exc, "unconfirmed", False)``) precisely so a caller cannot mistake
  them for "safe to retry" — a retry there duplicates sources.

Every test below asserts the marker explicitly, because an unmarked error and
a marked one look identical to ``pytest.raises`` and only the marker tells a
caller whether retrying is safe.

The backend-neutral base class is driven through a minimal concrete subclass
rather than through the Web adapter: ``WebResearchAPI`` carries its own
duplicate implementation of ``_import_sources_with_verification``, so exercising
it would test the wrong code.
"""

from __future__ import annotations

from itertools import chain, repeat
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import notebooklm._research as _research_mod
from notebooklm import research as research_pub
from notebooklm._research import BaseResearchAPI, _only_source
from notebooklm._research_import import _ResearchImportBatch
from notebooklm._types.research import ResearchStatus, ResearchTask
from notebooklm.exceptions import (
    AmbiguousResearchTaskError,
    NetworkError,
    ResearchTaskMismatchError,
    RPCError,
    ServerError,
)
from notebooklm.types import Source

FAILED_PRECONDITION = 9


def _rpc_refusal(message: str = "task already imported") -> RPCError:
    """One ``FAILED_PRECONDITION`` — the server *rejecting* the import (#2187)."""
    return RPCError(message, rpc_code=FAILED_PRECONDITION)


def _src(source_id: str, url: str | None, title: str = "") -> Source:
    return Source(id=source_id, title=title or source_id, url=url)


def _unconfirmed(exc: BaseException) -> bool:
    """Read the marker exactly as production callers do."""
    return bool(getattr(exc, "unconfirmed", False))


class _Lister:
    """Replays queued ``sources.list`` outcomes; the last one repeats."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, bool]] = []

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        self.calls.append((notebook_id, strict))
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return list(outcome)


class _StubResearchAPI(BaseResearchAPI):
    """The smallest concrete backend-neutral Research namespace.

    Only ``import_sources`` has behaviour; the other abstract members exist so
    the class is instantiable and blow up loudly if the orchestration ever
    reaches them.
    """

    def __init__(self, lister: _Lister, outcomes: list[Any]) -> None:
        super().__init__(source_lister=lister)  # type: ignore[arg-type]
        self._outcomes = list(outcomes)
        self.import_calls: list[tuple[list[Any], float | None]] = []

    async def start(self, notebook_id, query, source="web", mode="fast"):  # noqa: ANN001
        raise AssertionError("start() must not be reached")

    async def discover(self, notebook_id, query, *, mode="default"):  # noqa: ANN001
        raise AssertionError("discover() must not be reached")

    async def poll(self, notebook_id, task_id=None):  # noqa: ANN001
        raise AssertionError("poll() must not be reached")

    async def cancel(self, notebook_id, run_id):  # noqa: ANN001
        raise AssertionError("cancel() must not be reached")

    async def _send_import(
        self,
        notebook_id: str,
        batch: _ResearchImportBatch,
        *,
        _remaining_budget: float | None,
    ) -> list[dict[str, str]]:
        del notebook_id
        self.import_calls.append(([item.source_input for item in batch.items], _remaining_budget))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _make_api(
    *, list_outcomes: list[Any], import_outcomes: list[Any]
) -> tuple[_StubResearchAPI, _Lister]:
    lister = _Lister(list_outcomes)
    return _StubResearchAPI(lister, import_outcomes), lister


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_the_sole_source_helper_refuses_to_pick_a_winner_from_an_ambiguous_match() -> None:
    """Attribution is all-or-nothing: 0 or 2+ candidates must both mean "unknown".

    Positionally grabbing ``candidates[0]`` would silently attribute a landed
    row to the wrong requested URL, which is exactly the duplicate-import bug
    the reconciliation exists to prevent.
    """
    only = _src("s1", "https://a.example.com")

    assert _only_source([]) is None
    assert _only_source([only]) is only
    assert _only_source([only, _src("s2", "https://a.example.com")]) is None


def test_the_sole_source_helper_falls_back_to_none_for_a_sequence_that_yields_nothing() -> None:
    """A length of one that iterates empty must not leak an implicit ``None`` path.

    The helper avoids positional indexing on purpose, so it iterates instead;
    the explicit trailing ``return None`` is what keeps a sequence whose
    ``__len__`` disagrees with its iterator from falling off the end untyped.
    """

    class _LyingSequence(list):
        def __len__(self) -> int:
            return 1

    assert _only_source(_LyingSequence()) is None


def test_the_base_url_normalizer_delegates_to_the_public_citation_normalizer() -> None:
    """Citation matching and import de-duplication use *different* normalizers.

    The public one keeps a fragment; the import-time one strips it. Pinning the
    delegation here catches a "simplification" that points the base class at the
    import normalizer and silently changes citation matching.
    """
    url = "https://Example.COM/a/?q=1#section"

    assert BaseResearchAPI._normalize_url(url) == research_pub.normalize_url(url)
    assert BaseResearchAPI._normalize_url(url).endswith("#section")


@pytest.mark.parametrize(
    ("available_ids", "task_id", "raise_on_ambiguous", "expected_ids"),
    [
        pytest.param(["t1", "t2"], "t1", False, ["t1"], id="explicit-pin-filters-to-that-run"),
        pytest.param(["t1", "t2"], "nope", False, [], id="explicit-pin-with-no-match-selects-none"),
        pytest.param(["t1", "t2"], None, False, ["t1", "t2"], id="tolerant-mode-returns-every-run"),
        pytest.param(["t1"], None, True, ["t1"], id="strict-mode-accepts-a-single-run"),
        pytest.param([], None, True, [], id="strict-mode-accepts-no-runs-at-all"),
    ],
)
def test_polled_task_selection_only_refuses_on_genuine_unpinned_ambiguity(
    available_ids: list[str],
    task_id: str | None,
    raise_on_ambiguous: bool,
    expected_ids: list[str],
) -> None:
    """Only an unpinned poll in strict mode over several runs may refuse to guess.

    Raising whenever strict mode is on would break a single-run poll, and
    filtering when a pin is supplied must ignore ``raise_on_ambiguous``
    entirely — the pin already resolved the ambiguity.
    """
    tasks = [
        ResearchTask(task_id=task, status=ResearchStatus.IN_PROGRESS) for task in available_ids
    ]

    selected = BaseResearchAPI._select_polled_tasks(
        tasks, notebook_id="nb", task_id=task_id, raise_on_ambiguous=raise_on_ambiguous
    )

    assert [task.task_id for task in selected] == expected_ids


def test_an_unpinned_strict_poll_over_several_runs_refuses_to_guess() -> None:
    """The complement of the case above: two in-flight runs and no pin is an error."""
    tasks = [
        ResearchTask(task_id="t1", status=ResearchStatus.IN_PROGRESS),
        ResearchTask(task_id="t2", status=ResearchStatus.IN_PROGRESS),
    ]

    with pytest.raises(AmbiguousResearchTaskError) as exc_info:
        BaseResearchAPI._select_polled_tasks(
            tasks, notebook_id="nb", task_id=None, raise_on_ambiguous=True
        )

    assert exc_info.value.task_ids == ["t1", "t2"]


# ---------------------------------------------------------------------------
# Up-front validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_import_empty_and_unusable_batches_do_not_reach_send_hook() -> None:
    api, _lister = _make_api(list_outcomes=[[]], import_outcomes=[])

    assert await api.import_sources("nb", "opaque-task", []) == []
    assert await api.import_sources("nb", "opaque-task", [{"title": "No URL or report body"}]) == []
    assert api.import_calls == []


@pytest.mark.asyncio
async def test_raw_import_propagates_send_error_by_identity() -> None:
    error = RPCError("rejected", method_id="fake.Finish", rpc_code=9)
    api, _lister = _make_api(list_outcomes=[[]], import_outcomes=[error])

    with pytest.raises(RPCError) as raised:
        await api.import_sources(
            "nb",
            "opaque-task",
            [{"url": "https://example.com", "title": "Example"}],
        )

    assert raised.value is error
    assert raised.value.method_id == "fake.Finish"
    assert raised.value.rpc_code == 9


@pytest.mark.asyncio
async def test_an_empty_request_short_circuits_before_any_network_call() -> None:
    """Nothing to import means no baseline read and no mutation, not an empty POST."""
    api, lister = _make_api(list_outcomes=[[]], import_outcomes=[])

    imported = await api.import_sources_with_verification("nb", "task", [])

    assert imported == []
    assert imported.already_present == []  # type: ignore[attr-defined]
    assert lister.calls == []
    assert api.import_calls == []


@pytest.mark.asyncio
async def test_a_source_from_a_different_research_task_is_rejected_before_the_baseline_read() -> (
    None
):
    """Cross-task provenance is a caller bug, caught before any I/O.

    Importing a row discovered under another run would attach it to the wrong
    task; validating up front also means the mutation is never dispatched.
    """
    api, lister = _make_api(list_outcomes=[[]], import_outcomes=[])

    with pytest.raises(ResearchTaskMismatchError) as exc_info:
        await api.import_sources_with_verification(
            "nb",
            "task_a",
            [{"url": "https://a.example.com", "title": "A", "research_task_id": "task_b"}],
        )

    assert exc_info.value.task_id == "task_a"
    assert exc_info.value.source_research_task_id == "task_b"
    assert lister.calls == []
    assert api.import_calls == []


@pytest.mark.asyncio
async def test_a_baseline_row_without_a_url_is_skipped_by_the_duplicate_prefilter() -> None:
    """Pasted-text and report rows have no URL and must not reach the normalizer.

    A single URL-less row in a real notebook is enough to break the whole
    idempotency pre-filter if the guard is dropped, so the dedupe still has to
    work around one — matching the trailing-slash duplicate and letting the
    genuinely new URL through.
    """
    api, lister = _make_api(
        list_outcomes=[
            [
                _src("s_text", None, title="Pasted notes"),
                _src("s_dup", "https://dup.example.com/"),
            ]
        ],
        import_outcomes=[[{"id": "s_new", "title": "New"}]],
    )

    imported = await api.import_sources_with_verification(
        "nb",
        "task",
        [
            {"url": "https://dup.example.com", "title": "Dup"},
            {"url": "https://new.example.com", "title": "New"},
        ],
    )

    assert imported == [{"id": "s_new", "title": "New"}]
    assert imported.already_present == [  # type: ignore[attr-defined]
        {"id": "s_dup", "title": "s_dup", "url": "https://dup.example.com/"}
    ]
    # Only the genuinely-new URL reached the mutation.
    sent, _budget = api.import_calls[0]
    assert [entry["url"] for entry in sent] == ["https://new.example.com"]


# ---------------------------------------------------------------------------
# Failure classification: refused vs unconfirmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_precondition_rpc_error_is_raised_as_is_without_probing() -> None:
    """An ordinary RPC error is neither retryable nor unconfirmed.

    It never entered the timeout/precondition ladder, so spending a probe on it
    — or tagging it unconfirmed — would misreport a plain failure.
    """
    api, lister = _make_api(
        list_outcomes=[[]],
        import_outcomes=[RPCError("bad request", rpc_code=3)],
    )

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert _unconfirmed(exc_info.value) is False
    assert len(api.import_calls) == 1
    # One baseline read and no probe.
    assert len(lister.calls) == 1


@pytest.mark.asyncio
async def test_a_server_error_is_reconciled_like_a_timeout_rather_than_raised() -> None:
    """A 5xx may still have committed, so it takes the read-back path.

    Here the probe proves the row landed, so the call resolves as *confirmed
    success* — no retry, and the caller learns the id the server assigned.
    """
    api, lister = _make_api(
        list_outcomes=[
            [],  # baseline: empty notebook
            [_src("s_landed", "https://a.example.com", title="A")],  # probe: it landed
        ],
        import_outcomes=[ServerError("500 from Finish")],
    )

    imported = await api.import_sources_with_verification(
        "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
    )

    assert imported == [{"id": "s_landed", "title": "A"}]
    # Exactly one mutation was ever sent — the whole point of reconciling.
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_a_refused_report_import_is_raised_bare_rather_than_marked_unconfirmed() -> None:
    """A report row has no URL, so nothing can ever be reconciled for it.

    The two outcomes must still stay apart: ``FAILED_PRECONDITION`` is the
    server *answering* "no", while a transport failure leaves the commit
    genuinely unknown. Marking the refusal unconfirmed would tell callers to
    stop a batch that was cleanly rejected.
    """
    api, _lister = _make_api(list_outcomes=[[]], import_outcomes=[_rpc_refusal()])

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb",
            "task",
            [{"url": "", "title": "Report", "result_type": 5, "report_markdown": "# r"}],
        )

    assert _unconfirmed(exc_info.value) is False
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_a_dropped_report_import_is_marked_unconfirmed() -> None:
    """The contrast case for the test above: no answer means unconfirmed.

    The server may hold the report either way and there is no URL to probe, so
    this must never look retryable.
    """
    api, _lister = _make_api(list_outcomes=[[]], import_outcomes=[NetworkError("connection reset")])

    with pytest.raises(NetworkError) as exc_info:
        await api.import_sources_with_verification(
            "nb",
            "task",
            [{"url": "", "title": "Report", "result_type": 5, "report_markdown": "# r"}],
        )

    assert _unconfirmed(exc_info.value) is True
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_a_refusal_whose_probe_also_fails_stays_a_refusal_on_the_first_attempt() -> None:
    """A first-attempt refusal is attributable even when the probe cannot answer.

    Nothing was sent before it, so there is no earlier in-flight mutation whose
    fate is unknown: the refusal itself is the whole story, and the probe
    failure is attached as its cause rather than upgrading it to unconfirmed.
    """
    probe_failure = NetworkError("probe blew up")
    api, lister = _make_api(
        list_outcomes=[[], probe_failure],
        import_outcomes=[_rpc_refusal()],
    )

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert _unconfirmed(exc_info.value) is False
    assert exc_info.value.__cause__ is probe_failure
    assert len(lister.calls) == 2


@pytest.mark.asyncio
async def test_a_refusal_after_a_concurrent_addition_hides_the_landed_set_but_stays_a_refusal() -> (
    None
):
    """An unattributable read-back cannot upgrade a first-attempt refusal.

    Another session added a row, so the reconciler refuses to attribute
    anything; the ``FAILED_PRECONDITION`` still surfaces bare, carrying the
    unconfirmed reconciliation error as its cause so the ambiguity is visible.
    """
    refusal = _rpc_refusal()
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [_src("s_other", "https://someone-else.example.com")],  # unrelated new row
        ],
        import_outcomes=[refusal],
    )

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert exc_info.value is refusal
    assert _unconfirmed(exc_info.value) is False
    cause = exc_info.value.__cause__
    assert isinstance(cause, RPCError)
    assert "concurrent source additions" in str(cause)
    assert _unconfirmed(cause) is True


@pytest.mark.asyncio
async def test_a_partially_landed_refusal_is_raised_rather_than_retried_for_the_remainder() -> None:
    """A refusal is terminal even when the read-back proves a partial landing.

    The remaining URL is genuinely missing, but re-sending ``IMPORT_RESEARCH``
    against a task the server has already refused is exactly the blind retry
    #2187 forbids.
    """
    refusal = _rpc_refusal()
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [_src("s_a", "https://a.example.com", title="A")],  # only A landed
        ],
        import_outcomes=[refusal],
    )

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb",
            "task",
            [
                {"url": "https://a.example.com", "title": "A"},
                {"url": "https://b.example.com", "title": "B"},
            ],
        )

    assert exc_info.value is refusal
    assert _unconfirmed(exc_info.value) is False
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_a_refusal_after_an_earlier_dropped_attempt_reports_the_dropped_one_as_unconfirmed() -> (
    None
):
    """Once a mutation has been dropped in flight, the refusal is not the story.

    The earlier ``NetworkError`` may have committed rows the reconciler still
    cannot see, so the raised error is *that* one, marked unconfirmed — the
    later refusal and the unattributable read-back become its causes.
    """
    dropped = NetworkError("connection reset")
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [],  # probe after the dropped attempt: nothing landed yet
            [_src("s_other", "https://someone-else.example.com")],  # concurrent row appears
        ],
        import_outcomes=[dropped, _rpc_refusal()],
    )

    with (
        patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock),
        pytest.raises(NetworkError) as exc_info,
    ):
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}], initial_delay=0
        )

    assert exc_info.value is dropped
    assert _unconfirmed(exc_info.value) is True
    cause = exc_info.value.__cause__
    assert isinstance(cause, RPCError)
    assert "concurrent source additions" in str(cause)
    assert len(api.import_calls) == 2


@pytest.mark.asyncio
async def test_a_read_back_that_cannot_be_attributed_uniquely_is_unconfirmed() -> None:
    """Two new rows sharing one requested URL make attribution impossible.

    Both rows are "expected" URLs, so the concurrent-addition guard passes — but
    one request cannot have produced two rows, so retrying could triple them.
    """
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [
                _src("s_a1", "https://a.example.com"),
                _src("s_a2", "https://a.example.com/"),  # same URL once normalized
            ],
        ],
        import_outcomes=[NetworkError("connection reset")],
    )

    with pytest.raises(RPCError) as exc_info:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert "not uniquely attributable" in str(exc_info.value)
    assert _unconfirmed(exc_info.value) is True
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_two_requests_for_the_same_url_reconcile_to_a_single_landed_row() -> None:
    """A caller that asks for one URL twice must not be told it landed twice.

    Both requested entries normalize to the same URL and match the one row the
    server created; emitting it once keeps the returned ids a faithful record of
    what exists.
    """
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [_src("s_a", "https://a.example.com", title="A")],
        ],
        import_outcomes=[NetworkError("connection reset")],
    )

    imported = await api.import_sources_with_verification(
        "nb",
        "task",
        # The duplicate-prefilter compares each request against the *baseline*,
        # never against its siblings, so both of these survive into the import
        # and reconciliation is what has to collapse them.
        [
            {"url": "https://a.example.com/", "title": "A"},
            {"url": "https://a.example.com", "title": "A again"},
        ],
    )

    assert imported == [{"id": "s_a", "title": "A"}]
    sent, _budget = api.import_calls[0]
    assert len(sent) == 2
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_a_retry_whose_remaining_budget_cannot_hold_an_attempt_is_unconfirmed() -> None:
    """Running out of budget mid-backoff must not silently drop the earlier failure.

    The first attempt may have committed; a window too short to observe its own
    result is worse than stopping, so the loop re-raises the original error with
    the unconfirmed marker instead of sending a second mutation.
    """
    dropped = NetworkError("connection reset")
    api, _lister = _make_api(
        list_outcomes=[[], []],
        import_outcomes=[dropped, [{"id": "never", "title": "sent"}]],
    )

    # Clock reads, in order: loop start, attempt-1 budget, post-failure
    # remaining (20s left → viable, so it sleeps), then the top of iteration 2
    # (5s left → below the viable-attempt floor).
    with (
        patch.object(
            _research_mod.time,
            "monotonic",
            side_effect=chain([0.0, 0.0, 80.0], repeat(95.0)),
        ),
        patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(NetworkError) as exc_info,
    ):
        await api.import_sources_with_verification(
            "nb",
            "task",
            [{"url": "https://a.example.com", "title": "A"}],
            max_elapsed=100,
            initial_delay=60,
            max_delay=60,
        )

    assert exc_info.value is dropped
    assert _unconfirmed(exc_info.value) is True
    # ``from None`` — an exhausted budget is not itself a cause worth chaining.
    assert exc_info.value.__cause__ is None
    # The backoff ran, but the second mutation was never dispatched.
    mock_sleep.assert_awaited_once_with(20.0)
    assert len(api.import_calls) == 1


# ---------------------------------------------------------------------------
# Success-path enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_success_that_under_reports_ids_is_enriched_from_the_read_back() -> None:
    """A short id list is completed from new rows, never from baseline rows.

    ``Finish`` occasionally returns fewer ids than URLs requested. The missing
    id exists — the caller just cannot address the source without it — so it is
    recovered from rows that were not in the baseline.
    """
    api, lister = _make_api(
        list_outcomes=[
            [_src("s_old", "https://old.example.com")],  # baseline
            [
                _src("s_old", "https://old.example.com"),
                _src("s_a", "https://a.example.com", title="A"),
                _src("s_b", "https://b.example.com", title="B"),
            ],
        ],
        import_outcomes=[[{"id": "s_a", "title": "A"}]],  # only one id came back
    )

    imported = await api.import_sources_with_verification(
        "nb",
        "task",
        [
            {"url": "https://a.example.com", "title": "A"},
            {"url": "https://b.example.com", "title": "B"},
        ],
    )

    assert imported == [{"id": "s_a", "title": "A"}, {"id": "s_b", "title": "B"}]
    # The pre-existing baseline row is never mistaken for a fresh import.
    assert "s_old" not in {entry["id"] for entry in imported}
    assert len(lister.calls) == 2


@pytest.mark.asyncio
async def test_enrichment_declines_to_guess_when_two_new_rows_share_the_requested_url() -> None:
    """Enrichment is best-effort, but it must not fabricate an attribution.

    Two new rows for one requested URL cannot be told apart, so the result keeps
    only the ids the server actually returned rather than picking one at random.
    """
    api, _lister = _make_api(
        list_outcomes=[
            [],  # baseline
            [
                _src("s_a", "https://a.example.com", title="A"),
                _src("s_b1", "https://b.example.com", title="B"),
                _src("s_b2", "https://b.example.com/", title="B duplicate"),
            ],
        ],
        import_outcomes=[[{"id": "s_a", "title": "A"}]],
    )

    imported = await api.import_sources_with_verification(
        "nb",
        "task",
        [
            {"url": "https://a.example.com", "title": "A"},
            {"url": "https://b.example.com", "title": "B"},
        ],
    )

    assert imported == [{"id": "s_a", "title": "A"}]


@pytest.mark.asyncio
async def test_a_failed_enrichment_read_never_turns_a_committed_import_into_a_failure() -> None:
    """``Finish`` already succeeded, so the enrichment read is decoration only.

    Propagating its failure would invite the caller to replay a mutation the
    server has already committed.
    """
    api, _lister = _make_api(
        list_outcomes=[[], NetworkError("probe blew up")],
        import_outcomes=[[{"id": "s_a", "title": "A"}]],
    )

    imported = await api.import_sources_with_verification(
        "nb",
        "task",
        [
            {"url": "https://a.example.com", "title": "A"},
            {"url": "https://b.example.com", "title": "B"},
        ],
    )

    assert imported == [{"id": "s_a", "title": "A"}]
