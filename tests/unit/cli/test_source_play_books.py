"""CLI tests for ``source books`` and ``source add-book`` (#2292)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from notebooklm.exceptions import PlayBookNotExportableError
from notebooklm.notebooklm_cli import cli
from notebooklm.types import PlayBook, Source

from .conftest import create_mock_client, inject_client


@pytest.fixture
def runner():
    return CliRunner()


def _book(content_id: str, title: str, *, disabled: bool = False) -> PlayBook:
    return PlayBook(
        content_id=content_id,
        title=title,
        authors=("Sun Tzu",),
        description_html="<p>…</p>",
        cover_url="https://cover",
        export_disabled=disabled,
        reason=None,
        field_type=4.6,
        updated_at=None,
    )


class TestSourceBooks:
    def test_text_mode_lists_titles(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.list_play_books = AsyncMock(
            return_value=[_book("QhsZEAAAQBAJ", "The Art of War")]
        )
        result = runner.invoke(cli, ["source", "books"], obj=inject_client(client))
        assert result.exit_code == 0, result.output
        assert "The Art of War" in result.output
        assert "QhsZEAAAQBAJ" in result.output

    def test_json_mode_emits_library(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.list_play_books = AsyncMock(
            return_value=[
                _book("QhsZEAAAQBAJ", "The Art of War"),
                _book("kLrxEQAAQBAJ", "Bill Gates", disabled=True),
            ]
        )
        result = runner.invoke(cli, ["source", "books", "--json"], obj=inject_client(client))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 2
        assert payload["play_books"][0]["content_id"] == "QhsZEAAAQBAJ"
        assert payload["play_books"][1]["export_disabled"] is True

    def test_empty_library_text(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.list_play_books = AsyncMock(return_value=[])
        result = runner.invoke(cli, ["source", "books"], obj=inject_client(client))
        assert result.exit_code == 0, result.output
        assert "No Google Play Books" in result.output


class TestSourceAddBook:
    def test_text_mode_prints_added_line(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.add_play_book = AsyncMock(
            return_value=Source(id="src_book", title="The Art of War", _type_code=20)
        )
        result = runner.invoke(
            cli,
            ["source", "add-book", "QhsZEAAAQBAJ", "-n", "nb_123"],
            obj=inject_client(client),
        )
        assert result.exit_code == 0, result.output
        assert "Added Play Book source:" in result.output
        assert "src_book" in result.output
        client.sources.add_play_book.assert_awaited_once_with(
            "nb_123", "QhsZEAAAQBAJ", wait=False, wait_timeout=120.0
        )

    def test_json_mode_envelope(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.add_play_book = AsyncMock(
            return_value=Source(id="src_book", title="The Art of War", _type_code=20)
        )
        result = runner.invoke(
            cli,
            ["source", "add-book", "QhsZEAAAQBAJ", "-n", "nb_123", "--wait", "--json"],
            obj=inject_client(client),
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["action"] == "add-book"
        assert payload["content_id"] == "QhsZEAAAQBAJ"
        assert payload["source"]["id"] == "src_book"
        assert payload["source"]["type"] == "expert_intelligence"
        client.sources.add_play_book.assert_awaited_once_with(
            "nb_123", "QhsZEAAAQBAJ", wait=True, wait_timeout=120.0
        )

    def test_blocked_title_errors(self, runner, mock_auth, mock_fetch_tokens):
        client = create_mock_client()
        client.sources.add_play_book = AsyncMock(
            side_effect=PlayBookNotExportableError("kLrxEQAAQBAJ")
        )
        result = runner.invoke(
            cli,
            ["source", "add-book", "kLrxEQAAQBAJ", "-n", "nb_123"],
            obj=inject_client(client),
        )
        assert result.exit_code != 0
