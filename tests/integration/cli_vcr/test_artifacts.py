"""CLI integration tests for artifact commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


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
        kind. T8.C6 / audit-I21.
        """
        # T8.C6: only the INFOGRAPHIC + DATA_TABLE rows opt into ``--json``.
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


class TestArtifactSuggestionsCommand:
    """Test 'notebooklm artifact suggestions' command."""

    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    def test_artifact_suggestions(self, runner, mock_auth_for_vcr, mock_context):
        """Get artifact suggestions works with real client."""
        result = runner.invoke(cli, ["artifact", "suggestions"])
        assert_command_success(result)
