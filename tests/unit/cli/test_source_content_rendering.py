"""CliRunner coverage for source-content command rendering branches."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Source, SourceFulltext

from .conftest import create_mock_client, inject_client, source_guide


@contextmanager
def _patched_source_client(client) -> Iterator[dict]:
    """Yield the ``ctx.obj`` payload that injects ``client`` into the command.

    Callers thread the yielded value into ``runner.invoke(cli, args, obj=obj)``.
    The ``fetch_tokens`` auth seam is still patched here.
    """
    with patch.object(
        auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
    ) as fetch_tokens:
        fetch_tokens.return_value = ("csrf", "session")
        yield inject_client(client)


def _client_resolving_source(source_id: str = "src_1", title: str = "Source One"):
    client = create_mock_client()
    client.sources.list = AsyncMock(return_value=[Source(id=source_id, title=title)])
    return client


def _client_with_fulltext(content: str, *, type_code: int | None = None):
    client = _client_resolving_source()
    payload = {
        "source_id": "src_1",
        "title": "Source One",
        "content": content,
        "char_count": len(content),
    }
    if type_code is not None:
        payload["_type_code"] = type_code
    client.sources.get_fulltext = AsyncMock(return_value=SourceFulltext(**payload))
    return client


@pytest.mark.parametrize("json_output", [False, True])
def test_source_get_not_found_renderer_exits_one(
    runner: CliRunner,
    mock_auth,
    json_output: bool,
) -> None:
    source_id = "11111111-2222-3333-4444-555555555555"
    client = create_mock_client()
    client.sources.get_or_none = AsyncMock(return_value=None)
    args = ["source", "get", source_id, "-n", "nb_123"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client) as obj:
        result = runner.invoke(cli, args, obj=obj)

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

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli, ["source", "fulltext", "src_1", "-n", "nb_123", "--json"], obj=obj
        )

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

    with _patched_source_client(client) as obj:
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
            obj=obj,
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


def test_source_fulltext_output_file_auto_renames_existing_file(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source [draft].txt"
    renamed_file = tmp_path / "source [draft] (2).txt"
    output_file.write_text("existing content", encoding="utf-8")
    content = "replacement content"
    client = _client_with_fulltext(content)

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli,
            ["source", "fulltext", "src_1", "-n", "nb_123", "-o", str(output_file)],
            obj=obj,
        )

    assert result.exit_code == 0, result.output
    assert output_file.read_text(encoding="utf-8") == "existing content"
    assert renamed_file.read_text(encoding="utf-8") == content
    assert f"Saved {len(content)} chars to" in result.output
    assert str(renamed_file) in result.output


def test_source_fulltext_json_output_file_auto_renames_and_emits_actual_path(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    renamed_file = tmp_path / "source (2).txt"
    output_file.write_text("existing content", encoding="utf-8")
    content = "replacement content"
    client = _client_with_fulltext(content, type_code=5)

    with _patched_source_client(client) as obj:
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
            obj=obj,
        )

    assert result.exit_code == 0, result.output
    assert output_file.read_text(encoding="utf-8") == "existing content"
    assert renamed_file.read_text(encoding="utf-8") == content
    payload = json.loads(result.output)
    assert payload["path"] == str(renamed_file)
    assert payload["bytes"] == len(content.encode("utf-8"))


def test_source_fulltext_output_file_no_clobber_rejects_existing_file(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    output_file.write_text("existing content", encoding="utf-8")
    client = _client_with_fulltext("replacement content")

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli,
            [
                "source",
                "fulltext",
                "src_1",
                "-n",
                "nb_123",
                "-o",
                str(output_file),
                "--no-clobber",
            ],
            obj=obj,
        )

    assert result.exit_code == 1, result.output
    assert output_file.read_text(encoding="utf-8") == "existing content"
    client.sources.get_fulltext.assert_not_awaited()
    assert f"File exists: {output_file}" in result.output
    assert "Use --force to overwrite" in result.output


def test_source_fulltext_json_output_file_no_clobber_rejects_existing_file(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    output_file.write_text("existing content", encoding="utf-8")
    client = _client_with_fulltext("replacement content")

    with _patched_source_client(client) as obj:
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
                "--no-clobber",
            ],
            obj=obj,
        )

    assert result.exit_code == 1, result.output
    assert output_file.read_text(encoding="utf-8") == "existing content"
    client.sources.get_fulltext.assert_not_awaited()
    payload = json.loads(result.output)
    assert payload == {
        "error": True,
        "code": "FILE_EXISTS",
        "message": f"File exists: {output_file}",
        "path": str(output_file),
        "suggestion": "Use --force to overwrite or choose a different path",
    }


def test_source_fulltext_output_rejects_directory_before_fetching(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_dir = tmp_path / "fulltext"
    output_dir.mkdir()
    client = _client_with_fulltext("content that should not be fetched")

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli,
            ["source", "fulltext", "src_1", "-n", "nb_123", "-o", str(output_dir)],
            obj=obj,
        )

    assert result.exit_code == 2, result.output
    assert f"Output path is a directory: {output_dir}" in result.output
    assert not result.output.strip().startswith("{")
    client.sources.list.assert_not_awaited()
    client.sources.get_fulltext.assert_not_awaited()


def test_source_fulltext_json_output_rejects_directory_before_fetching(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_dir = tmp_path / "fulltext"
    output_dir.mkdir()
    client = _client_with_fulltext("content that should not be fetched")

    with _patched_source_client(client) as obj:
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
                str(output_dir),
            ],
            obj=obj,
        )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload == {
        "error": True,
        "code": "VALIDATION_ERROR",
        "message": f"Output path is a directory: {output_dir}",
    }
    client.sources.list.assert_not_awaited()
    client.sources.get_fulltext.assert_not_awaited()


def test_source_fulltext_output_file_force_overwrites_existing_file(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    output_file.write_text("existing content", encoding="utf-8")
    content = "replacement content"
    client = _client_with_fulltext(content)

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli,
            [
                "source",
                "fulltext",
                "src_1",
                "-n",
                "nb_123",
                "-o",
                str(output_file),
                "--force",
            ],
            obj=obj,
        )

    assert result.exit_code == 0, result.output
    assert output_file.read_text(encoding="utf-8") == content
    assert not (tmp_path / "source (2).txt").exists()


def test_source_fulltext_output_rejects_force_and_no_clobber_json(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    client = _client_with_fulltext("replacement content")

    with _patched_source_client(client) as obj:
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
                "--force",
                "--no-clobber",
            ],
            obj=obj,
        )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload == {
        "error": True,
        "code": "VALIDATION_ERROR",
        "message": "Cannot specify both --force and --no-clobber",
    }


def test_source_fulltext_output_rejects_force_and_no_clobber_text(
    runner: CliRunner,
    mock_auth,
    tmp_path,
) -> None:
    output_file = tmp_path / "source.txt"
    client = _client_with_fulltext("replacement content")

    with _patched_source_client(client) as obj:
        result = runner.invoke(
            cli,
            [
                "source",
                "fulltext",
                "src_1",
                "-n",
                "nb_123",
                "-o",
                str(output_file),
                "--force",
                "--no-clobber",
            ],
            obj=obj,
        )

    assert result.exit_code == 2, result.output
    assert "Cannot specify both --force and --no-clobber" in result.output
    assert not result.output.strip().startswith("{")
    client.sources.get_fulltext.assert_not_awaited()


def test_source_guide_empty_text_renderer_uses_empty_state(
    runner: CliRunner,
    mock_auth,
) -> None:
    client = _client_resolving_source()
    client.sources.get_guide = AsyncMock(
        return_value=source_guide({"summary": "  ", "keywords": ["", "  ", 7, None]})
    )

    with _patched_source_client(client) as obj:
        result = runner.invoke(cli, ["source", "guide", "src_1", "-n", "nb_123"], obj=obj)

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
        return_value=source_guide(
            {"summary": "Summary text", "keywords": [" alpha ", "", "beta", "  "]}
        )
    )
    args = ["source", "guide", "src_1", "-n", "nb_123"]
    if json_output:
        args.append("--json")

    with _patched_source_client(client) as obj:
        result = runner.invoke(cli, args, obj=obj)

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

    with _patched_source_client(client) as obj:
        result = runner.invoke(cli, args, obj=obj)

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

    with _patched_source_client(client) as obj:
        result = runner.invoke(cli, args, obj=obj)

    assert result.exit_code == expected_exit, result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["stale"] is expected_stale
        assert payload["fresh"] is is_fresh
    else:
        assert ("stale" if expected_stale else "fresh") in result.output.lower()
