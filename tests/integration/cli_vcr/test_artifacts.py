"""CLI integration tests for artifact commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.

The ``artifact list --json`` test carries the depth-2 re-record-safe tiers
(issue #1452), adapted for artifacts:

* **Schema/invariants.** The ``--json`` envelope is an object with a list of
  artifact items; ids are non-empty (but NOT forced UUID — some artifact ids are
  numeric).
* **Cassette-derived value correctness (depth-2).** The CLI's emitted ids/count
  are checked against an INDEPENDENT shallow projection of the recorded
  ``LIST_ARTIFACTS`` payload (``_cassette_expectations``). Because the CLI list
  merges note-backed mind maps from a *separate* RPC, the CLI output is a
  superset of the ``gArtLc`` projection — so this uses CONTAINMENT and a count
  floor (``proj.ids ⊆ cli_ids``, ``len(cli) >= proj.count``), not equality.
* **Per-field semantic invariants.** ``assert_semantic_invariants`` pins per-field
  meaning (``type_id``/``status`` are known enum values, ``created_at`` parses).
"""

import pytest

from notebooklm.notebooklm_cli import cli

# Enum *code* sets — an allowed-membership floor for the projection's raw integer
# type/status codes (a set-membership check, not a decode; see #1452).
from notebooklm.rpc.types import ArtifactStatus, ArtifactTypeCode

from ._cassette_expectations import load_rpc_payload, project_artifact_list
from ._fixtures import ARTIFACT_NOTEBOOK_ID
from .conftest import (
    assert_command_success,
    assert_semantic_invariants,
    notebooklm_vcr,
    parse_json_dict,
    parse_json_output,
    skip_no_cassettes,
)

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``artifact list`` reads the recorded ``LIST_ARTIFACTS`` (``gArtLc``) payload —
# the same RPC the projection helper reads independently.
_LIST_ARTIFACTS_RPC_ID = "gArtLc"

_KNOWN_ARTIFACT_TYPE_CODES = frozenset(member.value for member in ArtifactTypeCode)
# ``0`` is the "unknown" status the CLI degrades an unrecognized code to (see
# ``conftest._ARTIFACT_STATUS_STR_VALUES`` which keeps ``artifact_status_to_str(0)``);
# tolerate it here so a re-record carrying a status-0 row is not a spurious failure.
_KNOWN_ARTIFACT_STATUS_CODES = frozenset(member.value for member in ArtifactStatus) | {0}


class TestArtifactListCommand:
    """Test 'notebooklm artifact list' command."""

    @pytest.mark.parametrize("json_flag", [False, True])
    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list(self, runner, mock_auth_for_vcr, mock_context, json_flag):
        """List artifacts with optional --json flag."""
        args = ["artifact", "list"]
        if json_flag:
            args.append("--json")

        result = runner.invoke(cli, args)
        assert_command_success(result)

        if json_flag and result.exit_code == 0:
            data = parse_json_output(result.output)
            assert data is not None, "Expected valid JSON output"
            assert isinstance(data, list | dict)

    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list_matches_cassette_projection(
        self, runner, mock_auth_for_vcr, mock_context
    ):
        """Depth-2: the CLI's artifact ids/count + per-field meaning are checked
        against an INDEPENDENT projection of the recorded ``LIST_ARTIFACTS`` payload.

        Containment, not equality: ``artifact list`` merges note-backed mind maps
        from a separate RPC, so the CLI output is a superset of the ``gArtLc``
        projection. Artifact ids are also not all UUID-shaped, so this anchors on
        id CONTAINMENT + a count floor (never a recorded value), which stays
        re-record-safe — both sides are read from the same cassette.
        """
        result = runner.invoke(cli, ["artifact", "list", "--json"])
        assert_command_success(result)
        data = parse_json_dict(result.output)
        cli_items = data["artifacts"]
        assert isinstance(cli_items, list)

        payload = load_rpc_payload("artifacts_list.yaml", _LIST_ARTIFACTS_RPC_ID)
        proj = project_artifact_list(payload)
        assert proj.count > 0, "projection found no artifacts — cassette/projection drift"

        for art in cli_items:
            assert art.get("id"), f"artifact item is missing a non-empty id: {art!r}"
        cli_ids = {art.get("id") for art in cli_items}
        assert len(cli_ids) == len(cli_items), "CLI emitted a duplicate artifact id"
        # Count floor: the CLI list is the gArtLc rows PLUS merged note-backed
        # mind maps, so it can only be >= the projection's row count.
        assert len(cli_items) >= proj.count, (
            f"CLI emitted {len(cli_items)} artifacts, fewer than the "
            f"{proj.count} recorded LIST_ARTIFACTS rows (a drop)"
        )
        # Containment: every recorded gArtLc artifact id must surface in the CLI
        # output (none dropped); the projection ids are a subset of the CLI's.
        assert proj.ids <= cli_ids, (
            "recorded LIST_ARTIFACTS ids missing from CLI output (a drop): "
            f"{sorted(proj.ids - cli_ids)}"
        )
        # No duplicate id within the recorded rows: each projected row has a
        # distinct id, so the id set is exactly as large as the row count (a
        # server-side duplicate would collapse the set below ``count``).
        assert len(proj.ids) == proj.count, (
            "recorded LIST_ARTIFACTS rows carry a duplicate id "
            f"({proj.count} rows, {len(proj.ids)} distinct ids)"
        )

        # Per-field semantic invariants: type_id/status are known enum values and
        # created_at parses — for EVERY CLI item, not just the projected subset.
        for art in cli_items:
            assert_semantic_invariants(art, "artifact")

        # Type/status histogram consistency: every type code the projection saw
        # is a known artifact type code, and likewise for status — the projection
        # is coarser than the CLI's variant-aware mapping, so this checks the
        # codes are in-range rather than equal to the CLI histogram.
        for status_id in proj.status_codes:
            assert status_id in _KNOWN_ARTIFACT_STATUS_CODES, (
                f"recorded artifact status code {status_id} is not a known code"
            )
        for type_code in proj.type_codes:
            assert type_code in _KNOWN_ARTIFACT_TYPE_CODES, (
                f"recorded artifact type code {type_code} is not a known code"
            )


class TestArtifactListByType:
    """Test 'notebooklm artifact list --type' command."""

    @pytest.mark.parametrize(
        ("artifact_type", "cassette"),
        [
            ("quiz", "artifacts_list_quizzes.yaml"),
            ("report", "artifacts_list_reports.yaml"),
            ("video", "artifacts_list_video.yaml"),
            ("flashcard", "artifacts_list_flashcards.yaml"),
            ("infographic", "artifacts_list_infographics.yaml"),
            ("slide-deck", "artifacts_list_slide_decks.yaml"),
            ("data-table", "artifacts_list_data_tables.yaml"),
            ("mind-map", "notes_list_mind_maps.yaml"),
        ],
    )
    def test_artifact_list_by_type(
        self, runner, mock_auth_for_vcr, mock_context, artifact_type, cassette
    ):
        """List artifacts filtered by type.

        For INFOGRAPHIC and DATA_TABLE we additionally assert the rendered
        JSON output exposes the parsed ``type_id`` matching the requested
        filter — proving the parser, not just the transport, agrees on the
        kind.
        """
        # only the INFOGRAPHIC + DATA_TABLE rows opt into ``--json``.
        # The other rows stay on the table renderer to preserve their
        # historical (xfail-masked) call sequence — the ``--json`` path
        # makes an extra ``notebooks.get()`` RPC for the table header that
        # several legacy cassettes do not have recorded.
        is_target_type = artifact_type in {"infographic", "data-table"}
        args = ["artifact", "list", "--type", artifact_type]
        if is_target_type:
            args.append("--json")

        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, args)
            assert_command_success(result)

            # Parser-shape sanity check for the two types this task targets.
            if is_target_type and result.exit_code == 0:
                data = parse_json_output(result.output)
                assert isinstance(data, dict)
                artifacts = data.get("artifacts", [])
                assert isinstance(artifacts, list)
                # The recorded cassettes each contain one artifact of the
                # requested kind. ``type_id`` is the user-facing string enum
                # value (``"infographic"`` / ``"data_table"``); the CLI maps
                # the kebab-case filter to the snake_case enum value.
                expected_type_id = artifact_type.replace("-", "_")
                for art in artifacts:
                    assert art.get("type_id") == expected_type_id, (
                        f"Parsed type_id {art.get('type_id')!r} does not match "
                        f"filter {artifact_type!r} (cassette {cassette})"
                    )

    def test_artifact_list_type_mind_map_interactive(self, runner, mock_auth_for_vcr, mock_context):
        """`artifact list --type mind-map` surfaces an interactive (studio-artifact) map.

        Reuses the interactive recording (``mind_maps_interactive.yaml``,
        ``ARTIFACT_NOTEBOOK_ID``). Stays on the table renderer (no ``--json``) so
        it needs only ``LIST_ARTIFACTS`` + ``GET_NOTES_AND_MIND_MAPS``, both
        present in the cassette — proving the type-4/variant-4 map is recognized
        end-to-end through the CLI (#1256).

        Re-record-safe assertion: the rendered table must carry the mind-map
        **type display** (``get_artifact_type_display`` → ``Mind Map``), which
        the renderer only emits for a row whose parsed kind is
        ``ArtifactType.MIND_MAP``. That proves the type-4/variant-4 artifact was
        recognized as a mind map and survived the ``--type mind-map`` filter,
        without pinning the recorded artifact id/title (which change on a
        re-record against a different notebook). The empty-state path prints
        ``No mind-map artifacts found`` instead, so the marker also proves the
        filter returned a non-empty row.
        """
        nb = ARTIFACT_NOTEBOOK_ID
        with notebooklm_vcr.use_cassette("mind_maps_interactive.yaml", allow_playback_repeats=True):
            result = runner.invoke(cli, ["artifact", "list", "--type", "mind-map", "-n", nb])
            assert_command_success(result)
            assert "Mind Map" in result.output, (
                "Expected the rendered table to carry the mind-map type display, "
                "proving the type-4/variant-4 interactive map was recognized and "
                f"passed the --type mind-map filter; output was:\n{result.output}"
            )
            assert "No mind-map artifacts found" not in result.output


class TestArtifactSuggestionsCommand:
    """Test 'notebooklm artifact suggestions' command."""

    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    def test_artifact_suggestions(self, runner, mock_auth_for_vcr, mock_context):
        """Get artifact suggestions works with real client."""
        result = runner.invoke(cli, ["artifact", "suggestions"])
        assert_command_success(result)
