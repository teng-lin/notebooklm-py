"""CLI integration tests for the ``collection`` command group.

These exercise the full CLI -> Client -> RPC path using VCR cassettes, mirroring
``test_label.py`` (the notebook-scoped sibling). They cover the ``collection``
happy paths (``list`` / ``notebooks`` / ``create`` / ``rename`` / ``add`` /
``remove`` / ``delete``).

Collections are **account-level** (see ``docs/architecture.md``): every command
takes no ``-n/--notebook`` option, and ``resolve_collection_id`` disables full-id
passthrough (a UUID-shaped *name* is not blindly accepted as an id) — unlike
notebook refs, a collection ``<ref>`` argument MUST match a real entry in the
recorded ``LIST_LABELS`` response. ``COLLECTION_ID`` in ``_fixtures.py`` is
therefore the REAL id of a persistent live collection ("VCR Test Collection")
the cassettes were recorded against, not a decorative placeholder (mirrors
``PLACEHOLDER_SOURCE_ID``'s "VCR Test Label", which is the same kind of real
recorded id). ``COLLECTION_MEMBER_NOTEBOOK_ID`` is a real notebook added to that
collection before recording, so ``add``/``remove`` exercise a genuine membership
mutation; notebook-ref resolution *does* have full-id passthrough, so a
mismatched value would still resolve, but using the real one avoids relying on
that at recording time. ``delete`` is destructive, so its two test methods each
target their OWN throwaway collection (``COLLECTION_DELETE_ID`` /
``COLLECTION_DELETE_ID_JSON``) instead of reusing ``COLLECTION_ID``.

RPC fan-out per command
------------------------
``client.collections`` issues these RPCs (see ``src/notebooklm/_collections.py``
and ``src/notebooklm/rpc/types.py``); every collection RPC reuses the label
method ids with a type-3 discriminator and ``source_path="/"`` (account-level):

* ``list``      -> one ``LIST_LABELS`` (``I3xc3c``).
* ``notebooks`` -> the resolver's ``LIST_LABELS``, then ``execute_collection_notebooks``
  -> one more ``LIST_LABELS`` (``get()``'s internal ``list()``) + one
  ``LIST_NOTEBOOKS`` (the membership -> Notebook join).
* ``create``    -> ``LIST_LABELS`` (the id-diff snapshot, BEFORE), ``CREATE_LABEL``
  (``agX4Bc``), then a second ``LIST_LABELS`` (the id-diff re-list, AFTER) — unlike
  ``label create``, this does NOT parse the create echo (the create-response shape
  was never captured), so it genuinely needs two *sequential, distinct-content*
  ``LIST_LABELS`` episodes. This cassette does NOT use
  ``allow_playback_repeats=True``: vcrpy's default (repeats disabled) plays
  identical-shape recorded episodes back in recorded order, which is what lets
  the second call see the post-create state instead of replaying the first
  (baseline) episode forever.
* ``rename``    -> the resolver's ``LIST_LABELS``, ``rename()``'s own preflight
  ``LIST_LABELS`` (``get_or_none``), ``UPDATE_LABEL``, then a post-write
  ``LIST_LABELS`` (``get()``).
* ``add``       -> the resolver's ``LIST_LABELS``, ``_resolve_notebook_ids``'s
  ``LIST_NOTEBOOKS``, ``UPDATE_LABEL`` (variant ``add_notebooks``, one call per
  id), then the ADR-0019 re-fetch ``LIST_LABELS`` (``get_or_none``).
* ``remove``    -> same shape as ``add``, variant ``remove_notebooks``.
* ``delete``    -> the batch resolver's ``LIST_LABELS`` (once, regardless of ref
  count), then ``DELETE_LABEL`` (``GyzE7e``).

Re-record-safe assertions
--------------------------
Per ``tests/integration/cli_vcr/README.md`` the assertions stay in the allowed
vocabulary — Schema (``--json`` envelope shape) and Invariants (id shape /
non-empty name / ``count >= 0``). NO recorded response value or ``== N`` is
pinned (except the fixed ``COLLECTION_ID``/``COLLECTION_MEMBER_NOTEBOOK_ID``
inputs echoed back verbatim, which is the input-echo tier), so the assertions
survive a re-record against a freshly recreated "VCR Test Collection".

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_collection.py -m vcr
"""

import re

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import (
    COLLECTION_DELETE_ID,
    COLLECTION_DELETE_ID_JSON,
    COLLECTION_ID,
    COLLECTION_MEMBER_NOTEBOOK_ID,
)
from .conftest import (
    FieldSpec,
    assert_command_success,
    assert_json_envelope,
    notebooklm_vcr,
    parse_json_dict,
    skip_no_cassettes,
)

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Loose UUID shape check (8-4-4-4-12 hex), deliberately not anchored to a value —
# a re-record yields different ids that must still be UUID-shaped. Collection
# ids are UUID-shaped like notebook/source/label ids.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# --- Per-family ``--json`` envelope schemas --------------------------------
# Defined locally (like ``test_label.py``'s label schemas) rather than in
# ``conftest.py`` — they are collection-specific and only this module consumes
# them. Shape-only: value invariants (UUID-shaped id, non-empty name) are
# asserted by the tests themselves so the schema stays a pure structural
# contract.

# ``collection list --json`` item, per ``collection_cmd.py``'s ``ListSpec``
# serializer: ``{"index", "id", "name", "emoji", "notebook_count"}``.
_COLLECTION_LIST_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "index": FieldSpec(int),
    "id": FieldSpec(str),
    "name": FieldSpec(str),
    "emoji": FieldSpec(str, nullable=True),
    "notebook_count": FieldSpec(int),
}

COLLECTION_LIST_SCHEMA: dict[str, FieldSpec] = {
    "collections": FieldSpec(list, item_schema=_COLLECTION_LIST_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

# ``collection notebooks --json`` envelope.
_COLLECTION_NOTEBOOK_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "title": FieldSpec(str, nullable=True),
}

COLLECTION_NOTEBOOKS_SCHEMA: dict[str, FieldSpec] = {
    "collection_id": FieldSpec(str),
    "notebooks": FieldSpec(list, item_schema=_COLLECTION_NOTEBOOK_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

# ``collection create/rename --json`` envelope: ``_collection_payload()`` — no
# ``notebook_id``/``collection_id`` wrapper (unlike labels, collections carry no
# notebook parent to echo).
COLLECTION_MUTATION_SCHEMA: dict[str, FieldSpec] = {
    "id": FieldSpec(str),
    "name": FieldSpec(str),
    "emoji": FieldSpec(str, nullable=True),
    "notebook_ids": FieldSpec(list),
}

# ``collection add --json`` adds the echoed ``added_notebook_ids`` on top of the
# mutation payload.
COLLECTION_ADD_SCHEMA: dict[str, FieldSpec] = {
    **COLLECTION_MUTATION_SCHEMA,
    "added_notebook_ids": FieldSpec(list),
}

# ``collection remove --json`` mirrors ``add`` but echoes ``removed_notebook_ids``.
COLLECTION_REMOVE_SCHEMA: dict[str, FieldSpec] = {
    **COLLECTION_MUTATION_SCHEMA,
    "removed_notebook_ids": FieldSpec(list),
}

# ``collection delete --yes --json`` (confirmed-mutation envelope) — no
# ``notebook_id`` (account-level), unlike ``label delete``.
COLLECTION_DELETE_SCHEMA: dict[str, FieldSpec] = {
    "collection_ids": FieldSpec(list),
    "deleted": FieldSpec(bool),
}


class TestCollectionListCommand:
    """Test ``notebooklm collection list``."""

    @notebooklm_vcr.use_cassette("collection_list.yaml", allow_playback_repeats=True)
    def test_collection_list(self, runner, mock_auth_for_vcr):
        """``collection list`` renders the collection set without crashing."""
        result = runner.invoke(cli, ["collection", "list"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_list.yaml", allow_playback_repeats=True)
    def test_collection_list_json_schema(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection list --json`` matches the schema and invariants."""
        result = runner.invoke(cli, ["collection", "list", "--json"])
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_LIST_SCHEMA)

        data = parse_json_dict(result.output)
        collections = data["collections"]
        assert data["count"] == len(collections), "count must match the array length"
        for item in collections:
            assert _UUID_RE.match(item.get("id", "")), (
                f"collection id not UUID-shaped: {item.get('id')!r}"
            )
            assert item.get("name", "").strip(), "collection name must be non-blank"
            assert item["notebook_count"] >= 0, "notebook_count cannot be negative"


class TestCollectionNotebooksCommand:
    """Test ``notebooklm collection notebooks <ref>`` (group -> notebooks)."""

    @notebooklm_vcr.use_cassette("collection_notebooks.yaml", allow_playback_repeats=True)
    def test_collection_notebooks(self, runner, mock_auth_for_vcr):
        """``collection notebooks`` expands a collection to its notebook objects."""
        result = runner.invoke(cli, ["collection", "notebooks", COLLECTION_ID])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_notebooks.yaml", allow_playback_repeats=True)
    def test_collection_notebooks_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``collection notebooks --json`` matches the schema; echoes the id."""
        result = runner.invoke(cli, ["collection", "notebooks", COLLECTION_ID, "--json"])
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_NOTEBOOKS_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: the resolved collection id round-trips into the result.
        assert data["collection_id"] == COLLECTION_ID
        assert data["count"] == len(data["notebooks"]), "count must match the array length"


class TestCollectionCreateCommand:
    """Test ``notebooklm collection create <name>``.

    Unlike every other test in this module, this cassette must NOT use
    ``allow_playback_repeats`` — see the module docstring's RPC fan-out note.
    """

    @notebooklm_vcr.use_cassette("collection_create.yaml")
    def test_collection_create(self, runner, mock_auth_for_vcr):
        """``collection create`` runs LIST_LABELS (before) + CREATE_LABEL + LIST_LABELS (after)."""
        result = runner.invoke(cli, ["collection", "create", "VCR Test Create"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_create_json.yaml")
    def test_collection_create_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection create --json`` schema + invariants."""
        result = runner.invoke(cli, ["collection", "create", "VCR Test Create JSON", "--json"])
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 2 — the created collection's id is UUID-shaped and its name non-blank.
        assert _UUID_RE.match(data.get("id", "")), (
            f"collection id not UUID-shaped: {data.get('id')!r}"
        )
        assert data.get("name", "").strip(), "created collection name must be non-blank"


class TestCollectionRenameCommand:
    """Test ``notebooklm collection rename <ref> <new_name>``."""

    @notebooklm_vcr.use_cassette("collection_rename.yaml", allow_playback_repeats=True)
    def test_collection_rename(self, runner, mock_auth_for_vcr):
        """``collection rename`` runs resolve + preflight + UPDATE_LABEL + re-read."""
        result = runner.invoke(cli, ["collection", "rename", COLLECTION_ID, "VCR Test Collection"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_rename.yaml", allow_playback_repeats=True)
    def test_collection_rename_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``collection rename --json`` matches the schema; echoes the id."""
        result = runner.invoke(
            cli, ["collection", "rename", COLLECTION_ID, "VCR Test Collection", "--json"]
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["id"] == COLLECTION_ID


class TestCollectionAddCommand:
    """Test ``notebooklm collection add <ref> <notebook_ids...>``."""

    @notebooklm_vcr.use_cassette("collection_add.yaml", allow_playback_repeats=True)
    def test_collection_add(self, runner, mock_auth_for_vcr):
        """``collection add`` runs resolve + notebook-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli, ["collection", "add", COLLECTION_ID, COLLECTION_MEMBER_NOTEBOOK_ID]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_add.yaml", allow_playback_repeats=True)
    def test_collection_add_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``collection add --json`` matches the schema; echoes the ids."""
        result = runner.invoke(
            cli,
            ["collection", "add", COLLECTION_ID, COLLECTION_MEMBER_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_ADD_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo: the resolved ids round-trip into the result.
        assert data["id"] == COLLECTION_ID
        assert data["added_notebook_ids"] == [COLLECTION_MEMBER_NOTEBOOK_ID]


class TestCollectionRemoveCommand:
    """Test ``notebooklm collection remove <ref> <notebook_ids...>`` (un-assign).

    The inverse of ``add`` — un-assigns the notebook from this collection only
    (the notebook survives in the account). No ``--yes`` gate (non-destructive).
    """

    @notebooklm_vcr.use_cassette("collection_remove.yaml", allow_playback_repeats=True)
    def test_collection_remove(self, runner, mock_auth_for_vcr):
        """``collection remove`` runs resolve + notebook-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli, ["collection", "remove", COLLECTION_ID, COLLECTION_MEMBER_NOTEBOOK_ID]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_remove.yaml", allow_playback_repeats=True)
    def test_collection_remove_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``collection remove --json`` matches the schema; echoes the ids."""
        result = runner.invoke(
            cli,
            ["collection", "remove", COLLECTION_ID, COLLECTION_MEMBER_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_REMOVE_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["id"] == COLLECTION_ID
        assert data["removed_notebook_ids"] == [COLLECTION_MEMBER_NOTEBOOK_ID]


class TestCollectionDeleteCommand:
    """Test ``notebooklm collection delete <refs...>``.

    Destructive, so each test method targets its OWN throwaway collection
    (``COLLECTION_DELETE_ID`` / ``COLLECTION_DELETE_ID_JSON``) rather than
    reusing ``COLLECTION_ID`` — deleting the shared collection here would break
    every other cassette in this module that depends on it still existing.
    """

    @notebooklm_vcr.use_cassette("collection_delete.yaml")
    def test_collection_delete(self, runner, mock_auth_for_vcr):
        """``collection delete --yes`` runs the batch resolve + DELETE_LABEL."""
        result = runner.invoke(cli, ["collection", "delete", COLLECTION_DELETE_ID, "--yes"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_delete_json.yaml")
    def test_collection_delete_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 5: ``collection delete --yes --json`` matches the schema; echoes ids."""
        result = runner.invoke(
            cli, ["collection", "delete", COLLECTION_DELETE_ID_JSON, "--yes", "--json"]
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_DELETE_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["deleted"] is True
        assert data["collection_ids"] == [COLLECTION_DELETE_ID_JSON]
