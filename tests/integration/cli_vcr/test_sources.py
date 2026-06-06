"""CLI integration tests for source commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.

The ``source list`` tests are the *template* for the re-record-safe assertion
tiers (issue #1452). Every assertion here must survive a re-record against a
DIFFERENT notebook with different data:

* **Tier 1 — Schema.** The ``--json`` envelope field names + types match
  ``SOURCE_LIST_SCHEMA`` (via ``assert_json_envelope``).
* **Tier 2 — Invariants.** ``count > 0``; each id is UUID-shaped; each title is
  non-empty. Never ``== N`` and never a recorded value.
* **Tier 2b — Cassette-derived value correctness (depth-2).** The CLI's emitted
  ids/urls/count are checked against an INDEPENDENT shallow projection of the
  recorded ``GET_NOTEBOOK`` payload (``_cassette_expectations``). This catches
  fabrication / drop / duplicate / miscount: ``count == proj.count``, the CLI id
  set equals ``proj.ids``, and every url the shallow projection found is present
  in the CLI render (``proj.urls ⊆ cli_urls`` — the shallow read may miss a url
  the deep decoder finds, so it must be the subset side). The projection reads
  the cassette with stdlib + yaml only (no production decoder), so the assert is
  a real oracle, not a tautology — and it stays re-record-safe because both sides
  are read from the *same* cassette.
* **Tier 2c — Per-field semantic invariants.** ``assert_semantic_invariants``
  pins per-field meaning (url parses, enum value is known, timestamp parses) —
  catching a "valid type but wrong field" read.
* **Tier 3 — Cross-render.** ``source list`` text mode and ``--json`` of the
  same cassette agree on the row count.
* **Tier 5 — Input-echo.** ``source delete --json`` echoes the source/notebook
  ids the test passed (``DELETE_SOURCE_ID`` / ``DELETE_NOTEBOOK_ID``) — the CLI
  threads the *input* into its own result, so it holds for any cassette.

(Tier 4 — filter correctness — lives in ``test_artifacts.py``, where
``artifact list --type`` exercises decoder filtering; ``source list`` has no
type filter to assert.)
"""

import re

import pytest

from notebooklm.notebooklm_cli import cli

from ._cassette_expectations import load_rpc_payload, project_source_list
from ._fixtures import DELETE_NOTEBOOK_ID, DELETE_SOURCE_ID, VCR_READONLY_SOURCE_ID
from .conftest import (
    SOURCE_LIST_SCHEMA,
    SOURCE_MUTATION_SCHEMA,
    assert_command_success,
    assert_json_envelope,
    assert_semantic_invariants,
    notebooklm_vcr,
    parse_json_dict,
    skip_no_cassettes,
)

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``source list`` resolves the source list out of the ``GET_NOTEBOOK`` (``rLM1Ne``)
# payload — the same RPC the projection helper reads independently.
_GET_NOTEBOOK_RPC_ID = "rLM1Ne"

# Loose UUID shape check (8-4-4-4-12 hex). Deliberately not anchored to a
# specific value — a re-record yields different ids that must still be UUIDs.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _count_text_table_data_rows(output: str) -> int:
    """Count source rows in the Rich table rendered by ``source list`` (text).

    Rich wraps long cell values across visual lines, so a source can span
    several lines. Each source row begins with its (full-width) ID cell; a
    continuation/wrap line leaves that cell blank. Count only the lines whose
    first cell is a non-empty ID fragment to get the source count.
    """
    count = 0
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "│┃":
            continue  # not a table data line
        # Split on the vertical separators and inspect the first cell.
        cells = re.split(r"[│┃]", line)
        # cells[0] is the leading margin before the first border; the ID cell
        # is cells[1].
        if len(cells) < 2:
            continue
        first_cell = cells[1].strip()
        # Skip the header row ("ID") and wrap-continuation lines (blank ID).
        if not first_cell or first_cell == "ID":
            continue
        count += 1
    return count


class TestSourceListCommand:
    """Test 'notebooklm source list' command (re-record-safe tier template)."""

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list_json_schema(self, runner, mock_auth_for_vcr, mock_context):
        """Tier 1 + 2 + 2c: ``source list --json`` matches the schema, invariants,
        and per-field semantic rules."""
        result = runner.invoke(cli, ["source", "list", "--json"])
        assert_command_success(result, allow_no_context=False)

        # Tier 1 — envelope shape (field names + types).
        assert_json_envelope(result, schema=SOURCE_LIST_SCHEMA)

        data = parse_json_dict(result.output)
        sources = data["sources"]

        # Tier 2 — value invariants, never pinned to recorded values.
        assert data["count"] > 0, "expected a non-empty source list"
        assert data["count"] == len(sources), "count must match the array length"
        assert _UUID_RE.match(data["notebook_id"]), "notebook_id must be UUID-shaped"
        for index, src in enumerate(sources, 1):
            assert src.get("index") == index, "index must be 1-based and contiguous"
            assert _UUID_RE.match(src.get("id", "")), (
                f"source id not UUID-shaped: {src.get('id')!r}"
            )
            # ``title`` is nullable in the schema; a source *may* be untitled.
            # When a title is present it must be non-blank — that is the
            # re-record-safe invariant (never pin the recorded title value).
            title = src.get("title")
            if title is not None:
                assert title.strip(), "a present source title must be non-blank"
            # Tier 2c — per-field semantic invariants (url parses, enum value is
            # known, timestamp parses): catches a "valid type but wrong field".
            assert_semantic_invariants(src, "source")

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list_matches_cassette_projection(self, runner, mock_auth_for_vcr, mock_context):
        """Tier 2b: the CLI's ids/urls/count match an INDEPENDENT projection of
        the recorded ``GET_NOTEBOOK`` payload.

        The projection (``_cassette_expectations``) reads the cassette with
        stdlib + yaml only — it does NOT import the production decoder — so this
        is a real fabrication/drop/duplicate/miscount oracle rather than a
        tautology. Both sides are read from the *same* cassette, so the assert is
        re-record-safe: a re-record against a different notebook re-reads both
        the CLI output and the projection, and they still must agree.
        """
        result = runner.invoke(cli, ["source", "list", "--json"])
        assert_command_success(result, allow_no_context=False)
        cli_items = parse_json_dict(result.output)["sources"]

        payload = load_rpc_payload("sources_list.yaml", _GET_NOTEBOOK_RPC_ID)
        proj = project_source_list(payload)

        assert proj.count > 0, "projection found no sources — cassette/projection drift"
        # Count: the CLI must emit exactly as many rows as the cassette holds
        # (no drop, no duplicate, no miscount).
        assert len(cli_items) == proj.count, (
            f"CLI emitted {len(cli_items)} sources but the cassette payload holds {proj.count}"
        )
        # Id set: every CLI id is in the cassette and vice versa (no fabricated
        # id, no dropped source). Source ids are reliably UUID-shaped. Filter out
        # a (schema-forbidden) ``None`` id so a missing id surfaces as a clean
        # set-difference rather than a ``TypeError`` while ``sorted()``-ing the
        # diff in the message.
        cli_ids = {id_ for src in cli_items if (id_ := src.get("id")) is not None}
        assert cli_ids == proj.ids, (
            "CLI source id set differs from the cassette projection "
            f"(only-in-CLI={sorted(cli_ids - proj.ids)}, "
            f"only-in-cassette={sorted(proj.ids - cli_ids)})"
        )
        # Urls: every url the shallow projection found must surface in the CLI
        # output. Containment in THIS direction (proj ⊆ cli) is the re-record-safe
        # one: the shallow projection may MISS a url the deep decoder finds (so it
        # must not be the superset side), but it never invents one — so any url it
        # did extract from the recorded payload must appear in the CLI's render.
        cli_urls = {url for src in cli_items if (url := src.get("url"))}
        assert proj.urls <= cli_urls, (
            "cassette-projected urls missing from the CLI output "
            f"(missing={sorted(proj.urls - cli_urls)})"
        )

    @notebooklm_vcr.use_cassette("sources_list.yaml", allow_playback_repeats=True)
    def test_source_list_text_and_json_agree(self, runner, mock_auth_for_vcr, mock_context):
        """Tier 3: text-mode row count agrees with the ``--json`` count.

        Same cassette replayed twice (``allow_playback_repeats``): the two
        render paths must agree on how many sources the notebook has, which
        catches a renderer drifting from the serializer regardless of the
        recorded data.
        """
        json_result = runner.invoke(cli, ["source", "list", "--json"])
        assert_command_success(json_result, allow_no_context=False)
        json_count = parse_json_dict(json_result.output)["count"]

        text_result = runner.invoke(cli, ["source", "list", "--no-truncate"])
        assert_command_success(text_result, allow_no_context=False)
        text_count = _count_text_table_data_rows(text_result.output)

        assert text_count == json_count, (
            f"text table rendered {text_count} rows but --json reported {json_count} sources"
        )


class TestSourceAddCommand:
    """Test 'notebooklm source add' command."""

    @pytest.mark.parametrize(
        ("cassette", "args"),
        [
            (
                "sources_add_url.yaml",
                ["source", "add", "https://en.wikipedia.org/wiki/Artificial_intelligence"],
            ),
            (
                "sources_add_text.yaml",
                [
                    "source",
                    "add",
                    "--type",
                    "text",
                    "--title",
                    "Test Source",
                    "This is test content.",
                ],
            ),
        ],
    )
    def test_source_add(self, runner, mock_auth_for_vcr, mock_context, cassette, args):
        """Add source (URL or text) works with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, args)
            assert_command_success(result)


class TestSourceContentCommands:
    """Test source content retrieval commands (guide, fulltext)."""

    @pytest.mark.parametrize(
        ("command", "cassette"),
        [
            ("guide", "sources_get_guide.yaml"),
            ("fulltext", "sources_get_fulltext.yaml"),
        ],
    )
    def test_source_content(self, runner, mock_auth_for_vcr, mock_context, command, cassette):
        """Get source content works with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["source", command, VCR_READONLY_SOURCE_ID])
            assert_command_success(result)


class TestSourceDeleteCommand:
    """Test delete command paths that can reuse existing VCR coverage."""

    @notebooklm_vcr.use_cassette("sources_delete.yaml")
    def test_source_delete_full_uuid(self, runner, mock_auth_for_vcr, mock_context):
        """Delete source by full UUID works with real client."""
        result = runner.invoke(
            cli,
            [
                "source",
                "delete",
                DELETE_SOURCE_ID,
                "-n",
                DELETE_NOTEBOOK_ID,
                "-y",
            ],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("sources_delete.yaml")
    def test_source_delete_json_input_echo(self, runner, mock_auth_for_vcr, mock_context):
        """Tier 1 + 5: ``source delete --json`` matches the mutation schema and
        echoes the ids the test passed.

        The echoed ids prove the CLI threads its *input* into the result, so the
        assertion holds for any cassette and any re-record (``DELETE_SOURCE_ID``
        / ``DELETE_NOTEBOOK_ID`` are decorative placeholders).
        """
        result = runner.invoke(
            cli,
            [
                "source",
                "delete",
                DELETE_SOURCE_ID,
                "-n",
                DELETE_NOTEBOOK_ID,
                "-y",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        # Tier 1 — envelope shape.
        assert_json_envelope(result, schema=SOURCE_MUTATION_SCHEMA)

        data = parse_json_dict(result.output)
        # Tier 5 — input-echo.
        assert data["action"] == "delete"
        assert data["source_id"] == DELETE_SOURCE_ID
        assert data["notebook_id"] == DELETE_NOTEBOOK_ID
        assert data["success"] is True
