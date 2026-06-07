"""CLI integration tests for the ``label`` command group.

These exercise the full CLI -> Client -> RPC path using VCR cassettes, mirroring
``test_share.py`` (the other pure-RPC, ``client.<api>``-backed group). They cover
the ``label`` happy paths (``list`` / ``sources`` / ``create`` / ``rename`` /
``emoji`` / ``add`` / ``remove`` / ``delete`` / ``generate``) plus the
``source list --label`` read filter.

.. note::

    **Cassettes pending recording.** The cassettes this module references do NOT
    exist yet — a maintainer must record them against a live account with
    ``NOTEBOOKLM_VCR_RECORD=1`` (see "Recording" below). Until then every test
    here is RED (skipped only if the whole cassette dir is absent — once *some*
    cassettes exist but the ``label_*`` ones do not, these fail with a
    can't-overwrite/cassette-not-found error). That is expected: the module is
    written so it goes GREEN the moment the cassettes land, at which point
    ``label`` also moves from ``COVERAGE_EXEMPT`` to ``GROUP_COVERAGE`` in
    ``tests/_guardrails/test_cli_vcr_coverage.py``.

RPC fan-out per command
-----------------------
``client.labels`` issues these RPCs (see ``src/notebooklm/_labels.py`` and
``src/notebooklm/rpc/types.py``):

* ``list``     -> one ``LIST_LABELS`` (``I3xc3c``) + one ``GET_NOTEBOOK``
  (``rLM1Ne``) for the member-title join.
* ``sources``  -> ``LIST_LABELS`` (the ``get``) + ``GET_NOTEBOOK`` (the
  membership->Source join), preceded by the ``<id|name>`` resolver's
  ``LIST_LABELS``.
* ``create``   -> ``LIST_LABELS`` (the id-diff snapshot) then ``CREATE_LABEL``
  (``agX4Bc``).
* ``rename`` / ``emoji`` -> the resolver's ``LIST_LABELS``, the ``update``
  preflight ``LIST_LABELS``, ``UPDATE_LABEL`` (``le8sX``), then a re-read
  ``LIST_LABELS``.
* ``add``      -> the resolver's ``LIST_LABELS``, source-id resolution
  (``GET_NOTEBOOK``), ``UPDATE_LABEL`` (variant ``add_sources``, one call per
  id), then the contract re-fetch ``LIST_LABELS``.
* ``remove``   -> the resolver's ``LIST_LABELS``, source-id resolution
  (``GET_NOTEBOOK``), ``UPDATE_LABEL`` (variant ``remove_sources``, one call per
  id), then the contract re-fetch ``LIST_LABELS``.
* ``delete``   -> the resolver's ``LIST_LABELS`` per ref, then ``DELETE_LABEL``
  (``GyzE7e``).
* ``generate`` -> ``CREATE_LABEL`` (multi-mode; ``agX4Bc`` echoes the full set).

``source list --label`` reuses the source-list pipeline plus the label resolver
+ ``labels.sources`` join, so its cassette holds ``LIST_LABELS`` + ``GET_NOTEBOOK``.

Each command resolves its notebook via a full UUID passed with ``-n`` so
``resolve_notebook_id`` skips the ``LIST_NOTEBOOKS`` preflight (mirrors the
``mock_context`` docstring in ``conftest.py``).

Re-record-safe assertions
-------------------------
Per ``tests/integration/cli_vcr/README.md`` the assertions stay in the allowed
vocabulary — Schema (``--json`` envelope shape), Invariants (id shape / non-empty
name / ``count >= 0``), and Input-echo (a mutation's ``notebook_id`` echoes the
placeholder the test passed). NO recorded response value or ``== N`` is pinned,
so the assertions survive a re-record against a different notebook.

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_label.py -m vcr
"""

import re

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import MUTATION_NOTEBOOK_ID, VCR_READONLY_SOURCE_ID
from .conftest import (
    SOURCE_LIST_SCHEMA,
    FieldSpec,
    assert_command_success,
    assert_json_envelope,
    notebooklm_vcr,
    parse_json_dict,
    skip_no_cassettes,
)

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# A full UUID passed with ``-n`` keeps ``resolve_notebook_id`` on its fast path so
# each cassette captures only the label RPC chain. ``MUTATION_NOTEBOOK_ID`` is
# present in ZERO cassettes on purpose, so a ``notebook_id`` that comes back equal
# to it proves the CLI threaded the *input* through (input-echo tier). The value
# is never matched against the recorded body (VCR matches on ``rpcids`` + shape).
VCR_LABEL_NOTEBOOK_ID = MUTATION_NOTEBOOK_ID

# Loose UUID shape check (8-4-4-4-12 hex), deliberately not anchored to a value —
# a re-record yields different ids that must still be UUID-shaped. Label ids are
# UUID-shaped like source ids (see ``_fixtures``/``test_sources``).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# --- Per-family ``--json`` envelope schemas --------------------------------
# Defined locally (like ``test_share.py``'s ``VCR_SHARE_EMAIL``) rather than in
# ``conftest.py`` — they are label-specific and only this module consumes them.
# Shape-only: value invariants (UUID-shaped id, non-empty name) are asserted by
# the tests themselves so the schema stays a pure structural contract.

# A single label member as serialized by ``_label_serialize`` (list join) and
# ``_label_payload`` (mutation result). The mutation payload carries
# ``source_ids`` only; the list payload also carries a ``sources`` join. Both are
# captured with ``optional``/``nullable`` so one schema validates either shape.
_LABEL_SOURCE_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "title": FieldSpec(str, nullable=True),
}

# ``label list --json`` envelope: ``{"labels": [...], "count": N}`` (the list
# service sets no ``envelope_extras``, so there is no top-level ``notebook_id``).
_LABEL_LIST_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "name": FieldSpec(str),
    "emoji": FieldSpec(str, nullable=True),
    "source_ids": FieldSpec(list),
    "sources": FieldSpec(list, item_schema=_LABEL_SOURCE_ITEM_SCHEMA),
}

LABEL_LIST_SCHEMA: dict[str, FieldSpec] = {
    "labels": FieldSpec(list, item_schema=_LABEL_LIST_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

# ``label create/rename/emoji --json`` envelope: a single label payload prefixed
# with the echoed ``notebook_id`` (``{"notebook_id": ..., **_label_payload}``).
LABEL_MUTATION_SCHEMA: dict[str, FieldSpec] = {
    "notebook_id": FieldSpec(str),
    "id": FieldSpec(str),
    "name": FieldSpec(str),
    "emoji": FieldSpec(str, nullable=True),
    "source_ids": FieldSpec(list),
}

# ``label add --json`` adds the echoed ``added_source_ids`` on top of the
# mutation payload.
LABEL_ADD_SCHEMA: dict[str, FieldSpec] = {
    **LABEL_MUTATION_SCHEMA,
    "added_source_ids": FieldSpec(list),
}

# ``label remove --json`` mirrors ``add`` but echoes ``removed_source_ids``.
LABEL_REMOVE_SCHEMA: dict[str, FieldSpec] = {
    **LABEL_MUTATION_SCHEMA,
    "removed_source_ids": FieldSpec(list),
}

# ``label sources --json`` envelope.
_LABEL_SOURCES_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "title": FieldSpec(str, nullable=True),
    "url": FieldSpec(str, nullable=True),
}

LABEL_SOURCES_SCHEMA: dict[str, FieldSpec] = {
    "notebook_id": FieldSpec(str),
    "label_id": FieldSpec(str),
    "sources": FieldSpec(list, item_schema=_LABEL_SOURCES_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

# ``label generate --json`` serializes each label via ``_label_payload`` (the
# mutation shape: id/name/emoji/source_ids) — NOT the list-join shape, so it
# carries NO ``sources`` join. Its item schema is therefore the list-item schema
# with ``sources`` made ``optional`` (present on ``label list``, absent here).
_LABEL_GENERATE_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "name": FieldSpec(str),
    "emoji": FieldSpec(str, nullable=True),
    "source_ids": FieldSpec(list),
    "sources": FieldSpec(list, optional=True, item_schema=_LABEL_SOURCE_ITEM_SCHEMA),
}

# ``label generate --json`` envelope: the post-op label set + echoed scope.
LABEL_GENERATE_SCHEMA: dict[str, FieldSpec] = {
    "notebook_id": FieldSpec(str),
    "scope": FieldSpec(str),
    "labels": FieldSpec(list, item_schema=_LABEL_GENERATE_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

# ``label delete --json`` (confirmed-mutation envelope).
LABEL_DELETE_SCHEMA: dict[str, FieldSpec] = {
    "notebook_id": FieldSpec(str),
    "label_ids": FieldSpec(list),
    "deleted": FieldSpec(bool),
}


class TestLabelListCommand:
    """Test ``notebooklm label list``."""

    @notebooklm_vcr.use_cassette("label_list.yaml", allow_playback_repeats=True)
    def test_label_list(self, runner, mock_auth_for_vcr):
        """``label list`` renders the label set without crashing."""
        result = runner.invoke(cli, ["label", "list", "-n", VCR_LABEL_NOTEBOOK_ID])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_list.yaml", allow_playback_repeats=True)
    def test_label_list_json_schema(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``label list --json`` matches the schema and invariants."""
        result = runner.invoke(cli, ["label", "list", "-n", VCR_LABEL_NOTEBOOK_ID, "--json"])
        assert_command_success(result, allow_no_context=False)

        # Tier 1 — envelope shape.
        assert_json_envelope(result, schema=LABEL_LIST_SCHEMA)

        data = parse_json_dict(result.output)
        labels = data["labels"]
        # Tier 2 — value invariants, never pinned to recorded values.
        assert data["count"] == len(labels), "count must match the array length"
        for label in labels:
            assert _UUID_RE.match(label.get("id", "")), (
                f"label id not UUID-shaped: {label.get('id')!r}"
            )
            assert label.get("name", "").strip(), "label name must be non-blank"
            # ``source_ids`` and the joined ``sources`` must agree in length.
            assert len(label["sources"]) <= len(label["source_ids"]), (
                "joined sources cannot exceed the membership id list"
            )


class TestLabelSourcesCommand:
    """Test ``notebooklm label sources <ref>`` (group -> sources)."""

    @notebooklm_vcr.use_cassette("label_sources.yaml", allow_playback_repeats=True)
    def test_label_sources(self, runner, mock_auth_for_vcr):
        """``label sources`` expands a label to its source objects."""
        result = runner.invoke(
            cli, ["label", "sources", VCR_READONLY_SOURCE_ID, "-n", VCR_LABEL_NOTEBOOK_ID]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_sources.yaml", allow_playback_repeats=True)
    def test_label_sources_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label sources --json`` matches the schema; echoes the ids."""
        result = runner.invoke(
            cli,
            ["label", "sources", VCR_READONLY_SOURCE_ID, "-n", VCR_LABEL_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_SOURCES_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: the notebook id threads through from the input.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert data["count"] == len(data["sources"]), "count must match the array length"


class TestLabelCreateCommand:
    """Test ``notebooklm label create <name>``."""

    @notebooklm_vcr.use_cassette("label_create.yaml", allow_playback_repeats=True)
    def test_label_create(self, runner, mock_auth_for_vcr):
        """``label create`` runs LIST_LABELS (diff) + CREATE_LABEL."""
        result = runner.invoke(
            cli,
            ["label", "create", "VCR Test Label", "-n", VCR_LABEL_NOTEBOOK_ID, "--emoji", "📄"],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_create.yaml", allow_playback_repeats=True)
    def test_label_create_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2 + 5: ``label create --json`` schema, invariants, input-echo."""
        result = runner.invoke(
            cli,
            [
                "label",
                "create",
                "VCR Test Label",
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--emoji",
                "📄",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        # Tier 2 — the created label's id is UUID-shaped and its name non-blank.
        assert _UUID_RE.match(data.get("id", "")), f"label id not UUID-shaped: {data.get('id')!r}"
        assert data.get("name", "").strip(), "created label name must be non-blank"


class TestLabelRenameCommand:
    """Test ``notebooklm label rename <ref> <new_name>``."""

    @notebooklm_vcr.use_cassette("label_rename.yaml", allow_playback_repeats=True)
    def test_label_rename(self, runner, mock_auth_for_vcr):
        """``label rename`` runs resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli,
            ["label", "rename", VCR_READONLY_SOURCE_ID, "Renamed", "-n", VCR_LABEL_NOTEBOOK_ID],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_rename.yaml", allow_playback_repeats=True)
    def test_label_rename_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label rename --json`` matches the schema; echoes the id."""
        result = runner.invoke(
            cli,
            [
                "label",
                "rename",
                VCR_READONLY_SOURCE_ID,
                "Renamed",
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID


class TestLabelEmojiCommand:
    """Test ``notebooklm label emoji <ref> <emoji>``."""

    @notebooklm_vcr.use_cassette("label_emoji.yaml", allow_playback_repeats=True)
    def test_label_emoji(self, runner, mock_auth_for_vcr):
        """``label emoji`` runs resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli,
            ["label", "emoji", VCR_READONLY_SOURCE_ID, "🚀", "-n", VCR_LABEL_NOTEBOOK_ID],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_emoji.yaml", allow_playback_repeats=True)
    def test_label_emoji_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label emoji --json`` matches the schema; echoes the id."""
        result = runner.invoke(
            cli,
            ["label", "emoji", VCR_READONLY_SOURCE_ID, "🚀", "-n", VCR_LABEL_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID


class TestLabelAddCommand:
    """Test ``notebooklm label add <ref> <source_ids...>``."""

    @notebooklm_vcr.use_cassette("label_add.yaml", allow_playback_repeats=True)
    def test_label_add(self, runner, mock_auth_for_vcr):
        """``label add`` runs resolve + source-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli,
            [
                "label",
                "add",
                VCR_READONLY_SOURCE_ID,
                VCR_READONLY_SOURCE_ID,
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
            ],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_add.yaml", allow_playback_repeats=True)
    def test_label_add_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label add --json`` matches the schema; echoes the ids."""
        result = runner.invoke(
            cli,
            [
                "label",
                "add",
                VCR_READONLY_SOURCE_ID,
                VCR_READONLY_SOURCE_ID,
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_ADD_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: the resolved source ids round-trip into the result.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert isinstance(data["added_source_ids"], list)


class TestLabelRemoveCommand:
    """Test ``notebooklm label remove <ref> <source_ids...>`` (un-assign).

    The inverse of ``add`` — un-assigns the source from the label only (the
    source survives in the notebook). No ``--yes`` gate (non-destructive).
    """

    @notebooklm_vcr.use_cassette("label_remove.yaml", allow_playback_repeats=True)
    def test_label_remove(self, runner, mock_auth_for_vcr):
        """``label remove`` runs resolve + source-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli,
            [
                "label",
                "remove",
                VCR_READONLY_SOURCE_ID,
                VCR_READONLY_SOURCE_ID,
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
            ],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_remove.yaml", allow_playback_repeats=True)
    def test_label_remove_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label remove --json`` matches the schema; echoes the ids."""
        result = runner.invoke(
            cli,
            [
                "label",
                "remove",
                VCR_READONLY_SOURCE_ID,
                VCR_READONLY_SOURCE_ID,
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_REMOVE_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: the resolved source ids round-trip into the result.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert isinstance(data["removed_source_ids"], list)


class TestLabelDeleteCommand:
    """Test ``notebooklm label delete <refs...>``."""

    @notebooklm_vcr.use_cassette("label_delete.yaml", allow_playback_repeats=True)
    def test_label_delete(self, runner, mock_auth_for_vcr):
        """``label delete --yes`` runs resolve + DELETE_LABEL."""
        result = runner.invoke(
            cli,
            ["label", "delete", VCR_READONLY_SOURCE_ID, "-n", VCR_LABEL_NOTEBOOK_ID, "--yes"],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_delete.yaml", allow_playback_repeats=True)
    def test_label_delete_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label delete --yes --json`` matches the schema; echoes ids."""
        result = runner.invoke(
            cli,
            [
                "label",
                "delete",
                VCR_READONLY_SOURCE_ID,
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--yes",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_DELETE_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert data["deleted"] is True
        assert isinstance(data["label_ids"], list)


class TestLabelGenerateCommand:
    """Test ``notebooklm label generate`` (AI auto-label, safe scope)."""

    @notebooklm_vcr.use_cassette("label_generate.yaml", allow_playback_repeats=True)
    def test_label_generate(self, runner, mock_auth_for_vcr):
        """``label generate`` (default ``--scope unlabeled``) runs CREATE_LABEL."""
        result = runner.invoke(cli, ["label", "generate", "-n", VCR_LABEL_NOTEBOOK_ID])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("label_generate.yaml", allow_playback_repeats=True)
    def test_label_generate_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``label generate --json`` matches the schema; echoes scope."""
        result = runner.invoke(cli, ["label", "generate", "-n", VCR_LABEL_NOTEBOOK_ID, "--json"])
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=LABEL_GENERATE_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: notebook id + the safe scope thread through.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert data["scope"] == "unlabeled"
        assert data["count"] == len(data["labels"]), "count must match the array length"


class TestSourceListLabelFilter:
    """Test ``notebooklm source list --label <ref>`` (read filter via labels)."""

    @notebooklm_vcr.use_cassette("source_list_label.yaml", allow_playback_repeats=True)
    def test_source_list_label_filter(self, runner, mock_auth_for_vcr):
        """``source list --label`` restricts the listing to a label's sources."""
        result = runner.invoke(
            cli,
            ["source", "list", "-n", VCR_LABEL_NOTEBOOK_ID, "--label", VCR_READONLY_SOURCE_ID],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("source_list_label.yaml", allow_playback_repeats=True)
    def test_source_list_label_filter_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``source list --label --json`` matches the source-list schema."""
        result = runner.invoke(
            cli,
            [
                "source",
                "list",
                "-n",
                VCR_LABEL_NOTEBOOK_ID,
                "--label",
                VCR_READONLY_SOURCE_ID,
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        # ``source list --label`` reuses the source-list envelope (the join just
        # narrows which sources appear), so the shared SOURCE_LIST_SCHEMA applies.
        assert_json_envelope(result, schema=SOURCE_LIST_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo.
        assert data["notebook_id"] == VCR_LABEL_NOTEBOOK_ID
        assert data["count"] == len(data["sources"]), "count must match the array length"
