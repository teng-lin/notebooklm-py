"""CLI integration tests for the ``collection`` command group.

These exercise the full CLI -> Client -> RPC path using VCR cassettes, mirroring
``test_label.py`` (the notebook-scoped sibling). They cover the ``collection``
happy paths (``list`` / ``notebooks`` / ``create`` / ``rename`` / ``add`` /
``remove`` / ``delete``).

Collections are **account-level**: every command takes no ``-n/--notebook``
option, and ``resolve_collection_id`` disables full-id passthrough (a
UUID-shaped *name* is not blindly accepted as an id) — unlike notebook refs, a
collection ``<ref>`` argument MUST match a real entry in the recorded
``LIST_LABELS`` response. Every collection ref below is therefore an exact
**name**, not an id: a re-record only needs live collections with the same
names to exist (no id-hunting in ``_fixtures.py`` afterward), and name
resolution is exercised as a side effect (matches ``resolve_collection_id``'s
Pass 2, not just the id/prefix fast path).

Mutation commands (``rename`` / ``add`` / ``remove``) record a REAL state
transition — a different name, a notebook actually absent-then-present or
present-then-absent — rather than an idempotent no-op, and assert on the
resulting values (not just echoed input), so a regression that silently no-ops
the mutation (exactly the class of bug PR #2009 fixed for ``remove_notebooks``)
would fail this test. Each such command therefore also gets its own cassette
per test method (not shared between the human-output and ``--json`` variants,
unlike the read-only commands) and does NOT use
``allow_playback_repeats=True`` — see the RPC fan-out note below for why.

RPC fan-out per command
------------------------
``client.collections`` issues these RPCs (see ``src/notebooklm/_web/collections.py``
and ``src/notebooklm/rpc/types.py``); every collection RPC reuses the label
method ids with a type-3 discriminator and ``source_path="/"`` (account-level):

* ``list``      -> one ``LIST_LABELS`` (``I3xc3c``).
* ``notebooks`` -> the resolver's ``LIST_LABELS``, then ``execute_collection_notebooks``
  -> one more ``LIST_LABELS`` (``get()``'s internal ``list()``) + one
  ``LIST_NOTEBOOKS`` (the membership -> Notebook join). Pure read, no state
  transition between the two ``LIST_LABELS`` calls, so this cassette is shared
  between the human-output and ``--json`` test methods with
  ``allow_playback_repeats=True`` like the other read-only commands.
* ``create``    -> ``LIST_LABELS`` (the id-diff snapshot, BEFORE), ``CREATE_LABEL``
  (``agX4Bc``), then a second ``LIST_LABELS`` (the id-diff re-list, AFTER) — unlike
  ``label create``, this does NOT parse the create echo (the create-response shape
  was never captured), so it genuinely needs two *sequential, distinct-content*
  ``LIST_LABELS`` episodes.
* ``rename``    -> the resolver's ``LIST_LABELS``, ``rename()``'s own preflight
  ``LIST_LABELS`` (``get_or_none``, same content as the resolver's — no
  transition yet), ``UPDATE_LABEL``, then a post-write ``LIST_LABELS``
  (``get()``) whose content DOES differ (the new name).
* ``add``       -> the resolver's ``LIST_LABELS`` (pre-add: notebook absent),
  ``_resolve_notebook_ids``'s ``LIST_NOTEBOOKS``, ``UPDATE_LABEL`` (variant
  ``add_notebooks``), then the ADR-0019 re-fetch ``LIST_LABELS``
  (``get_or_none``, post-add: notebook present) — content DOES differ.
* ``remove``    -> same shape as ``add`` but inverted (pre-remove: notebook
  present; post-remove: notebook absent), variant ``remove_notebooks``.
* ``delete``    -> the batch resolver's ``LIST_LABELS`` (once, regardless of ref
  count), then ``DELETE_LABEL`` (``GyzE7e``). Already one throwaway collection
  per test method (delete is destructive), unchanged from read-only cassette
  conventions otherwise.

For ``create`` / ``rename`` / ``add`` / ``remove``, the two ``LIST_LABELS``
calls that must show different content have an IDENTICAL request shape (all
collection RPCs take no notebook-scoping param), so vcrpy's matcher can't tell
them apart by request alone. Omitting ``allow_playback_repeats`` is what makes
this work: vcrpy's default consumes matching recorded episodes in RECORDED
ORDER (first matching request gets the first not-yet-played episode, second
gets the next), so the "before" and "after" episodes are correctly served in
sequence. ``allow_playback_repeats=True`` disables that consumption tracking —
it would always replay the SAME (first) episode, which would either break the
command's own control flow (``create``, which raises if the diff comes up
empty) or silently mask a regression (``add``/``remove``/``rename``, which
would still pass with stale content since post-mutation state is never
actually observed).

Re-record-safe assertions
--------------------------
Per ``tests/integration/cli_vcr/README.md`` the assertions stay in the allowed
vocabulary — Schema (``--json`` envelope shape) and Invariants (id shape /
non-empty name / ``count >= 0``) — plus, for the mutation commands, asserting
the RESULT reflects the real transition recorded (the new name, the added/
removed notebook id present/absent in ``notebook_ids``), which is a value the
test itself controls (the requested new name / notebook id), not an opaque
server-generated one. A re-record against freshly recreated same-named fixture
collections reproduces the same transition and keeps these assertions valid.

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_collection.py -m vcr

Recording note: several read/mutation cassettes need pre-existing live
collections in a specific starting state (e.g. ``add`` needs its target
collection to start EMPTY so the recorded before/after actually differ, and
``remove`` needs its target to start with the member already present) —
that bootstrap is done via direct ``client.collections``/``client.notebooks``
calls OUTSIDE any cassette context, not by the test methods themselves.
"""

import re

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import COLLECTION_MEMBER_NOTEBOOK_ID
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
        # Fixture invariant: recorded with several live collections present.
        assert data["count"] > 0, "recorded fixture account has collections"
        for item in collections:
            assert _UUID_RE.match(item.get("id", "")), (
                f"collection id not UUID-shaped: {item.get('id')!r}"
            )
            assert item.get("name", "").strip(), "collection name must be non-blank"
            assert item["notebook_count"] >= 0, "notebook_count cannot be negative"


class TestCollectionNotebooksCommand:
    """Test ``notebooklm collection notebooks <ref>`` (group -> notebooks).

    Targets "VCR Test Notebooks", recorded with one real member notebook
    already present (added outside the cassette context) so the join is
    exercised non-trivially.
    """

    @notebooklm_vcr.use_cassette("collection_notebooks.yaml", allow_playback_repeats=True)
    def test_collection_notebooks(self, runner, mock_auth_for_vcr):
        """``collection notebooks`` expands a collection to its notebook objects."""
        result = runner.invoke(cli, ["collection", "notebooks", "VCR Test Notebooks"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_notebooks.yaml", allow_playback_repeats=True)
    def test_collection_notebooks_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection notebooks --json`` matches the schema; nonempty."""
        result = runner.invoke(cli, ["collection", "notebooks", "VCR Test Notebooks", "--json"])
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_NOTEBOOKS_SCHEMA)

        data = parse_json_dict(result.output)
        assert _UUID_RE.match(data.get("collection_id", "")), (
            f"collection id not UUID-shaped: {data.get('collection_id')!r}"
        )
        assert data["count"] == len(data["notebooks"]), "count must match the array length"
        # Fixture invariant: "VCR Test Notebooks" was recorded with one member.
        assert data["count"] > 0, "fixture collection has a recorded member notebook"


class TestCollectionCreateCommand:
    """Test ``notebooklm collection create <name>``.

    Each method's cassette must NOT use ``allow_playback_repeats`` — see the
    module docstring's RPC fan-out note.
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
    """Test ``notebooklm collection rename <ref> <new_name>``.

    Targets throwaway collections starting as "VCR Test Rename Source"[ JSON],
    each renamed to a genuinely DIFFERENT name during recording — not an
    idempotent no-op — so the assertions can verify the returned name actually
    reflects the mutation, not just its own input.
    """

    @notebooklm_vcr.use_cassette("collection_rename.yaml")
    def test_collection_rename(self, runner, mock_auth_for_vcr):
        """``collection rename`` runs resolve + preflight + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli, ["collection", "rename", "VCR Test Rename Source", "VCR Test Rename Target"]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_rename_json.yaml")
    def test_collection_rename_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection rename --json`` matches the schema; reflects the new name."""
        result = runner.invoke(
            cli,
            [
                "collection",
                "rename",
                "VCR Test Rename Source JSON",
                "VCR Test Rename Target JSON",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        # The returned name reflects the mutation, not the pre-rename state.
        assert data["name"] == "VCR Test Rename Target JSON"


class TestCollectionAddCommand:
    """Test ``notebooklm collection add <ref> <notebook_ids...>``.

    Targets throwaway collections starting EMPTY ("VCR Test Add"[ JSON]) so the
    recorded before/after ``LIST_LABELS`` genuinely differ and the assertions
    can verify the notebook actually landed in ``notebook_ids``, not just that
    the input was echoed back.
    """

    @notebooklm_vcr.use_cassette("collection_add.yaml")
    def test_collection_add(self, runner, mock_auth_for_vcr):
        """``collection add`` runs resolve + notebook-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli, ["collection", "add", "VCR Test Add", COLLECTION_MEMBER_NOTEBOOK_ID]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_add_json.yaml")
    def test_collection_add_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection add --json`` matches the schema; membership actually changed."""
        result = runner.invoke(
            cli,
            ["collection", "add", "VCR Test Add JSON", COLLECTION_MEMBER_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_ADD_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["added_notebook_ids"] == [COLLECTION_MEMBER_NOTEBOOK_ID]
        # The post-write re-read reflects the add, not the pre-add (empty) state.
        assert COLLECTION_MEMBER_NOTEBOOK_ID in data["notebook_ids"]


class TestCollectionRemoveCommand:
    """Test ``notebooklm collection remove <ref> <notebook_ids...>`` (un-assign).

    The inverse of ``add`` — un-assigns the notebook from this collection only
    (the notebook survives in the account). No ``--yes`` gate (non-destructive).
    Targets throwaway collections starting WITH the member present
    ("VCR Test Remove"[ JSON]) so the recorded before/after genuinely differ and
    the assertions can verify the notebook actually left ``notebook_ids`` — this
    is exactly the class of regression PR #2009 fixed (an inferred wire shape
    that made ``remove_notebooks`` a silent no-op); a test that can't observe
    the post-state wouldn't have caught it.
    """

    @notebooklm_vcr.use_cassette("collection_remove.yaml")
    def test_collection_remove(self, runner, mock_auth_for_vcr):
        """``collection remove`` runs resolve + notebook-resolve + UPDATE_LABEL + re-read."""
        result = runner.invoke(
            cli, ["collection", "remove", "VCR Test Remove", COLLECTION_MEMBER_NOTEBOOK_ID]
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_remove_json.yaml")
    def test_collection_remove_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection remove --json`` matches the schema; membership actually changed."""
        result = runner.invoke(
            cli,
            [
                "collection",
                "remove",
                "VCR Test Remove JSON",
                COLLECTION_MEMBER_NOTEBOOK_ID,
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_REMOVE_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["removed_notebook_ids"] == [COLLECTION_MEMBER_NOTEBOOK_ID]
        # The post-write re-read reflects the removal, not the pre-remove (present) state.
        assert COLLECTION_MEMBER_NOTEBOOK_ID not in data["notebook_ids"]


class TestCollectionDeleteCommand:
    """Test ``notebooklm collection delete <refs...>``.

    Destructive, so each test method targets its OWN throwaway collection
    ("VCR Test Delete"[ JSON]) rather than a shared one — deleting a collection
    other cassettes in this module still depend on would break them.
    """

    @notebooklm_vcr.use_cassette("collection_delete.yaml")
    def test_collection_delete(self, runner, mock_auth_for_vcr):
        """``collection delete --yes`` runs the batch resolve + DELETE_LABEL."""
        result = runner.invoke(cli, ["collection", "delete", "VCR Test Delete", "--yes"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("collection_delete_json.yaml")
    def test_collection_delete_json(self, runner, mock_auth_for_vcr):
        """Tier 1 + 2: ``collection delete --yes --json`` matches the schema."""
        result = runner.invoke(
            cli, ["collection", "delete", "VCR Test Delete JSON", "--yes", "--json"]
        )
        assert_command_success(result, allow_no_context=False)

        assert_json_envelope(result, schema=COLLECTION_DELETE_SCHEMA)

        data = parse_json_dict(result.output)
        assert data["deleted"] is True
        assert len(data["collection_ids"]) == 1
        assert _UUID_RE.match(data["collection_ids"][0]), (
            f"collection id not UUID-shaped: {data['collection_ids'][0]!r}"
        )
