"""``ResearchAPI.poll`` and ``import_sources`` task-id discriminator.

Regression test for the cross-wire bug: when two research tasks are in
flight against the same notebook (e.g. an end-user kicks off a deep-research
task A and a follow-up task B before A completes), the legacy
``ResearchAPI.poll`` API has no way to tell callers *which* task a returned
payload describes — ``poll(notebook_id)`` silently returns the *latest*
task, so a caller that started task A may unknowingly act on results for
task B (the "cross-wire" bug).

The fix adds an OPTIONAL ``task_id`` discriminator to ``poll()`` and a
per-source ``research_task_id`` mismatch guard to ``import_sources()``.
Optional, not required: making it required would be a breaking change for
every existing caller. Old behavior is preserved when ``task_id`` is None,
with a ``DeprecationWarning`` raised on the actually-broken case (multiple
in-flight tasks + no discriminator).

Four scenarios:

A. **Explicit discriminator**: ``poll(nb, task_id="A")`` returns task A
   even when task B is also in flight; ``poll(nb, task_id="B")`` returns
   task B. No warning fires.
B. **Single in-flight, no discriminator**: ``poll(nb)`` returns the only
   task without any deprecation warning (old behavior, no ambiguity).
C. **Multiple in-flight, no discriminator**: ``poll(nb)`` emits a
   ``DeprecationWarning`` AND returns whatever old code returned (the
   latest task), preserving compatibility for legacy callers while
   surfacing the now-explicit hazard.
D. **import_sources mismatch**: passing ``task_id="A"`` together with a
   source whose ``research_task_id="B"`` raises
   :class:`ResearchTaskMismatchError` instead of silently importing
   under the wrong task.

These tests do not exercise the network — they assert on parsing /
filtering / warning semantics, which is the layer where the cross-wire
bug lives.
"""

from __future__ import annotations

import warnings

import pytest

from notebooklm import NotebookLMClient
from notebooklm.exceptions import ResearchTaskMismatchError
from notebooklm.rpc import RPCMethod

# Mock-only tests (no real HTTP, no cassette) — opt out of the
# integration-tree enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr


def _build_completed_task_payload(query: str, source_url: str, source_title: str) -> list:
    """Build a single ``POLL_RESEARCH`` task_info entry for a completed task.

    Status code ``2`` = completed (non-deep-research). Sources are encoded
    in the fast-research shape so ``research_task_id`` propagates onto
    each parsed source dict.
    """
    sources = [[source_url, source_title, "desc", 1]]
    return [None, [query, 1], 1, [sources, f"{query} summary"], 2]


@pytest.mark.asyncio
async def test_scenario_a_explicit_task_id_returns_matching_task(
    auth_tokens, httpx_mock, build_rpc_response
):
    """A. ``poll(nb, task_id="A")`` returns task A; ``task_id="B"`` returns task B.

    Two completed tasks are in flight on the same notebook. The optional
    discriminator filters down to the requested task. No warning is
    emitted in either case — the caller asked for an explicit task by id,
    so there is no ambiguity to surface.
    """
    task_a_payload = _build_completed_task_payload("query A", "https://a.example", "Result A")
    task_b_payload = _build_completed_task_payload("query B", "https://b.example", "Result B")
    # Two tasks in the response — ``poll`` currently returns the first
    # ("latest") on missing discriminator. ``task_id`` should pick either.
    response_body = build_rpc_response(
        RPCMethod.POLL_RESEARCH,
        [[["task_A", task_a_payload], ["task_B", task_b_payload]]],
    )

    httpx_mock.add_response(content=response_body.encode(), method="POST")
    async with NotebookLMClient(auth_tokens) as client:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result_a = await client.research.poll("nb_xwire", task_id="task_A")

    assert result_a["task_id"] == "task_A"
    assert result_a["query"] == "query A"
    # The ``tasks`` list should reflect the filtered view — only the
    # matched task remains, otherwise downstream callers iterating
    # ``tasks`` would still see the un-asked-for sibling.
    assert [t["task_id"] for t in result_a["tasks"]] == ["task_A"]
    assert result_a["sources"][0]["research_task_id"] == "task_A"

    # Fresh response for the second call — httpx_mock is per-request FIFO.
    httpx_mock.add_response(content=response_body.encode(), method="POST")
    async with NotebookLMClient(auth_tokens) as client:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result_b = await client.research.poll("nb_xwire", task_id="task_B")

    assert result_b["task_id"] == "task_B"
    assert result_b["query"] == "query B"
    assert [t["task_id"] for t in result_b["tasks"]] == ["task_B"]
    assert result_b["sources"][0]["research_task_id"] == "task_B"


@pytest.mark.asyncio
async def test_scenario_b_no_task_id_single_in_flight_no_warning(
    auth_tokens, httpx_mock, build_rpc_response
):
    """B. ``poll(nb)`` with a single in-flight task: old behavior, no warning.

    The deprecation warning fires only on the actually-broken case
    (ambiguous + missing discriminator). When only one task is in flight,
    there is nothing to disambiguate — surfacing a warning every poll
    would be noise for the dominant legacy usage pattern.
    """
    task_payload = _build_completed_task_payload("solo query", "https://solo.example", "Solo")
    response_body = build_rpc_response(
        RPCMethod.POLL_RESEARCH,
        [[["task_solo", task_payload]]],
    )

    httpx_mock.add_response(content=response_body.encode(), method="POST")
    async with NotebookLMClient(auth_tokens) as client:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = await client.research.poll("nb_solo")

    assert result["task_id"] == "task_solo"
    assert result["query"] == "solo query"
    # No deprecation warning — single in-flight task is unambiguous.
    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation_warnings == [], (
        f"Expected no DeprecationWarning on single-in-flight poll, got: "
        f"{[str(w.message) for w in deprecation_warnings]}"
    )


@pytest.mark.asyncio
async def test_scenario_c_no_task_id_multiple_in_flight_warns(
    auth_tokens, httpx_mock, build_rpc_response
):
    """C. ``poll(nb)`` with multiple in-flight tasks: warn + old return.

    The actually-broken case (cross-wire). We warn (don't error) to
    preserve back-compat — callers on the old API keep working until
    they migrate. The returned task must match the old "first task in
    list" behavior so existing consumers see no functional change.
    """
    task_a = _build_completed_task_payload("query A", "https://a.example", "Result A")
    task_b = _build_completed_task_payload("query B", "https://b.example", "Result B")
    response_body = build_rpc_response(
        RPCMethod.POLL_RESEARCH,
        [[["task_A", task_a], ["task_B", task_b]]],
    )

    httpx_mock.add_response(content=response_body.encode(), method="POST")
    async with NotebookLMClient(auth_tokens) as client:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = await client.research.poll("nb_ambig")

    # Old behavior: latest (first) task wins.
    assert result["task_id"] == "task_A"
    assert result["query"] == "query A"
    # All parsed tasks remain in ``tasks`` (old shape).
    assert [t["task_id"] for t in result["tasks"]] == ["task_A", "task_B"]

    # DeprecationWarning must fire exactly once for this ambiguity.
    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecation_warnings) == 1, (
        f"Expected one DeprecationWarning on ambiguous poll, got "
        f"{len(deprecation_warnings)}: {[str(w.message) for w in deprecation_warnings]}"
    )
    msg = str(deprecation_warnings[0].message)
    assert "task_id" in msg, f"Warning should mention task_id discriminator: {msg!r}"


@pytest.mark.asyncio
async def test_scenario_d_import_sources_mismatched_research_task_id_raises(auth_tokens):
    """D. ``import_sources(task_id="A", sources=[{research_task_id="B", ...}])`` raises.

    Per-source ``research_task_id`` (set by ``poll``) is now validated
    against the caller-supplied ``task_id`` for ``import_sources``. A
    mismatch is the wire-crossing bug — importing sources that were
    discovered for task B under task A would mis-attribute provenance.
    The new :class:`ResearchTaskMismatchError` makes this loud rather
    than silent.

    No RPC call is made: validation happens before the network.
    """
    async with NotebookLMClient(auth_tokens) as client:
        sources = [
            {
                "url": "https://a.example",
                "title": "Result A",
                "result_type": 1,
                "research_task_id": "task_B",  # mismatch — discovered under B
            },
        ]
        with pytest.raises(ResearchTaskMismatchError) as exc_info:
            await client.research.import_sources(
                notebook_id="nb_xwire",
                task_id="task_A",
                sources=sources,
            )

    err = exc_info.value
    # Diagnostic attributes — make it actionable in caller logs.
    assert err.task_id == "task_A"
    assert err.source_research_task_id == "task_B"
