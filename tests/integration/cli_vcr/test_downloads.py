"""CLI integration tests for download commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import json

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import ARTIFACT_NOTEBOOK_ID, VCR_READONLY_NOTEBOOK_ID
from .conftest import (
    notebooklm_vcr,
    skip_no_cassettes,
)

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestDownloadCommands:
    """Test 'notebooklm download' commands."""

    @pytest.mark.parametrize(
        ("command", "filename", "cassette", "extra_args"),
        [
            ("quiz", "quiz.json", "artifacts_download_quiz.yaml", []),
            ("quiz", "quiz.md", "artifacts_download_quiz_markdown.yaml", ["--format", "markdown"]),
            ("flashcards", "flashcards.json", "artifacts_download_flashcards.yaml", []),
            (
                "flashcards",
                "flashcards.md",
                "artifacts_download_flashcards_markdown.yaml",
                ["--format", "markdown"],
            ),
            ("report", "report.md", "artifacts_download_report.yaml", []),
            ("mind-map", "mindmap.json", "artifacts_download_mind_map.yaml", []),
            ("data-table", "data.csv", "artifacts_download_data_table.yaml", []),
        ],
    )
    def test_download(
        self,
        runner,
        mock_auth_for_vcr,
        mock_context,
        tmp_path,
        command,
        filename,
        cassette,
        extra_args,
    ):
        """Download commands genuinely succeed: exit 0 AND the file is written.

        Each ``artifacts_download_*.yaml`` cassette records a *single*
        ``LIST_ARTIFACTS`` (/ ``GET_NOTES_AND_MIND_MAPS``) interaction. Before
        issue #1488 the command listed artifacts twice (once in the ``_app``
        executor to select, once inside ``download_<x>`` to re-find), so the
        second list hit no recorded interaction and the command exited 1 with no
        file written — which the old ``assert_command_success`` (``allow_no_context``
        default) masked. These assertions now fail loud if the download regresses:
        a re-introduced double-list would exit 1 (caught by ``== 0``) and write
        nothing (caught by ``output_file.exists()``).
        """
        output_file = tmp_path / filename
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(
                cli,
                [
                    "download",
                    command,
                    "-n",
                    VCR_READONLY_NOTEBOOK_ID,
                    *extra_args,
                    str(output_file),
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.output}"
            assert output_file.exists(), f"Output file not written: {result.output}"

    def test_download_mind_map_interactive(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """`download mind-map <interactive_id>` exports the interactive map's tree.

        Reuses the interactive recording (``mind_maps_interactive.yaml``,
        ``ARTIFACT_NOTEBOOK_ID`` / artifact ``47523923``) captured for the
        API-level ``client.mind_maps`` tests. The CLI download flow now lists
        studio artifacts only **once** — the ``_app`` executor selects the id and
        threads the already-fetched raw rows into ``download_mind_map`` so its
        interactive branch does not re-list (issue #1488) — so the single
        recorded ``LIST_ARTIFACTS`` interaction replays without
        ``allow_playback_repeats``. The tree itself comes from the real
        ``GET_INTERACTIVE_HTML`` (``[0][9][3]``) response in the cassette
        (issue #1256).
        """
        nb = ARTIFACT_NOTEBOOK_ID
        art_id = "47523923"
        output_file = tmp_path / "interactive_mindmap.json"
        with notebooklm_vcr.use_cassette("mind_maps_interactive.yaml"):
            result = runner.invoke(
                cli, ["download", "mind-map", "-n", nb, "-a", art_id, str(output_file)]
            )
            assert result.exit_code == 0, f"Command failed: {result.output}"
        assert output_file.exists(), f"Output file not written: {result.output}"
        data = json.loads(output_file.read_text(encoding="utf-8"))
        assert "name" in data  # a {"name", "children"} mind-map node tree
