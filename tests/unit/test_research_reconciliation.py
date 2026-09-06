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

from typing import Any

import pytest

from notebooklm import research as research_pub
from notebooklm._research import BaseResearchAPI, _only_source
from notebooklm._research_import import _ResearchImportBatch
from notebooklm._types.research import ResearchStatus, ResearchTask
from notebooklm.exceptions import (
    AmbiguousResearchTaskError,
    NetworkError,
    ResearchTaskMismatchError,
    RPCError,
)
from notebooklm.types import Source
from tests._fixtures.fake_core import declared_noop_operation_scope

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

    _operation_scope = staticmethod(declared_noop_operation_scope)

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
# Single-send failure reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmarked_rpc_failure_is_conservative_unknown_and_preserves_identity() -> None:
    failure = RPCError("unclassified failure")
    api, lister = _make_api(
        list_outcomes=[[], [_src("possible", "https://a.example.com")]],
        import_outcomes=[failure],
    )

    with pytest.raises(RPCError) as raised:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert raised.value is failure
    assert _unconfirmed(failure) is True
    assert failure.reconciliation_candidates == ("possible",)  # type: ignore[attr-defined]
    assert len(api.import_calls) == 1
    assert len(lister.calls) == 2


@pytest.mark.asyncio
async def test_import_verification_redacts_long_userinfo_before_reconciliation_cap() -> None:
    url = (
        "https://userinfo-must-not-leak-"
        + "x" * 220
        + ":password-must-not-leak@unknown.test/path?access_token=query-must-not-leak"
    )
    failure = NetworkError("response lost")
    api, _lister = _make_api(
        list_outcomes=[[], []],
        import_outcomes=[failure],
    )

    with pytest.raises(NetworkError) as raised:
        await api.import_sources_with_verification(
            "nb",
            "task",
            [{"url": url, "title": "Unresolved"}],
            max_elapsed=0,
        )

    assert raised.value is failure
    assert failure.operation_metadata is not None
    assert failure.operation_metadata.reconciliation is not None
    assert failure.operation_metadata.reconciliation.unresolved_inputs == (
        "https://***@unknown.test/path?access_token=***",
    )
    rendered = repr(failure.operation_metadata)
    assert "userinfo-must-not-leak" not in rendered
    assert "password-must-not-leak" not in rendered
    assert "query-must-not-leak" not in rendered


@pytest.mark.asyncio
async def test_visible_rows_after_loss_are_candidates_not_success() -> None:
    failure = NetworkError("response lost")
    api, _lister = _make_api(
        list_outcomes=[
            [],
            [
                _src("possible-a", "https://a.example.com"),
                _src("possible-b", "https://a.example.com/"),
            ],
        ],
        import_outcomes=[failure],
    )

    with pytest.raises(NetworkError) as raised:
        await api.import_sources_with_verification(
            "nb", "task", [{"url": "https://a.example.com", "title": "A"}]
        )

    assert raised.value is failure
    assert failure.reconciliation_candidates == (  # type: ignore[attr-defined]
        "possible-a",
        "possible-b",
    )
    assert len(api.import_calls) == 1


@pytest.mark.asyncio
async def test_success_is_not_enriched_from_uncorrelated_rows() -> None:
    api, lister = _make_api(
        list_outcomes=[
            [_src("old", "https://old.example.com")],
            [_src("foreign", "https://b.example.com")],
        ],
        import_outcomes=[[{"id": "returned", "title": "A"}]],
    )

    result = await api.import_sources_with_verification(
        "nb",
        "task",
        [
            {"url": "https://a.example.com", "title": "A"},
            {"url": "https://b.example.com", "title": "B"},
        ],
    )

    assert result == [{"id": "returned", "title": "A"}]
    assert len(api.import_calls) == 1
    assert lister.calls == [("nb", False)]
