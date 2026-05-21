"""Unit tests for the shared CLI list-command service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

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
async def test_run_list_json_merges_envelope_extras_before_items_and_count(capsys):
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

    result = await listing.run_list(
        spec,
        fake_client,
        notebook_id="nb_123",
        limit=1,
        json_output=True,
    )

    data = json.loads(capsys.readouterr().out)
    assert list(data) == ["notebook_id", "notebook_title", "things", "count"]
    assert data == {
        "notebook_id": "nb_123",
        "notebook_title": "Notebook",
        "things": [{"index": 1, "id": "thing_1", "title": "Thing One"}],
        "count": 1,
    }
    assert result.items == [Thing("thing_1", "Thing One")]
    assert result.envelope == data


@pytest.mark.asyncio
async def test_run_list_text_renders_table_without_envelope_extras(monkeypatch):
    extras_called = False

    async def fetch(client: object, notebook_id: str) -> list[Thing]:
        return [Thing("thing_1", f"Thing in {notebook_id}")]

    async def envelope_extras(client: object, notebook_id: str) -> dict[str, str]:
        nonlocal extras_called
        extras_called = True
        return {"notebook_id": notebook_id}

    console = RecordingConsole()
    monkeypatch.setattr(listing, "console", console)
    spec = listing.ListSpec[Thing](
        title="Things in {notebook_id}",
        items_key="things",
        fetch=fetch,
        serialize=lambda thing: {"id": thing.id, "title": thing.title},
        columns=["ID", "Title"],
        row=lambda thing: [thing.id, thing.title],
        envelope_extras=envelope_extras,
    )

    result = await listing.run_list(
        spec,
        object(),
        notebook_id="nb_123",
        limit=None,
        json_output=False,
        no_truncate=True,
    )

    assert not extras_called
    assert result.envelope is None
    assert result.items == [Thing("thing_1", "Thing in nb_123")]
    assert len(console.printed) == 1
    table = console.printed[0]
    assert table.title == "Things in nb_123"
    assert [column.header for column in table.columns] == ["ID", "Title"]


@pytest.mark.asyncio
async def test_run_list_text_uses_empty_message_instead_of_empty_table(monkeypatch):
    async def fetch(client: object, notebook_id: str) -> list[Thing]:
        return []

    console = RecordingConsole()
    monkeypatch.setattr(listing, "console", console)
    spec = listing.ListSpec[Thing](
        title="Things in {notebook_id}",
        items_key="things",
        fetch=fetch,
        serialize=lambda thing: {"id": thing.id, "title": thing.title},
        columns=["ID", "Title"],
        row=lambda thing: [thing.id, thing.title],
        empty_message="[yellow]No things found[/yellow]",
    )

    result = await listing.run_list(
        spec,
        object(),
        notebook_id="nb_123",
        limit=None,
        json_output=False,
    )

    assert result.items == []
    assert console.printed == ["[yellow]No things found[/yellow]"]
