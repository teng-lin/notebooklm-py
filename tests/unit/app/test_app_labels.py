"""Unit tests for the transport-neutral ``notebooklm._app.labels`` mutation core.

The composite ``<id|name>`` :func:`resolve_label_id` resolver (id /
unambiguous-prefix / exact-name / ambiguity) is already covered directly in
``tests/unit/cli/test_label_listing.py`` (it re-exports the *same* ``_app``
function), so these tests do **not** duplicate that. They pin the label
**mutation** executors — which had no direct app coverage — plus the
:class:`LabelResolutionError` attribute contract, at the ``_app`` boundary with
a ``MagicMock`` client (no Click / ``CliRunner``):

* ``create`` / ``sources`` / ``generate`` / ``rename`` / ``emoji`` /
  ``add`` / ``remove`` / ``delete`` executors delegate to the right
  ``client.labels`` RPC and project the typed results,
* :class:`LabelResolutionError` carries ``message`` / ``code`` / ``extra`` and
  stays inside the public ``ValidationError`` hierarchy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.labels import (
    LabelGenerateResult,
    LabelMembershipResult,
    LabelResolutionError,
    execute_label_add_sources,
    execute_label_create,
    execute_label_delete,
    execute_label_generate,
    execute_label_remove_sources,
    execute_label_rename,
    execute_label_set_emoji,
    execute_label_sources,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import Label, Source


def _client() -> MagicMock:
    client = MagicMock()
    client.labels = MagicMock()
    return client


# ---------------------------------------------------------------------------
# LabelResolutionError — typed attribute contract
# ---------------------------------------------------------------------------


def test_label_resolution_error_carries_code_and_extra() -> None:
    err = LabelResolutionError("No match", "NOT_FOUND", {"id": "x"})
    assert err.message == "No match"
    assert err.code == "NOT_FOUND"
    assert err.extra == {"id": "x"}
    # Stays inside the public NotebookLMError hierarchy (errors.classify covers it).
    assert isinstance(err, ValidationError)
    # The str includes the code for adapter-agnostic diagnostics.
    assert "code=NOT_FOUND" in str(err)


def test_label_resolution_error_extra_defaults_to_none() -> None:
    err = LabelResolutionError("Ambiguous", "AMBIGUOUS_NAME")
    assert err.extra is None
    assert err.code == "AMBIGUOUS_NAME"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_create_delegates() -> None:
    client = _client()
    label = Label(id="lbl_1", name="Papers", emoji="📄")
    client.labels.create = AsyncMock(return_value=label)

    result = await execute_label_create(client, "nb_1", "Papers", "📄")

    assert result is label
    client.labels.create.assert_awaited_once_with("nb_1", "Papers", "📄")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_sources_expands_to_source_objects() -> None:
    client = _client()
    sources = [Source(id="s1", title="First"), Source(id="s2", title="Second")]
    client.labels.sources = AsyncMock(return_value=sources)

    result = await execute_label_sources(client, "nb_1", "lbl_1")

    assert result == sources
    client.labels.sources.assert_awaited_once_with("nb_1", "lbl_1")


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_generate_projects_result() -> None:
    client = _client()
    labels = [Label(id="lbl_1", name="Auto")]
    client.labels.generate = AsyncMock(return_value=labels)

    result = await execute_label_generate(client, "nb_1", "unlabeled")

    assert isinstance(result, LabelGenerateResult)
    assert result.notebook_id == "nb_1"
    assert result.scope == "unlabeled"
    assert result.labels == labels
    client.labels.generate.assert_awaited_once_with("nb_1", scope="unlabeled")


# ---------------------------------------------------------------------------
# rename / emoji
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_rename_returns_label() -> None:
    client = _client()
    label = Label(id="lbl_1", name="New name", emoji="📄")
    client.labels.rename = AsyncMock(return_value=label)

    result = await execute_label_rename(client, "nb_1", "lbl_1", "New name")

    assert result is label
    client.labels.rename.assert_awaited_once_with("nb_1", "lbl_1", "New name")


@pytest.mark.asyncio
async def test_execute_label_set_emoji_returns_label() -> None:
    client = _client()
    label = Label(id="lbl_1", name="Papers", emoji="🧠")
    client.labels.set_emoji = AsyncMock(return_value=label)

    result = await execute_label_set_emoji(client, "nb_1", "lbl_1", "🧠")

    assert result is label
    client.labels.set_emoji.assert_awaited_once_with("nb_1", "lbl_1", "🧠")


# ---------------------------------------------------------------------------
# add / remove sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_add_sources_projects_membership() -> None:
    client = _client()
    label = Label(id="lbl_1", name="Papers", source_ids=["s1", "s2"])
    client.labels.add_sources = AsyncMock(return_value=label)

    result = await execute_label_add_sources(client, "nb_1", "lbl_1", ("s1", "s2"))

    assert isinstance(result, LabelMembershipResult)
    assert result.label is label
    assert result.source_ids == ["s1", "s2"]
    client.labels.add_sources.assert_awaited_once_with("nb_1", "lbl_1", ["s1", "s2"])


@pytest.mark.asyncio
async def test_execute_label_remove_sources_projects_membership() -> None:
    client = _client()
    label = Label(id="lbl_1", name="Papers", source_ids=[])
    client.labels.remove_sources = AsyncMock(return_value=label)

    result = await execute_label_remove_sources(client, "nb_1", "lbl_1", ["s1"])

    assert result.label is label
    assert result.source_ids == ["s1"]
    client.labels.remove_sources.assert_awaited_once_with("nb_1", "lbl_1", ["s1"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_label_delete_passes_id_list() -> None:
    client = _client()
    client.labels.delete = AsyncMock(return_value=None)

    await execute_label_delete(client, "nb_1", ("lbl_1", "lbl_2"))

    client.labels.delete.assert_awaited_once_with("nb_1", ["lbl_1", "lbl_2"])
