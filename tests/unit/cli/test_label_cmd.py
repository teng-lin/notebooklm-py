"""Tests for the ``label`` CLI command group.

Drives the thin Click handlers in ``cli/label_cmd.py`` via ``CliRunner``,
injecting a mock client through ``ctx.obj`` (``inject_client``), which seeds
``ctx.obj["client_factory"]`` so the command builds the mock instead of a real
``NotebookLMClient``. Covers ``list`` (with the member-id + title join in
``--json``), ``sources`` (delegates to ``client.labels.sources()``), the CRUD
verbs (``create`` / ``rename`` / ``emoji`` / ``add`` / ``remove`` / ``delete``),
and ``generate`` (the ``--yes/-y`` gate on ``--scope all``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Label, Source

from .conftest import create_mock_client, inject_client


def _client_with_labels(*, labels=None, sources=None):
    client = create_mock_client()
    client.labels = AsyncMock()
    # ``notebooks.list`` already matches ``nb_*`` via the conftest stub; pin the
    # label/source lists per test.
    client.labels.list = AsyncMock(return_value=labels or [])
    client.sources.list = AsyncMock(return_value=sources or [])
    return client


def _run(runner, mock_auth, mock_fetch_tokens, argv, client):
    return runner.invoke(cli, argv, obj=inject_client(client), catch_exceptions=False)


# ---------------------------------------------------------------------------
# label list
# ---------------------------------------------------------------------------


def test_label_list_json_envelope_with_members_and_titles(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    labels = [
        Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1", "s2"]),
    ]
    sources = [Source(id="s1", title="First"), Source(id="s2", title="Second")]
    client = _client_with_labels(labels=labels, sources=sources)

    result = _run(
        runner, mock_auth, mock_fetch_tokens, ["label", "list", "-n", "nb_123", "--json"], client
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    label = payload["labels"][0]
    assert label["id"] == "lblaaa111"
    assert label["source_ids"] == ["s1", "s2"]
    assert label["sources"] == [
        {"id": "s1", "title": "First"},
        {"id": "s2", "title": "Second"},
    ]


def test_label_list_human_mode(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1"])]
    sources = [Source(id="s1", title="First")]
    client = _client_with_labels(labels=labels, sources=sources)

    result = _run(runner, mock_auth, mock_fetch_tokens, ["label", "list", "-n", "nb_123"], client)

    assert result.exit_code == 0, result.output
    assert "Papers" in result.output


# ---------------------------------------------------------------------------
# label sources
# ---------------------------------------------------------------------------


def test_label_sources_delegates_to_api(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", source_ids=["s1"])]
    client = _client_with_labels(labels=labels)
    client.labels.sources = AsyncMock(return_value=[Source(id="s1", title="First")])

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "sources", "lblaaa111", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.sources.assert_awaited_once_with("nb_123", "lblaaa111")
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["sources"][0]["id"] == "s1"


def test_label_sources_resolves_by_name(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", source_ids=["s1"])]
    client = _client_with_labels(labels=labels)
    client.labels.sources = AsyncMock(return_value=[Source(id="s1", title="First")])

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "sources", "Papers", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    # The name "Papers" resolves to lblaaa111 before delegating.
    client.labels.sources.assert_awaited_once_with("nb_123", "lblaaa111")


def test_label_sources_near_miss_text_mode_shows_did_you_mean(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    """A label name mistyped with a hyphen for an em-dash gets a text-mode hint (#1787)."""
    labels = [Label(id="lblaaa111", name="Q3 — Papers", source_ids=["s1"])]
    client = _client_with_labels(labels=labels)

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "sources", "Q3 - Papers", "-n", "nb_123"],
        client,
    )

    assert result.exit_code == 1, result.output
    assert "Did you mean:" in result.output
    assert "Q3 — Papers" in result.output
    assert "lblaaa111" in result.output


def test_label_sources_near_miss_json_mode_carries_candidates(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    labels = [Label(id="lblaaa111", name="Q3 — Papers", source_ids=["s1"])]
    client = _client_with_labels(labels=labels)

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "sources", "Q3 - Papers", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "NOT_FOUND"
    assert payload["candidates"] == [{"id": "lblaaa111", "title": "Q3 — Papers"}]


# ---------------------------------------------------------------------------
# label create
# ---------------------------------------------------------------------------


def test_label_create_json(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_labels()
    client.labels.create = AsyncMock(return_value=Label(id="lblnew999", name="Topics", emoji="🧠"))

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "create", "Topics", "-n", "nb_123", "--emoji", "🧠", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.create.assert_awaited_once_with("nb_123", "Topics", "🧠")
    payload = json.loads(result.stdout)
    assert payload["id"] == "lblnew999"
    assert payload["name"] == "Topics"


# ---------------------------------------------------------------------------
# label rename / emoji
# ---------------------------------------------------------------------------


def test_label_rename_json(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", emoji="📄")]
    client = _client_with_labels(labels=labels)
    client.labels.rename = AsyncMock(
        return_value=Label(id="lblaaa111", name="Articles", emoji="📄")
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "rename", "lblaaa111", "Articles", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.rename.assert_awaited_once_with("nb_123", "lblaaa111", "Articles")
    payload = json.loads(result.stdout)
    assert payload["name"] == "Articles"


def test_label_emoji_json(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", emoji="📄")]
    client = _client_with_labels(labels=labels)
    client.labels.set_emoji = AsyncMock(
        return_value=Label(id="lblaaa111", name="Papers", emoji="🔬")
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "emoji", "lblaaa111", "🔬", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.set_emoji.assert_awaited_once_with("nb_123", "lblaaa111", "🔬")
    payload = json.loads(result.stdout)
    assert payload["emoji"] == "🔬"


# ---------------------------------------------------------------------------
# label add (resolve_source_ids)
# ---------------------------------------------------------------------------


def test_label_add_resolves_source_ids(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", source_ids=[])]
    client = _client_with_labels(labels=labels, sources=[Source(id="src_1", title="Source One")])
    client.labels.add_sources = AsyncMock(
        return_value=Label(id="lblaaa111", name="Papers", source_ids=["src_1"])
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "add", "lblaaa111", "src_1", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    # resolve_source_ids expands src_1 (conftest source list has src_1).
    client.labels.add_sources.assert_awaited_once_with("nb_123", "lblaaa111", ["src_1"])


# ---------------------------------------------------------------------------
# label remove (inverse of add; NO --yes gate — un-assign is non-destructive)
# ---------------------------------------------------------------------------


def test_label_remove_resolves_source_ids(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers", source_ids=["src_1"])]
    client = _client_with_labels(labels=labels, sources=[Source(id="src_1", title="Source One")])
    client.labels.remove_sources = AsyncMock(
        return_value=Label(id="lblaaa111", name="Papers", source_ids=[])
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "remove", "lblaaa111", "src_1", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    # No confirmation prompt — remove runs straight through.
    client.labels.remove_sources.assert_awaited_once_with("nb_123", "lblaaa111", ["src_1"])
    payload = json.loads(result.stdout)
    assert payload["id"] == "lblaaa111"
    assert payload["removed_source_ids"] == ["src_1"]


def test_label_remove_not_found_routes_through_envelope(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    from notebooklm.exceptions import LabelNotFoundError

    labels = [Label(id="lblaaa111", name="Papers", source_ids=["src_1"])]
    client = _client_with_labels(labels=labels, sources=[Source(id="src_1", title="Source One")])
    client.labels.remove_sources = AsyncMock(
        side_effect=LabelNotFoundError("lblaaa111", method_id="le8sX")
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "remove", "lblaaa111", "src_1", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# label delete (--yes gate)
# ---------------------------------------------------------------------------


def test_label_delete_json_requires_yes(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers")]
    client = _client_with_labels(labels=labels)
    client.labels.delete = AsyncMock(return_value=None)

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "delete", "lblaaa111", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 1, result.output
    client.labels.delete.assert_not_called()
    payload = json.loads(result.stdout)
    assert payload["error"] is True


def test_label_delete_json_with_yes(runner, mock_auth, mock_fetch_tokens) -> None:
    labels = [Label(id="lblaaa111", name="Papers")]
    client = _client_with_labels(labels=labels)
    client.labels.delete = AsyncMock(return_value=None)

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "delete", "lblaaa111", "-n", "nb_123", "--yes", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.delete.assert_awaited_once()
    payload = json.loads(result.stdout)
    assert payload["deleted"] is True


# ---------------------------------------------------------------------------
# label generate (--scope all --yes gate)
# ---------------------------------------------------------------------------


def test_label_generate_default_scope_unlabeled(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_labels()
    client.labels.generate = AsyncMock(return_value=[Label(id="lblaaa111", name="Auto")])

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "generate", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.generate.assert_awaited_once_with("nb_123", scope="unlabeled")
    payload = json.loads(result.stdout)
    assert payload["count"] == 1


def test_label_generate_scope_all_requires_yes_json(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_labels()
    client.labels.generate = AsyncMock(return_value=[])

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "generate", "-n", "nb_123", "--scope", "all", "--json"],
        client,
    )

    assert result.exit_code == 1, result.output
    client.labels.generate.assert_not_called()
    payload = json.loads(result.stdout)
    assert payload["error"] is True


def test_label_generate_scope_all_with_yes(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_labels()
    client.labels.generate = AsyncMock(return_value=[Label(id="lblaaa111", name="Auto")])

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "generate", "-n", "nb_123", "--scope", "all", "--yes", "--json"],
        client,
    )

    assert result.exit_code == 0, result.output
    client.labels.generate.assert_awaited_once_with("nb_123", scope="all")


# ---------------------------------------------------------------------------
# error routing
# ---------------------------------------------------------------------------


def test_label_sources_not_found_routes_through_envelope(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    from notebooklm.exceptions import LabelNotFoundError

    labels = [Label(id="lblaaa111", name="Papers")]
    client = _client_with_labels(labels=labels)
    client.labels.sources = AsyncMock(
        side_effect=LabelNotFoundError("lblaaa111", method_id="I3xc3c")
    )

    result = _run(
        runner,
        mock_auth,
        mock_fetch_tokens,
        ["label", "sources", "lblaaa111", "-n", "nb_123", "--json"],
        client,
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "subcmd",
    ["list", "sources", "generate", "create", "rename", "emoji", "add", "remove", "delete"],
)
def test_label_subcommands_exist(subcmd) -> None:
    import click

    label_group = cli.get_command(click.Context(cli), "label")
    assert label_group is not None
    assert subcmd in label_group.list_commands(click.Context(label_group))
