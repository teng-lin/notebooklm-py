"""Unit tests for the ``label`` CLI service (``cli/services/label_listing.py``).

Covers:

* :func:`resolve_label_id` — id / unambiguous-prefix / exact-name resolution,
  id-vs-name precedence (including a UUID-shaped *name*), and the ambiguous-name
  error that lists candidates (id + emoji + source count).
* The members→titles join: a single ``sources.list()`` builds the
  ``{source_id: title}`` map (no N+1).
* :func:`execute_label_list` — the ``LabelListPlan`` executor produces the
  ``{"labels": [...], "count": N}`` envelope with member ids + titles.

This is a pure-service test (no Click / CliRunner) — the command-layer wiring is
exercised in ``test_label_cmd.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm.cli.services import label_listing
from notebooklm.cli.services.label_listing import (
    LabelListPlan,
    LabelResolutionError,
    execute_label_list,
    resolve_label_id,
)
from notebooklm.types import Label, Source


def _make_client(*, labels: list[Label], sources: list[Source] | None = None) -> MagicMock:
    client = MagicMock()
    client.labels = MagicMock()
    client.sources = MagicMock()
    client.labels.list = AsyncMock(return_value=labels)
    client.sources.list = AsyncMock(return_value=sources or [])
    return client


# ---------------------------------------------------------------------------
# resolve_label_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_exact_id() -> None:
    labels = [
        Label(id="lblaaa111", name="Papers", emoji="📄"),
        Label(id="lblbbb222", name="Topics", emoji="🧠"),
    ]
    client = _make_client(labels=labels)
    resolved = await resolve_label_id(client, "nb_1", "lblaaa111")
    assert resolved == "lblaaa111"


@pytest.mark.asyncio
async def test_resolve_by_unambiguous_prefix() -> None:
    labels = [
        Label(id="lblaaa111", name="Papers"),
        Label(id="lblbbb222", name="Topics"),
    ]
    client = _make_client(labels=labels)
    resolved = await resolve_label_id(client, "nb_1", "lblaaa")
    assert resolved == "lblaaa111"


@pytest.mark.asyncio
async def test_resolve_by_exact_name() -> None:
    labels = [
        Label(id="lblaaa111", name="Papers"),
        Label(id="lblbbb222", name="Topics"),
    ]
    client = _make_client(labels=labels)
    resolved = await resolve_label_id(client, "nb_1", "Topics")
    assert resolved == "lblbbb222"


@pytest.mark.asyncio
async def test_resolve_ambiguous_name_lists_candidates() -> None:
    labels = [
        Label(id="lblaaa111", name="Dup", emoji="📄", source_ids=["s1", "s2"]),
        Label(id="lblbbb222", name="Dup", emoji="🧠", source_ids=["s3"]),
    ]
    client = _make_client(labels=labels)
    with pytest.raises(LabelResolutionError) as exc:
        await resolve_label_id(client, "nb_1", "Dup")
    message = str(exc.value)
    # Both candidate ids, emojis, and source counts are surfaced.
    assert "lblaaa111" in message
    assert "lblbbb222" in message
    assert "📄" in message
    assert "🧠" in message
    assert "2" in message  # first candidate source count
    assert "1" in message  # second candidate source count


@pytest.mark.asyncio
async def test_resolve_ambiguous_prefix_lists_candidates() -> None:
    """An ambiguous id *prefix* raises AMBIGUOUS_ID with candidates, not NOT_FOUND.

    Regression guard: ``resolve_partial_id_in_items`` raises the id-pass miss
    sentinel for both "no match" and "ambiguous prefix", so an ambiguous prefix
    must be detected distinctly here instead of falling through to the name pass
    (which would report a misleading NOT_FOUND and lose the candidate list).
    """
    labels = [
        Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1", "s2"]),
        Label(id="lblaaa222", name="Topics", emoji="🧠", source_ids=["s3"]),
    ]
    client = _make_client(labels=labels)
    with pytest.raises(LabelResolutionError) as exc:
        # ``lblaaa`` is a prefix of BOTH ids — ambiguous.
        await resolve_label_id(client, "nb_1", "lblaaa")
    assert exc.value.code == "AMBIGUOUS_ID"
    # Candidate ids, emojis, and source counts are surfaced (message + extra).
    message = str(exc.value)
    assert "lblaaa111" in message
    assert "lblaaa222" in message
    assert "📄" in message
    assert "🧠" in message
    assert exc.value.extra is not None
    candidate_ids = {c["id"] for c in exc.value.extra["candidates"]}
    assert candidate_ids == {"lblaaa111", "lblaaa222"}


@pytest.mark.asyncio
async def test_resolve_no_match_raises() -> None:
    labels = [Label(id="lblaaa111", name="Papers")]
    client = _make_client(labels=labels)
    with pytest.raises(LabelResolutionError):
        await resolve_label_id(client, "nb_1", "nope")


@pytest.mark.asyncio
async def test_resolve_uuid_shaped_name_after_id_pass_misses() -> None:
    """A UUID-shaped *name* is found by the name pass once the id pass misses.

    Full-id passthrough is disabled, so a canonical-UUID token that is not an
    actual label id falls through to the exact-name pass.
    """
    uuid_name = "12345678-1234-4abc-8def-1234567890ab"
    labels = [Label(id="lblaaa111", name=uuid_name, emoji="📄")]
    client = _make_client(labels=labels)
    resolved = await resolve_label_id(client, "nb_1", uuid_name)
    assert resolved == "lblaaa111"


@pytest.mark.asyncio
async def test_resolve_calls_labels_list_not_sources() -> None:
    labels = [Label(id="lblaaa111", name="Papers")]
    client = _make_client(labels=labels)
    await resolve_label_id(client, "nb_1", "lblaaa111")
    client.labels.list.assert_awaited_once_with("nb_1")
    client.sources.list.assert_not_called()


# ---------------------------------------------------------------------------
# execute_label_list — join + envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_list_json_joins_titles_single_sources_call() -> None:
    labels = [
        Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1", "s2"]),
        Label(id="lblbbb222", name="Topics", emoji=None, source_ids=[]),
    ]
    sources = [
        Source(id="s1", title="First"),
        Source(id="s2", title="Second"),
    ]
    client = _make_client(labels=labels, sources=sources)
    plan = LabelListPlan(notebook_id="nb_1", json_output=True, limit=None, no_truncate=False)

    render = await execute_label_list(client, plan)

    # No N+1: exactly one sources.list() and one labels.list().
    client.sources.list.assert_awaited_once_with("nb_1")
    client.labels.list.assert_awaited_once_with("nb_1")

    envelope = render.json_envelope
    assert envelope is not None
    assert envelope["count"] == 2
    items = envelope["labels"]
    first = items[0]
    assert first["id"] == "lblaaa111"
    assert first["name"] == "Papers"
    assert first["emoji"] == "📄"
    assert first["source_ids"] == ["s1", "s2"]
    # Member titles are resolved from the single sources.list().
    assert first["sources"] == [
        {"id": "s1", "title": "First"},
        {"id": "s2", "title": "Second"},
    ]
    second = items[1]
    assert second["id"] == "lblbbb222"
    assert second["source_ids"] == []
    assert second["sources"] == []


@pytest.mark.asyncio
async def test_execute_label_list_table_mode_builds_rows() -> None:
    labels = [Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1"])]
    sources = [Source(id="s1", title="First")]
    client = _make_client(labels=labels, sources=sources)
    plan = LabelListPlan(notebook_id="nb_1", json_output=False, limit=None, no_truncate=False)

    render = await execute_label_list(client, plan)

    assert render.json_envelope is None
    assert render.columns
    assert render.rows
    # The single row surfaces the label id + name.
    flat = " ".join(str(cell) for cell in render.rows[0])
    assert "lblaaa111" in flat
    assert "Papers" in flat


def test_module_is_boundary_clean() -> None:
    """Service module must not export Click / rendering symbols (ADR-0008)."""
    assert not hasattr(label_listing, "click")
