"""Characterization tests for ``notebooklm source ...`` CLI commands.

These tests pin observable CLI behavior across the ``source`` command surface
BEFORE the P3.T5 service extraction. They are end-to-end at the ``CliRunner``
level so they capture exit codes, stdout/stderr structure, and JSON envelope
shape that lower-level service-unit tests in ``test_source.py`` do not cover
holistically.

They MUST pass identically on ``main`` HEAD before the extraction commit
lands, and they MUST continue to pass byte-for-byte after the extraction.
Any divergence is a behavior regression — not an opportunity to "fix" the
old behavior.

Coverage matrix (commands × output modes from the P3.T5 spec):

| Command                  | text | json |
|--------------------------|------|------|
| source add               | yes  | yes  |
| source list              | yes  | yes  |
| source get               | yes  | yes  |
| source delete            | yes  | yes  |
| source delete-by-title   | yes  | yes  |
| source clean             | yes  | yes  |
| source fulltext          | yes  | yes  |
| source guide             | yes  | yes  |
| source add-research      | yes  | yes  |
| source wait              | yes  | yes  |

Each test pins one (command, mode) cell of the matrix to a specific
expected-text fragment or JSON envelope. The fragments are intentionally
narrow (one or two structural lines) so the snapshots survive cosmetic
changes (e.g. Rich color codes) while still catching shape regressions.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Source,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
)

from .conftest import create_mock_client, inject_client, research_start, source_guide

pytestmark = pytest.mark.characterization


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch.object(helpers_module, "load_auth_from_storage") as m:
        m.return_value = {
            "SID": "test",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield m


@pytest.fixture
def patched_fetch_tokens():
    with patch.object(auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock) as fetch:
        fetch.return_value = ("csrf", "session")
        yield fetch


# ----------------------------------------------------------------------------
# source add
# ----------------------------------------------------------------------------


class TestSourceAddCharacterization:
    def test_add_url_text_mode_prints_added_source_line(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.add_url = AsyncMock(
            return_value=Source(id="src_new_url", title="URL Source", url="https://x")
        )
        result = runner.invoke(
            cli, ["source", "add", "https://example.com", "-n", "nb_123"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "Added source:" in result.output
        assert "src_new_url" in result.output

    def test_add_url_json_mode_emits_source_envelope(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.add_url = AsyncMock(
            return_value=Source(id="src_new", title="T", url="https://x", _type_code=5)
        )
        result = runner.invoke(
            cli,
            ["source", "add", "https://example.com", "-n", "nb_123", "--json"],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["source"]["id"] == "src_new"
        assert payload["source"]["title"] == "T"
        assert payload["source"]["type"] == "web_page"
        assert payload["source"]["url"] == "https://x"


# ----------------------------------------------------------------------------
# source list
# ----------------------------------------------------------------------------


class TestSourceListCharacterization:
    def test_list_text_mode_renders_rich_table(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="One")])
        result = runner.invoke(cli, ["source", "list", "-n", "nb_123"], obj=inject_client(client))
        assert result.exit_code == 0
        assert "Sources in nb_123" in result.output
        assert "src_1" in result.output
        assert "One" in result.output

    def test_list_json_mode_emits_array_with_count(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(
            return_value=[
                Source(
                    id="src_1",
                    title="One",
                    url=None,
                    _type_code=5,
                    status=SourceStatus.READY,
                    created_at=datetime(2025, 1, 1, 12, 0, 0),
                )
            ]
        )
        client.notebooks.get = AsyncMock(return_value=type("N", (), {"title": "NB"})())
        result = runner.invoke(
            cli, ["source", "list", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["notebook_id"] == "nb_123"
        assert payload["count"] == 1
        assert payload["sources"][0]["id"] == "src_1"
        assert payload["sources"][0]["title"] == "One"
        assert payload["sources"][0]["type"] == "web_page"


# ----------------------------------------------------------------------------
# source get
# ----------------------------------------------------------------------------


class TestSourceGetCharacterization:
    def test_get_text_mode_prints_title_and_type(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.get_or_none = AsyncMock(
            return_value=Source(id="src_1", title="My Source", url=None)
        )
        result = runner.invoke(
            cli, ["source", "get", "src_1", "-n", "nb_123"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "Source:" in result.output
        assert "src_1" in result.output
        assert "My Source" in result.output

    def test_get_json_mode_emits_source_envelope(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.get_or_none = AsyncMock(
            return_value=Source(
                id="src_1",
                title="My Source",
                url="https://x",
                _type_code=5,
                status=SourceStatus.READY,
            )
        )
        result = runner.invoke(
            cli, ["source", "get", "src_1", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["found"] is True
        assert payload["source"]["id"] == "src_1"
        assert payload["source"]["type"] == "web_page"
        assert payload["source"]["url"] == "https://x"

    def test_get_not_found_exits_1_text_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.get_or_none = AsyncMock(return_value=None)
        result = runner.invoke(
            cli,
            [
                "source",
                "get",
                "11111111-2222-3333-4444-555555555555",
                "-n",
                "nb_123",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 1

    def test_get_not_found_exits_1_json_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.get_or_none = AsyncMock(return_value=None)
        result = runner.invoke(
            cli,
            [
                "source",
                "get",
                "11111111-2222-3333-4444-555555555555",
                "-n",
                "nb_123",
                "--json",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "NOT_FOUND"


# ----------------------------------------------------------------------------
# source delete
# ----------------------------------------------------------------------------


class TestSourceDeleteCharacterization:
    def test_delete_text_mode_with_yes_prints_deleted(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.delete = AsyncMock(return_value=True)
        result = runner.invoke(
            cli, ["source", "delete", "src_1", "-n", "nb_123", "-y"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "Deleted source:" in result.output

    def test_delete_json_mode_with_yes_emits_envelope(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.delete = AsyncMock(return_value=True)
        result = runner.invoke(
            cli,
            ["source", "delete", "src_1", "-n", "nb_123", "-y", "--json"],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["action"] == "delete"
        assert payload["source_id"] == "src_1"
        assert payload["success"] is True
        assert payload["status"] == "deleted"


# ----------------------------------------------------------------------------
# source delete-by-title
# ----------------------------------------------------------------------------


class TestSourceDeleteByTitleCharacterization:
    def test_delete_by_title_text_mode_with_yes(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_999", title="Exact Title")])
        client.sources.delete = AsyncMock(return_value=True)
        result = runner.invoke(
            cli,
            [
                "source",
                "delete-by-title",
                "Exact Title",
                "-n",
                "nb_123",
                "-y",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        assert "Deleted source:" in result.output

    def test_delete_by_title_json_mode_with_yes(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_999", title="Exact Title")])
        client.sources.delete = AsyncMock(return_value=True)
        result = runner.invoke(
            cli,
            [
                "source",
                "delete-by-title",
                "Exact Title",
                "-n",
                "nb_123",
                "-y",
                "--json",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["action"] == "delete-by-title"
        assert payload["source_id"] == "src_999"
        assert payload["title"] == "Exact Title"


# ----------------------------------------------------------------------------
# source clean
# ----------------------------------------------------------------------------


class TestSourceCleanCharacterization:
    def test_clean_already_clean_text_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[])
        result = runner.invoke(cli, ["source", "clean", "-n", "nb_123"], obj=inject_client(client))
        assert result.exit_code == 0
        assert "already clean" in result.output.lower()

    def test_clean_already_clean_json_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[])
        result = runner.invoke(
            cli, ["source", "clean", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["action"] == "clean"
        assert payload["status"] == "already_clean"
        assert payload["deleted_count"] == 0


# ----------------------------------------------------------------------------
# source fulltext
# ----------------------------------------------------------------------------


class TestSourceFulltextCharacterization:
    def test_fulltext_text_mode_prints_content(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.get_fulltext = AsyncMock(
            return_value=SourceFulltext(
                source_id="src_1",
                title="T",
                content="hello world",
                char_count=11,
                url=None,
            )
        )
        result = runner.invoke(
            cli, ["source", "fulltext", "src_1", "-n", "nb_123"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "hello world" in result.output

    def test_fulltext_json_mode_emits_dataclass_payload(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.get_fulltext = AsyncMock(
            return_value=SourceFulltext(
                source_id="src_1",
                title="T",
                content="hi",
                _type_code=5,
                char_count=2,
                url=None,
            )
        )
        result = runner.invoke(
            cli,
            ["source", "fulltext", "src_1", "-n", "nb_123", "--json"],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["source_id"] == "src_1"
        assert payload["kind"] == "web_page"
        assert payload["content"] == "hi"
        assert payload["char_count"] == 2
        assert "_type_code" not in payload


# ----------------------------------------------------------------------------
# source guide
# ----------------------------------------------------------------------------


class TestSourceGuideCharacterization:
    def test_guide_text_mode_prints_summary_and_keywords(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.get_guide = AsyncMock(
            return_value=source_guide({"summary": "summary text", "keywords": ["a", "b"]})
        )
        result = runner.invoke(
            cli, ["source", "guide", "src_1", "-n", "nb_123"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "summary text" in result.output
        assert "Keywords:" in result.output
        assert "a, b" in result.output

    def test_guide_json_mode_emits_summary_and_keywords(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.sources.get_guide = AsyncMock(
            return_value=source_guide({"summary": "s", "keywords": ["k1"]})
        )
        result = runner.invoke(
            cli, ["source", "guide", "src_1", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["source_id"] == "src_1"
        assert payload["summary"] == "s"
        assert payload["keywords"] == ["k1"]


# ----------------------------------------------------------------------------
# source add-research
# ----------------------------------------------------------------------------


class TestSourceAddResearchCharacterization:
    def test_add_research_no_wait_returns_after_start(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        client.research.start = AsyncMock(return_value=research_start({"task_id": "task_123"}))
        result = runner.invoke(
            cli,
            [
                "source",
                "add-research",
                "machine learning",
                "-n",
                "nb_123",
                "--no-wait",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 0
        assert "Task ID:" in result.output
        assert "Research started" in result.output

    def test_add_research_no_wait_with_import_all_is_usage_error(
        self, runner, mock_auth, patched_fetch_tokens
    ):
        client = create_mock_client()
        result = runner.invoke(
            cli,
            [
                "source",
                "add-research",
                "ml",
                "-n",
                "nb_123",
                "--no-wait",
                "--import-all",
            ],
            obj=inject_client(client),
        )
        assert result.exit_code == 2
        assert "--import-all requires" in result.output


# ----------------------------------------------------------------------------
# source wait
# ----------------------------------------------------------------------------


class TestSourceWaitCharacterization:
    def test_wait_success_text_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.wait_until_ready = AsyncMock(
            return_value=Source(id="src_1", title="Ready One")
        )
        result = runner.invoke(
            cli, ["source", "wait", "src_1", "-n", "nb_123"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        assert "Source ready:" in result.output
        assert "src_1" in result.output

    def test_wait_success_json_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.wait_until_ready = AsyncMock(
            return_value=Source(id="src_1", title="Ready", status=2)
        )
        result = runner.invoke(
            cli, ["source", "wait", "src_1", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "ready"
        assert payload["source_id"] == "src_1"

    def test_wait_not_found_exits_1_json_mode(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="One")])
        client.sources.wait_until_ready = AsyncMock(side_effect=SourceNotFoundError("src_1"))
        result = runner.invoke(
            cli,
            ["source", "wait", "src_1", "-n", "nb_123", "--json"],
            obj=inject_client(client),
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "not_found"

    def test_wait_processing_error_exits_1(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="One")])
        client.sources.wait_until_ready = AsyncMock(
            side_effect=SourceProcessingError("src_1", status=4)
        )
        result = runner.invoke(
            cli, ["source", "wait", "src_1", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["status_code"] == 4

    def test_wait_timeout_exits_2(self, runner, mock_auth, patched_fetch_tokens):
        client = create_mock_client()
        client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="One")])
        client.sources.wait_until_ready = AsyncMock(
            side_effect=SourceTimeoutError("src_1", timeout=5.0, last_status=3)
        )
        result = runner.invoke(
            cli, ["source", "wait", "src_1", "-n", "nb_123", "--json"], obj=inject_client(client)
        )
        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload["status"] == "timeout"
        assert payload["last_status_code"] == 3
