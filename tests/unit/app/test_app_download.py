"""Unit tests for the transport-neutral ``notebooklm._app.download`` core.

These pin the relocated download business logic at the ``_app`` boundary
(independent of the Click adapter):

* the pure :func:`select_artifact` / :func:`artifact_title_to_filename`
  helpers (filter → count → select, filename sanitization + dedup) — moved
  from the former multi-artifact CLI download coverage (they are *defined*
  here and only re-exported via ``cli.download_helpers``);
* :func:`build_download_plan` flag-conflict validation + format-extension
  resolution + notebook-required hook;
* :func:`execute_download` against a ``MagicMock`` facade: no-artifacts,
  single dry-run, single download, single-file conflict, ``--all`` dispatch,
  partial-failure exit policy, and the injected resolver seams.

No Click / ``CliRunner`` — every test calls the ``_app`` function directly. The
CLI-shaped ``--json`` envelope / exit-code rendering stays in
``tests/unit/cli/test_download*.py``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.download import (
    FORMAT_EXTENSIONS,
    DownloadOutcome,
    DownloadPlan,
    DownloadPlanValidationError,
    DownloadTypeSpec,
    _resolve_format_extension,
    artifact_title_to_filename,
    build_download_plan,
    execute_download,
    select_artifact,
)
from notebooklm.types import Artifact, ArtifactType

# ---------------------------------------------------------------------------
# Pure artifact-selection logic (Filter → Count → Select).
#
# Moved from the former multi-artifact CLI download coverage: these call
# ``select_artifact`` directly (no CliRunner), and the function is defined in
# ``_app.download`` — the CLI only re-exports it.
# ---------------------------------------------------------------------------


class TestArtifactSelection:
    """Tests for artifact selection logic (Filter → Count → Select)."""

    def test_filter_then_select_latest(self):
        """Should apply name filter BEFORE selecting latest."""
        artifacts = [
            {"id": "a1", "title": "Debate Round 1", "created_at": 1000},
            {"id": "a2", "title": "Meeting Notes", "created_at": 2000},
            {"id": "a3", "title": "Debate Round 3", "created_at": 3000},  # Latest "debate"
            {"id": "a4", "title": "Debate Round 2", "created_at": 2500},
            {"id": "a5", "title": "Overview", "created_at": 4000},  # Latest overall
        ]

        selected, reason = select_artifact(artifacts, latest=True, name="debate")

        # Should select a3 (latest of the 3 "debate" matches, NOT a5 which is latest overall)
        assert selected["id"] == "a3"
        assert selected["title"] == "Debate Round 3"
        assert reason == "latest of 3 artifacts"

    def test_filter_then_select_earliest(self):
        """Should apply name filter BEFORE selecting earliest."""
        artifacts = [
            {"id": "a1", "title": "Introduction", "created_at": 1000},  # Earliest overall
            {"id": "a2", "title": "Chapter 2", "created_at": 3000},
            {"id": "a3", "title": "Chapter 1", "created_at": 2000},  # Earliest "chapter"
            {"id": "a4", "title": "Chapter 3", "created_at": 4000},
            {"id": "a5", "title": "Conclusion", "created_at": 5000},
        ]

        selected, reason = select_artifact(artifacts, latest=False, earliest=True, name="chapter")

        # Should select a3 (earliest of the 3 "chapter" matches, NOT a1)
        assert selected["id"] == "a3"
        assert selected["title"] == "Chapter 1"
        assert reason == "earliest of 3 artifacts"

    def test_select_latest_without_filter(self):
        """Should select latest when no filter applied."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 3000},  # Latest
            {"id": "a3", "title": "Audio 3", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, latest=True)

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "latest of 3 artifacts"

    def test_select_earliest_without_filter(self):
        """Should select earliest when no filter applied."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 3000},
            {"id": "a2", "title": "Audio 2", "created_at": 1000},  # Earliest
            {"id": "a3", "title": "Audio 3", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, latest=False, earliest=True)

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "earliest of 3 artifacts"

    def test_select_by_artifact_id(self):
        """Should select by exact artifact ID."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
            {"id": "a3", "title": "Audio 3", "created_at": 3000},
        ]

        selected, reason = select_artifact(artifacts, artifact_id="a2")

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "matched by ID: a2"

    def test_artifact_id_not_found(self):
        """Should raise ValueError if artifact ID not found."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="Artifact nonexistent not found"):
            select_artifact(artifacts, artifact_id="nonexistent")

    def test_name_filter_no_matches(self):
        """Should raise ValueError if name filter matches nothing."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="No artifacts matching 'nonexistent'"):
            select_artifact(artifacts, name="nonexistent")

    def test_name_filter_case_insensitive(self):
        """Should perform case-insensitive name matching."""
        artifacts = [
            {"id": "a1", "title": "CHAPTER ONE", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, name="chapter")

        assert selected["id"] == "a1"
        assert reason == "matched by name"

    def test_single_artifact_without_filter(self):
        """Should select single artifact with appropriate reason."""
        artifacts = [
            {"id": "a1", "title": "Only One", "created_at": 1000},
        ]

        selected, reason = select_artifact(artifacts)

        assert selected["id"] == "a1"
        assert reason == "only artifact"

    def test_single_artifact_with_name_match(self):
        """Should select single artifact matching name filter."""
        artifacts = [
            {"id": "a1", "title": "Chapter 1", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, name="chapter")

        assert selected["id"] == "a1"
        assert reason == "matched by name"

    def test_no_artifacts_error(self):
        """Should raise ValueError if no artifacts provided."""
        with pytest.raises(ValueError, match="No artifacts found"):
            select_artifact([])

    def test_latest_and_earliest_conflict(self):
        """Should raise ValueError if both latest and earliest specified."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
        ]

        with pytest.raises(ValueError, match="Cannot specify both"):
            select_artifact(artifacts, latest=True, earliest=True)


class TestFilenameGeneration:
    """Tests for artifact filename generation."""

    def test_basic_filename_generation(self):
        """Should generate safe filename from title."""
        filename = artifact_title_to_filename("My Audio", ".mp3", set())

        assert filename == "My Audio.mp3"

    def test_sanitize_invalid_characters(self):
        """Should replace invalid filesystem characters."""
        filename = artifact_title_to_filename('Audio: Part 1 / "Main"', ".mp3", set())

        # Invalid chars (: / ") should be replaced with _
        assert "/" not in filename
        assert ":" not in filename
        assert '"' not in filename
        assert filename == "Audio_ Part 1 _ _Main_.mp3"

    def test_handle_duplicates(self):
        """Should add (2), (3) suffixes for duplicates."""
        existing = {"Overview.mp3"}

        filename1 = artifact_title_to_filename("Overview", ".mp3", existing)
        assert filename1 == "Overview (2).mp3"

        existing.add(filename1)
        filename2 = artifact_title_to_filename("Overview", ".mp3", existing)
        assert filename2 == "Overview (3).mp3"

    def test_empty_title_fallback(self):
        """Should use 'untitled' for empty/whitespace titles."""
        filename1 = artifact_title_to_filename("", ".mp3", set())
        assert filename1 == "untitled.mp3"

        filename2 = artifact_title_to_filename("   ", ".mp3", set())
        assert filename2 == "untitled.mp3"

    def test_strip_leading_trailing_whitespace(self):
        """Should strip leading/trailing whitespace and dots."""
        filename = artifact_title_to_filename("  Audio Title  ", ".mp3", set())
        assert filename == "Audio Title.mp3"

        filename = artifact_title_to_filename("...Title...", ".mp3", set())
        assert filename == "Title.mp3"

    def test_truncate_long_titles(self):
        """Should truncate very long titles."""
        long_title = "A" * 300
        filename = artifact_title_to_filename(long_title, ".mp3", set())

        # Should be truncated (max 240 - 7 for duplicate suffix reserve = 233)
        assert len(filename) < 250
        assert filename.endswith(".mp3")

    def test_directory_no_extension(self):
        """Should handle directory-type artifacts (no extension)."""
        filename = artifact_title_to_filename("My Presentation", "", set())
        assert filename == "My Presentation"

    def test_duplicate_tracking_across_calls(self):
        """Should track duplicates correctly across multiple calls."""
        existing = set()

        f1 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f1)
        assert f1 == "Overview.mp3"

        f2 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f2)
        assert f2 == "Overview (2).mp3"

        f3 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f3)
        assert f3 == "Overview (3).mp3"

        # Verify all are unique
        assert len(existing) == 3


class TestIntegrationScenarios:
    """Integration scenarios combining selection and filename generation."""

    def test_download_all_with_duplicates_scenario(self):
        """Simulate downloading all artifacts with duplicate names."""
        artifacts = [
            {"id": "a1", "title": "Overview", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
            {"id": "a3", "title": "Overview", "created_at": 3000},
        ]

        existing_names = set()
        filenames = []

        for artifact in artifacts:
            filename = artifact_title_to_filename(artifact["title"], ".mp3", existing_names)
            existing_names.add(filename)
            filenames.append(filename)

        assert sorted(filenames) == ["Overview (2).mp3", "Overview (3).mp3", "Overview.mp3"]

    @pytest.mark.asyncio
    async def test_download_all_with_name_filter_drives_production_filter(self, tmp_path):
        """``--all --name`` positive filtering is exercised through the real
        :func:`execute_download` path (not a hand-rolled list comprehension).

        Drives the production ``_execute_download_all`` name filter so a
        regression in the case-insensitive substring match would fail here.
        """
        facade = _make_facade(
            artifacts=[
                _make_artifact("a1", "Chapter 1"),
                _make_artifact("a2", "Overview"),
                _make_artifact("a3", "Chapter 2"),
                _make_artifact("a4", "Summary"),
            ],
        )
        plan = build_download_plan(
            _AUDIO_SPEC,
            {
                "notebook_id": "nb_1",
                "download_all": True,
                "dry_run": True,  # preview only — no disk writes
                "name": "chapter",
            },
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        # Exactly the two "chapter" artifacts survive the production filter.
        assert result.outcome is DownloadOutcome.ALL_DRY_RUN
        assert result.count == 2
        titles = sorted(a["title"] for a in result.artifacts)
        assert titles == ["Chapter 1", "Chapter 2"]

    def test_latest_of_filtered_artifacts_scenario(self):
        """Simulate selecting latest of filtered artifacts."""
        artifacts = [
            {"id": "a1", "title": "Debate Round 1", "created_at": 1000},
            {"id": "a2", "title": "Meeting Notes", "created_at": 2000},
            {"id": "a3", "title": "Debate Round 3", "created_at": 3000},
            {"id": "a4", "title": "Debate Round 2", "created_at": 2500},
            {"id": "a5", "title": "Overview", "created_at": 4000},
        ]

        # This is the key test: Filter → Count → Select
        selected, reason = select_artifact(artifacts, latest=True, name="debate")

        # Should get the latest of the "debate" matches (created_at=3000)
        # NOT the latest overall (created_at=4000)
        assert selected["id"] == "a3"
        assert selected["created_at"] == 3000
        assert "latest of 3 artifacts" in reason


# ---------------------------------------------------------------------------
# Test fixtures: realistic DownloadTypeSpec rows + artifact builders.
# ---------------------------------------------------------------------------


_AUDIO_SPEC = DownloadTypeSpec(
    name="audio",
    kind=ArtifactType.AUDIO,
    extension=".mp3",
    default_dir="./audio",
    download_attr="download_audio",
    help_summary="",
    help_examples="",
)

# Slide-deck: forward_format_only_if_set, slide_format param name, pdf default.
_SLIDE_SPEC = DownloadTypeSpec(
    name="slide-deck",
    kind=ArtifactType.SLIDE_DECK,
    extension=".pdf",
    default_dir="./slide-decks",
    download_attr="download_slide_deck",
    help_summary="",
    help_examples="",
    format_choices=("pdf", "pptx"),
    format_default="pdf",
    format_extension_map={"pdf": ".pdf", "pptx": ".pptx"},
    format_kwarg="output_format",
    format_param_name="slide_format",
    forward_format_only_if_set=True,
)

# Quiz: always-forward format kwarg, output_format param name, json default.
_QUIZ_SPEC = DownloadTypeSpec(
    name="quiz",
    kind=ArtifactType.QUIZ,
    extension=".json",
    default_dir="./quiz",
    download_attr="download_quiz",
    help_summary="",
    help_examples="",
    format_choices=("json", "markdown", "html"),
    format_default="json",
    format_extension_map=dict(FORMAT_EXTENSIONS),
    format_kwarg="output_format",
)


def _make_artifact(id: str, title: str, artifact_type: int = 1, created_at: int = 1234567890):
    return Artifact(
        id=id,
        title=title,
        _artifact_type=artifact_type,
        status=3,  # COMPLETED
        created_at=datetime.fromtimestamp(created_at),
    )


def _passthrough_notebook_resolver() -> AsyncMock:
    """Resolver that returns the id unchanged (the CLI injects the real one)."""
    return AsyncMock(side_effect=lambda nb_id: nb_id)


def _make_facade(*, artifacts: list, download_return: str | None = "/out/path") -> MagicMock:
    facade = MagicMock()
    facade.artifacts = MagicMock()
    facade.artifacts.list = AsyncMock(return_value=artifacts)
    # The executor lists once via ``_list_for_download`` (``list`` + raw rows;
    # issue #1488). On a bare MagicMock this would auto-spawn a non-awaitable
    # child; wire it to return the typed list plus empty raw rows (the mocked
    # ``download_<x>`` never consumes the raw rows here).
    facade.artifacts._list_for_download = AsyncMock(return_value=(artifacts, [], []))
    facade.artifacts.download_audio = AsyncMock(return_value=download_return)
    facade.artifacts.download_slide_deck = AsyncMock(return_value=download_return)
    facade.artifacts.download_quiz = AsyncMock(return_value=download_return)
    return facade


def _artifact_resolver_identity(_artifacts, partial: str) -> str:
    """Stand-in for the CLI's partial-artifact-id resolver: returns it unchanged."""
    return partial


# ---------------------------------------------------------------------------
# build_download_plan — flag-conflict validation (SPLIT from CLI exit-code tests).
# ---------------------------------------------------------------------------


class TestBuildDownloadPlanValidation:
    def test_force_and_no_clobber_conflict(self):
        with pytest.raises(DownloadPlanValidationError, match="--force and --no-clobber"):
            build_download_plan(
                _AUDIO_SPEC, {"force": True, "no_clobber": True, "notebook_id": "n"}
            )

    def test_latest_and_earliest_conflict(self):
        with pytest.raises(DownloadPlanValidationError, match="--latest and --earliest"):
            build_download_plan(_AUDIO_SPEC, {"latest": True, "earliest": True, "notebook_id": "n"})

    def test_all_and_artifact_conflict(self):
        with pytest.raises(DownloadPlanValidationError, match="--all and --artifact"):
            build_download_plan(
                _AUDIO_SPEC,
                {"download_all": True, "artifact_id": "art_1", "notebook_id": "n"},
            )

    def test_validation_error_carries_validation_code(self):
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(
                _AUDIO_SPEC, {"force": True, "no_clobber": True, "notebook_id": "n"}
            )
        assert exc.value.code == "VALIDATION_ERROR"

    def test_missing_notebook_id_raises_by_default_hook(self):
        """The default ``_identity_notebook`` hook fails loud on a blank id."""
        with pytest.raises(DownloadPlanValidationError, match="notebook_id is required"):
            build_download_plan(_AUDIO_SPEC, {"notebook_id": None})

    def test_notebook_required_hook_is_applied(self):
        """The injected ``notebook_required`` hook can rewrite the id (env/context fallback)."""
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": None},
            notebook_required=lambda _raw: "from_context",
        )
        assert plan.notebook_id == "from_context"

    def test_plan_captures_flags_and_cwd(self):
        cwd = Path("/tmp/somewhere")
        plan = build_download_plan(
            _AUDIO_SPEC,
            {
                "notebook_id": "nb_1",
                "output_path": "out.mp3",
                "latest": True,
                "dry_run": True,
                "name": "chapter",
            },
            cwd=cwd,
        )
        assert isinstance(plan, DownloadPlan)
        assert plan.notebook_id == "nb_1"
        assert plan.output_path == "out.mp3"
        assert plan.latest is True
        assert plan.dry_run is True
        assert plan.name == "chapter"
        assert plan.cwd == cwd


# ---------------------------------------------------------------------------
# Format-extension resolution (SPLIT from the characterization --format snapshots).
# ---------------------------------------------------------------------------


class TestFormatExtensionResolution:
    def test_no_format_choices_returns_spec_extension(self):
        ext, warnings = _resolve_format_extension(_AUDIO_SPEC, None, "")
        assert ext == ".mp3"
        assert warnings == ()

    def test_slide_deck_pdf_default_extension(self):
        ext, warnings = _resolve_format_extension(_SLIDE_SPEC, None, "pdf")
        assert ext == ".pdf"
        assert warnings == ()

    def test_slide_deck_pptx_override_extension(self):
        ext, warnings = _resolve_format_extension(_SLIDE_SPEC, None, "pptx")
        assert ext == ".pptx"
        assert warnings == ()

    def test_quiz_markdown_extension(self):
        ext, _ = _resolve_format_extension(_QUIZ_SPEC, None, "markdown")
        assert ext == ".md"

    def test_quiz_html_extension(self):
        ext, _ = _resolve_format_extension(_QUIZ_SPEC, None, "html")
        assert ext == ".html"

    def test_extension_mismatch_warning_for_explicit_output_path(self):
        ext, warnings = _resolve_format_extension(_SLIDE_SPEC, "deck.pdf", "pptx")
        assert ext == ".pptx"
        assert len(warnings) == 1
        assert "deck.pdf" in warnings[0]
        assert ".pptx" in warnings[0]

    def test_matching_output_path_no_warning(self):
        _ext, warnings = _resolve_format_extension(_SLIDE_SPEC, "deck.pptx", "pptx")
        assert warnings == ()

    def test_download_all_suppresses_mismatch_warning(self):
        """Under ``--all`` the output path is a directory, so no extension check."""
        _ext, warnings = _resolve_format_extension(
            _SLIDE_SPEC, "out-dir", "pptx", download_all=True
        )
        assert warnings == ()

    def test_build_plan_threads_format_choice_and_warning(self):
        plan = build_download_plan(
            _SLIDE_SPEC,
            {"notebook_id": "n", "slide_format": "pptx", "output_path": "deck.pdf"},
        )
        assert plan.format_choice == "pptx"
        assert plan.file_extension == ".pptx"
        assert plan.warnings  # mismatch warning queued


# ---------------------------------------------------------------------------
# execute_download — typed-outcome dispatch against a MagicMock facade.
# ---------------------------------------------------------------------------


class TestExecuteDownload:
    @pytest.mark.asyncio
    async def test_no_completed_artifacts(self):
        facade = _make_facade(artifacts=[])
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"})
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.NO_ARTIFACTS
        assert result.has_error
        assert "No completed audio" in result.error
        assert result.suggestion is not None

    @pytest.mark.asyncio
    async def test_single_dry_run_does_not_download(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "dry_run": True},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.SINGLE_DRY_RUN
        assert not result.has_error
        assert result.artifact["id"] == "a1"
        assert result.output_path.endswith("Only One.mp3")
        facade.artifacts.download_audio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_download_succeeds(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "Only One")],
            download_return=str(tmp_path / "Only One.mp3"),
        )
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"}, cwd=tmp_path)
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.SINGLE_DOWNLOADED
        assert not result.has_error
        assert result.artifact["id"] == "a1"
        facade.artifacts.download_audio.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_single_no_clobber_conflict_errors(self, tmp_path):
        existing = tmp_path / "Only One.mp3"
        existing.write_bytes(b"x")
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "no_clobber": True},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ERROR
        assert result.has_error
        assert "File exists" in result.error
        assert result.suggestion is not None
        facade.artifacts.download_audio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_download_exception_maps_to_error(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        facade.artifacts.download_audio = AsyncMock(side_effect=RuntimeError("boom"))
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"}, cwd=tmp_path)
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ERROR
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_all_dry_run_previews_all(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "First"), _make_artifact("a2", "Second")],
        )
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "dry_run": True},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ALL_DRY_RUN
        assert result.count == 2
        assert len(result.artifacts) == 2
        facade.artifacts.download_audio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_partial_failure_sets_is_failure(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "First"), _make_artifact("a2", "Second")],
        )
        call_count = {"n": 0}

        async def fake_download(_nb, output_path, artifact_id=None, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            Path(output_path).write_bytes(b"x")
            return output_path

        facade.artifacts.download_audio = AsyncMock(side_effect=fake_download)
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "output_path": str(tmp_path / "out")},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ALL_EXECUTED
        assert result.is_failure is True
        assert result.has_error  # ANY per-item failure surfaces a non-zero exit
        assert result.failed_count == 1
        assert result.succeeded_count == 1

    @pytest.mark.asyncio
    async def test_all_name_filter_no_match_errors(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "First")])
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "name": "nope"},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ERROR
        assert "No artifacts matching 'nope'" in result.error

    @pytest.mark.asyncio
    async def test_notebook_resolver_is_invoked(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        resolver = AsyncMock(return_value="resolved_nb")
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_partial"}, cwd=tmp_path)
        await execute_download(
            plan,
            facade,
            notebook_resolver=resolver,
            artifact_resolver=_artifact_resolver_identity,
        )
        resolver.assert_awaited_once_with("nb_partial")
        # The resolved id flows into the single ``_list_for_download`` call — the
        # executor lists once and threads the raw rows down (#1488), so it no
        # longer goes through the plain ``artifacts.list`` seam. Assert awaited
        # first, then the first positional arg (a ``spec.kind`` arg follows it).
        facade.artifacts._list_for_download.assert_awaited_once()
        assert facade.artifacts._list_for_download.call_args[0][0] == "resolved_nb"

    @pytest.mark.asyncio
    async def test_artifact_resolver_used_for_partial_id(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("audio_full_1", "Only One")],
        )
        resolver = MagicMock(return_value="audio_full_1")
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "artifact_id": "audio_f"},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=resolver,
        )
        resolver.assert_called_once()
        assert result.outcome is DownloadOutcome.SINGLE_DOWNLOADED

    @pytest.mark.asyncio
    async def test_fallback_without_seam_threads_no_prefetch(self, tmp_path):
        """A narrow facade exposing only ``.list()`` (no ``_list_for_download``)
        gets NO prefetch kwargs, so the bound ``download_<x>`` self-fetches as
        before. Regression for #1488: the seam-absent fallback must not bind
        ``artifacts_data=[]`` — that would suppress the method's self-fetch (it
        only fetches on ``is None``) and break an old-style ``download_<x>``
        whose signature lacks the new keyword-only param.
        """
        artifacts = [_make_artifact("audio_1", "Only One")]
        facade = MagicMock()
        # ``spec`` restricts the attribute set so
        # ``getattr(facade.artifacts, "_list_for_download", None)`` is ``None``,
        # exercising the fallback branch; ``download_audio`` takes no prefetch kwarg.
        facade.artifacts = MagicMock(spec=["list", "download_audio"])
        facade.artifacts.list = AsyncMock(return_value=artifacts)
        facade.artifacts.download_audio = AsyncMock(return_value="/out/audio.mp3")

        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "artifact_id": "audio_1"},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )

        assert result.outcome is DownloadOutcome.SINGLE_DOWNLOADED
        # Selection used the plain ``.list()`` seam (the only one available)...
        facade.artifacts.list.assert_awaited_once_with("nb_1")
        # ...and the bound download fn received NO prefetch kwarg, so it would
        # self-fetch (rather than being handed an empty list).
        _args, kwargs = facade.artifacts.download_audio.call_args
        assert "artifacts_data" not in kwargs
        assert "artifacts" not in kwargs
        assert "mind_maps" not in kwargs


def test_format_extensions_map_contract():
    """The shared format→extension table stays stable across adapters."""
    assert FORMAT_EXTENSIONS == {"json": ".json", "markdown": ".md", "html": ".html"}
