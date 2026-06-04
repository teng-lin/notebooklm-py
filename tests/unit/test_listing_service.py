"""Unit tests for the shared CLI list-command service.

Split across the two layers:

* ``prepare_list`` (pure-data, lives in ``cli.services.listing``) is exercised
  for envelope ordering, item serialization, and the empty-state signal.
* ``render_list`` (presentation, lives in ``cli.rendering``) is exercised for
  JSON stdout output, empty-message printing, and Rich table assembly via a
  monkeypatched console.

The split mirrors the ADR-0008 boundary refactor in PR-C: ``cli.services.listing``
no longer imports anything from ``..rendering`` and never writes to stdout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm.cli import rendering
from notebooklm.cli.services import listing


@dataclass
class Thing:
    id: str
    title: str


class RecordingConsole:
    def __init__(self) -> None:
        self.printed: list[Any] = []

    def print(self, value: Any) -> None:
        self.printed.append(value)


@pytest.mark.asyncio
async def test_prepare_list_json_merges_envelope_extras_before_items_and_count():
    async def fetch(client: object, notebook_id: str) -> list[Thing]:
        assert client is fake_client
        assert notebook_id == "nb_123"
        return [Thing("thing_1", "Thing One"), Thing("thing_2", "Thing Two")]

    async def envelope_extras(client: object, notebook_id: str) -> dict[str, str]:
        assert client is fake_client
        assert notebook_id == "nb_123"
        return {"notebook_id": notebook_id, "notebook_title": "Notebook"}

    fake_client = object()
    spec = listing.ListSpec[Thing](
        title="Things in {notebook_id}",
        items_key="things",
        fetch=fetch,
        serialize=lambda thing: {"id": thing.id, "title": thing.title},
        columns=["ID", "Title"],
        row=lambda thing: [thing.id, thing.title],
        envelope_extras=envelope_extras,
    )

    render = await listing.prepare_list(
        spec,
        fake_client,
        notebook_id="nb_123",
        limit=1,
        json_output=True,
    )

    # prepare_list returns pure data — no stdout writes happened.
    assert render.json_envelope is not None
    assert list(render.json_envelope) == ["notebook_id", "notebook_title", "things", "count"]
    assert render.json_envelope == {
        "notebook_id": "nb_123",
        "notebook_title": "Notebook",
        "things": [{"index": 1, "id": "thing_1", "title": "Thing One"}],
        "count": 1,
    }
    assert render.items == [Thing("thing_1", "Thing One")]

    # And the backward-compat ListResult view round-trips the envelope.
    legacy = render.to_list_result()
    assert legacy.items == render.items
    assert legacy.envelope == render.json_envelope


@pytest.mark.asyncio
async def test_render_list_json_writes_envelope_to_stdout(capsys):
    render = listing.ListRender(
        items=[Thing("thing_1", "Thing One")],
        title="Things in nb_123",
        json_envelope={
            "notebook_id": "nb_123",
            "notebook_title": "Notebook",
            "things": [{"index": 1, "id": "thing_1", "title": "Thing One"}],
            "count": 1,
        },
    )

    rendering.render_list(render)

    data = json.loads(capsys.readouterr().out)
    assert list(data) == ["notebook_id", "notebook_title", "things", "count"]
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_prepare_list_text_returns_table_payload_without_calling_envelope_extras():
    extras_called = False

    async def fetch(client: object, notebook_id: str) -> list[Thing]:
        return [Thing("thing_1", f"Thing in {notebook_id}")]

    async def envelope_extras(client: object, notebook_id: str) -> dict[str, str]:
        nonlocal extras_called
        extras_called = True
        return {"notebook_id": notebook_id}

    spec = listing.ListSpec[Thing](
        title="Things in {notebook_id}",
        items_key="things",
        fetch=fetch,
        serialize=lambda thing: {"id": thing.id, "title": thing.title},
        columns=["ID", "Title"],
        row=lambda thing: [thing.id, thing.title],
        envelope_extras=envelope_extras,
    )

    render = await listing.prepare_list(
        spec,
        object(),
        notebook_id="nb_123",
        limit=None,
        json_output=False,
        no_truncate=True,
    )

    assert not extras_called
    assert render.json_envelope is None
    assert render.empty_message is None
    assert render.title == "Things in nb_123"
    assert render.columns == ["ID", "Title"]
    assert render.rows == [["thing_1", "Thing in nb_123"]]
    assert render.no_truncate is True
    assert render.items == [Thing("thing_1", "Thing in nb_123")]


def test_render_list_text_renders_table_with_column_headers(monkeypatch):
    console = RecordingConsole()
    monkeypatch.setattr(rendering, "console", console)
    render = listing.ListRender(
        items=[Thing("thing_1", "Thing in nb_123")],
        title="Things in nb_123",
        columns=["ID", "Title"],
        rows=[["thing_1", "Thing in nb_123"]],
        no_truncate=True,
    )

    rendering.render_list(render)

    assert len(console.printed) == 1
    table = console.printed[0]
    assert table.title == "Things in nb_123"
    assert [column.header for column in table.columns] == ["ID", "Title"]


@pytest.mark.asyncio
async def test_prepare_list_text_signals_empty_message_for_empty_items():
    async def fetch(client: object, notebook_id: str) -> list[Thing]:
        return []

    spec = listing.ListSpec[Thing](
        title="Things in {notebook_id}",
        items_key="things",
        fetch=fetch,
        serialize=lambda thing: {"id": thing.id, "title": thing.title},
        columns=["ID", "Title"],
        row=lambda thing: [thing.id, thing.title],
        empty_message="[yellow]No things found[/yellow]",
    )

    render = await listing.prepare_list(
        spec,
        object(),
        notebook_id="nb_123",
        limit=None,
        json_output=False,
    )

    assert render.items == []
    assert render.empty_message == "[yellow]No things found[/yellow]"
    # Empty-state mode: no table payload built.
    assert render.columns == []
    assert render.rows == []


def test_render_list_text_prints_empty_message(monkeypatch):
    console = RecordingConsole()
    monkeypatch.setattr(rendering, "console", console)
    render = listing.ListRender(
        items=[],
        title="Things in nb_123",
        empty_message="[yellow]No things found[/yellow]",
    )

    rendering.render_list(render)

    assert console.printed == ["[yellow]No things found[/yellow]"]
