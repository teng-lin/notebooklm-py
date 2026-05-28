"""CliRunner coverage for source-content command rendering branches."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Source, SourceFulltext

from .conftest import create_mock_client


@contextmanager
def _patched_source_client(client) -> Iterator[None]:
    with (
        patch("notebooklm.cli.source_cmd.NotebookLMClient") as client_cls,
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as fetch_tokens,
    ):
        fetch_tokens.return_value = ("csrf", "session")
        client_cls.return_value = client
        yield


def _client_resolving_source(source_id: str = "src_1", title: str = "Source One"):
    client = create_mock_client()
    client.sources.list = AsyncMock(return_value=[Source(id=source_id, title=title)])
    return client


@pytest.mark.parametrize("json_output", [False, True])
def test_source_get_not_found_renderer_exits_one(
    runner: CliRunner,
    mock_auth,
    json_output: bool,
) -> None:
    source_id = "11111111-2222-3333-4444-555555555555"
    client = create_mock_client()
    client.sources.get = AsyncMock(return_value=None)
    args = ["source", "get", source_id, "-n", "nb_123"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client):
        result = runner.invoke(cli, args)

    assert result.exit_code == 1, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "NOT_FOUND"
        assert payload["source_id"] == source_id
    else:
        assert "Source not found" in result.output


def test_source_fulltext_json_renderer_emits_full_payload(
    runner: CliRunner,
    mock_auth,
) -> None:
    client = _client_resolving_source()
    client.sources.get_fulltext = AsyncMock(
        return_value=SourceFulltext(
            source_id="src_1",
            title="Source One",
            content="indexed content",
            _type_code=5,
            char_count=15,
        )
    )

    with _patched_source_client(client):
        result = runner.invoke(cli, ["source", "fulltext", "src_1", "-n", "nb_123", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_id"] == "src_1"
    assert payload["title"] == "Source One"
    assert payload["kind"] == "web_page"
    assert payload["content"] == "indexed content"
    assert payload["char_count"] == 15
    assert "_type_code" not in payload


def test_source_fulltext_json_output_file_writes_content_and_emits_metadata(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    content = "full content for disk"
    client = _client_resolving_source()
    client.sources.get_fulltext = AsyncMock(
        return_value=SourceFulltext(
            source_id="src_1",
            title="Source One",
            content=content,
            _type_code=5,
            char_count=len(content),
        )
    )

    with _patched_source_client(client):
        result = runner.invoke(
            cli,
            [
                "source",
                "fulltext",
                "src_1",
                "-n",
                "nb_123",
                "--json",
                "-o",
                str(output_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert output_file.read_text(encoding="utf-8") == content
    payload = json.loads(result.output)
    assert payload == {
        "path": str(output_file),
        "bytes": len(content.encode("utf-8")),
        "source_id": "src_1",
        "title": "Source One",
        "kind": "web_page",
    }


def test_source_guide_empty_text_renderer_uses_empty_state(
    runner: CliRunner,
    mock_auth,
) -> None:
    client = _client_resolving_source()
    client.sources.get_guide = AsyncMock(
        return_value={"summary": "  ", "keywords": ["", "  ", 7, None]}
    )

    with _patched_source_client(client):
        result = runner.invoke(cli, ["source", "guide", "src_1", "-n", "nb_123"])

    assert result.exit_code == 0, result.output
    assert "No guide available" in result.output
    assert "Keywords:" not in result.output


@pytest.mark.parametrize("json_output", [False, True])
def test_source_guide_populated_renderer_strips_keywords(
    runner: CliRunner,
    mock_auth,
    json_output: bool,
) -> None:
    client = _client_resolving_source()
    client.sources.get_guide = AsyncMock(
        return_value={"summary": "Summary text", "keywords": [" alpha ", "", "beta", "  "]}
    )
    args = ["source", "guide", "src_1", "-n", "nb_123"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client):
        result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["summary"] == "Summary text"
        assert payload["keywords"] == ["alpha", "beta"]
    else:
        assert "Summary text" in result.output
        assert "alpha, beta" in result.output


@pytest.mark.parametrize(
    ("is_fresh", "json_output", "expected_stale"),
    [
        (False, False, True),
        (True, False, False),
        (False, True, True),
        (True, True, False),
    ],
)
def test_source_stale_renderer_default_exits_zero_on_success(
    runner: CliRunner,
    mock_auth,
    is_fresh: bool,
    json_output: bool,
    expected_stale: bool,
) -> None:
    """Default policy: exit 0 on a successful freshness check regardless of verdict.

    The verdict is communicated through stdout text (or the JSON
    ``stale``/``fresh`` fields) — callers branch on that, not the exit
    code. See ``docs/cli-exit-codes.md`` and the ``--exit-on-stale``
    flag for the back-compat inverted predicate.
    """
    client = _client_resolving_source()
    client.sources.check_freshness = AsyncMock(return_value=is_fresh)
    args = ["source", "stale", "src_1", "-n", "nb_123"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client):
        result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["stale"] is expected_stale
        assert payload["fresh"] is is_fresh
    else:
        assert ("stale" if expected_stale else "fresh") in result.output.lower()


@pytest.mark.parametrize(
    ("is_fresh", "json_output", "expected_exit", "expected_stale"),
    [
        (False, False, 0, True),
        (True, False, 1, False),
        (False, True, 0, True),
        (True, True, 1, False),
    ],
)
def test_source_stale_renderer_exit_on_stale_flag_inverts_exit_codes(
    runner: CliRunner,
    mock_auth,
    is_fresh: bool,
    json_output: bool,
    expected_exit: int,
    expected_stale: bool,
) -> None:
    """``--exit-on-stale`` opts into back-compat inverted predicate semantics."""
    client = _client_resolving_source()
    client.sources.check_freshness = AsyncMock(return_value=is_fresh)
    args = ["source", "stale", "src_1", "-n", "nb_123", "--exit-on-stale"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client):
        result = runner.invoke(cli, args)

    assert result.exit_code == expected_exit, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["stale"] is expected_stale
        assert payload["fresh"] is is_fresh
    else:
        assert ("stale" if expected_stale else "fresh") in result.output.lower()
