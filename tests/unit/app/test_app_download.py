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
    DownloadEvent,
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
from notebooklm.downloads import resolve_download_format
from notebooklm.exceptions import AuthError, RPCError, ValidationError
from notebooklm.types import (
    Artifact,
    ArtifactDownloadListing,
    ArtifactDownloadRequest,
    ArtifactDownloadSelection,
    ArtifactListingComponent,
    ArtifactListingFailure,
    ArtifactType,
)

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

    @pytest.mark.parametrize("name", ["istanbul", "istanbul notları", "İSTANBUL NOT"])
    def test_name_filter_dotted_capital_i(self, name):
        """The --name filter compares accent-insensitive keys (issue #2476)."""
        artifacts = [
            {"id": "a1", "title": "İstanbul notları", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, name=name)

        assert selected["id"] == "a1"
        assert reason == "matched by name"

    def test_name_filter_keeps_dotless_i_distinct(self):
        """Dotless ı is a different letter from i, so it must not match."""
        artifacts = [{"id": "a1", "title": "ılık çay", "created_at": 1000}]

        with pytest.raises(ValueError, match="No artifacts matching"):
            select_artifact(artifacts, name="ilik")

    def test_name_filter_combining_marks_only_is_a_miss(self):
        """A name of only combining marks must not match every title."""
        artifacts = [
            {"id": "a1", "title": "First", "created_at": 1000},
            {"id": "a2", "title": "Second", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="No artifacts matching"):
            select_artifact(artifacts, name="\u0301")

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

        with pytest.raises(DownloadPlanValidationError) as exc:
            select_artifact(artifacts, latest=True, earliest=True)
        assert exc.value.reason == "conflicting_selection_order"


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

    @pytest.mark.asyncio
    async def test_download_all_name_filter_matches_dotted_capital_i(self, tmp_path):
        """``--all --name istanbul`` keeps an ``İstanbul`` title (issue #2476)."""
        facade = _make_facade(
            artifacts=[
                _make_artifact("a1", "İstanbul notları"),
                _make_artifact("a2", "Overview"),
                _make_artifact("a3", "ılık çay"),
            ],
        )
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "dry_run": True, "name": "istanbul"},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ALL_DRY_RUN
        assert [a["title"] for a in result.artifacts] == ["İstanbul notları"]

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
    extension=".m4a",
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
    # The application needs exactly these public methods. Accessing a legacy
    # per-kind method, raw cache, or private list hook fails this strict double.
    facade.artifacts = MagicMock(spec_set=["prepare_downloads", "download"])

    async def prepare(request: ArtifactDownloadRequest) -> ArtifactDownloadListing:
        representation, output = resolve_download_format(request.kind, request.output_format)
        return ArtifactDownloadListing(
            tuple(
                ArtifactDownloadSelection(
                    notebook_id=request.notebook_id,
                    artifact_id=item.id,
                    kind=item.kind,
                    title=item.title,
                    created_at=item.created_at,
                    representation=representation,
                    extension=output.extension,
                    mime_type=output.mime_type,
                )
                for item in artifacts
                if item.kind == request.kind and item.is_completed
            ),
            is_complete=True,
        )

    facade.artifacts.prepare_downloads = AsyncMock(side_effect=prepare)
    facade.artifacts.download = AsyncMock(return_value=download_return)
    return facade


def _artifact_resolver_identity(_artifacts, partial: str) -> str:
    """Stand-in for the CLI's partial-artifact-id resolver: returns it unchanged."""
    return partial


# ---------------------------------------------------------------------------
# build_download_plan — flag-conflict validation (SPLIT from CLI exit-code tests).
# ---------------------------------------------------------------------------


class TestBuildDownloadPlanValidation:
    def test_force_and_no_clobber_conflict(self):
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(
                _AUDIO_SPEC, {"force": True, "no_clobber": True, "notebook_id": "n"}
            )
        assert exc.value.reason == "conflicting_overwrite_policy"

    def test_latest_and_earliest_conflict(self):
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(_AUDIO_SPEC, {"latest": True, "earliest": True, "notebook_id": "n"})
        assert exc.value.reason == "conflicting_selection_order"

    def test_all_and_artifact_conflict(self):
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(
                _AUDIO_SPEC,
                {"download_all": True, "artifact_id": "art_1", "notebook_id": "n"},
            )
        assert exc.value.reason == "all_with_artifact"

    def test_validation_error_carries_semantic_reason(self):
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(
                _AUDIO_SPEC, {"force": True, "no_clobber": True, "notebook_id": "n"}
            )
        assert exc.value.reason == "conflicting_overwrite_policy"
        assert "--" not in str(exc.value)

    def test_missing_notebook_id_raises_by_default_hook(self):
        """The default ``_identity_notebook`` hook fails loud on a blank id."""
        with pytest.raises(DownloadPlanValidationError) as exc:
            build_download_plan(_AUDIO_SPEC, {"notebook_id": None})
        assert exc.value.reason == "missing_notebook"

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
                "output_path": "out.m4a",
                "latest": True,
                "dry_run": True,
                "name": "chapter",
            },
            cwd=cwd,
        )
        assert isinstance(plan, DownloadPlan)
        assert plan.notebook_id == "nb_1"
        assert plan.output_path == "out.m4a"
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
        assert ext == ".m4a"
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
        assert warnings[0].output_path == "deck.pdf"
        assert warnings[0].expected_extension == ".pptx"
        assert warnings[0].format_choice == "pptx"

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
        assert result.failure is not None
        assert result.failure.reason == "no_artifacts"
        assert result.failure.artifact_type == "audio"

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
        assert result.output_path.endswith("Only One.m4a")
        facade.artifacts.download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_download_succeeds(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "Only One")],
            download_return=str(tmp_path / "Only One.m4a"),
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
        facade.artifacts.download.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_single_no_clobber_conflict_errors(self, tmp_path):
        existing = tmp_path / "Only One.m4a"
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
        assert result.failure is not None
        assert result.failure.reason == "file_exists"
        assert result.failure.path == str(existing)
        facade.artifacts.download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_download_exception_maps_to_error(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        facade.artifacts.download = AsyncMock(side_effect=RuntimeError("boom"))
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"}, cwd=tmp_path)
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ERROR
        assert result.failure is not None
        assert result.failure.reason == "download_failed"
        assert result.failure.detail == "boom"

    @pytest.mark.asyncio
    async def test_single_download_auth_error_is_not_swallowed(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("a1", "Only One")])
        facade.artifacts.download = AsyncMock(side_effect=AuthError("expired"))
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"}, cwd=tmp_path)

        with pytest.raises(AuthError, match="expired"):
            await execute_download(
                plan,
                facade,
                notebook_resolver=_passthrough_notebook_resolver(),
                artifact_resolver=_artifact_resolver_identity,
            )

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
        facade.artifacts.download.assert_not_awaited()

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

        facade.artifacts.download = AsyncMock(side_effect=fake_download)
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "output_path": str(tmp_path / "out")},
            cwd=tmp_path,
        )
        progress = MagicMock()
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
            progress=progress,
        )
        assert result.outcome is DownloadOutcome.ALL_EXECUTED
        assert result.is_failure is True
        assert result.has_error  # ANY per-item failure surfaces a non-zero exit
        assert result.failed_count == 1
        assert result.succeeded_count == 1
        assert [call.args[0] for call in progress.emit.call_args_list] == [
            DownloadEvent("ITEM_STARTED", index=1, total=2, title="First"),
            DownloadEvent("ITEM_STARTED", index=2, total=2, title="Second"),
        ]

    @pytest.mark.asyncio
    async def test_all_auth_failure_keeps_rows_and_typed_guidance(self, tmp_path):
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "First"), _make_artifact("a2", "Second")],
        )
        facade.artifacts.download = AsyncMock(
            side_effect=[AuthError("expired"), str(tmp_path / "out" / "Second.m4a")]
        )
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
        assert [row["status"] for row in result.artifacts] == ["failed", "downloaded"]
        assert result.failure is not None
        assert result.failure.reason == "authentication"
        assert result.failure.detail == "expired"

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
        assert result.failure is not None
        assert result.failure.reason == "name_not_found"
        assert result.failure.name == "nope"

    @pytest.mark.asyncio
    async def test_all_name_filter_combining_marks_only_errors(self, tmp_path):
        """``--all --name`` of only combining marks selects nothing, not everything."""
        facade = _make_facade(
            artifacts=[_make_artifact("a1", "First"), _make_artifact("a2", "Second")]
        )
        plan = build_download_plan(
            _AUDIO_SPEC,
            {"notebook_id": "nb_1", "download_all": True, "dry_run": True, "name": "\u0301"},
            cwd=tmp_path,
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.ERROR
        assert result.failure is not None
        assert result.failure.reason == "name_not_found"

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
        facade.artifacts.prepare_downloads.assert_awaited_once_with(
            ArtifactDownloadRequest("resolved_nb", ArtifactType.AUDIO)
        )

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
    async def test_download_receives_the_exact_prepared_identity(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("audio_1", "Only One")])
        request = ArtifactDownloadRequest("nb_1", ArtifactType.AUDIO)
        listing = await facade.artifacts.prepare_downloads(request)
        facade.artifacts.prepare_downloads = AsyncMock(return_value=listing)
        plan = build_download_plan(
            _AUDIO_SPEC, {"notebook_id": "nb_1", "artifact_id": "audio_1"}, cwd=tmp_path
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.SINGLE_DOWNLOADED
        args, kwargs = facade.artifacts.download.call_args
        assert args[0] is listing.selections[0]
        assert args[1] == str(tmp_path / "Only One.m4a")
        assert kwargs == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "flags",
        [{}, {"latest": True}, {"name": "One"}, {"artifact_id": "audio"}, {"download_all": True}],
    )
    async def test_partial_listing_cannot_authorize_nonexact_selection(self, tmp_path, flags):
        facade = _make_facade(artifacts=[_make_artifact("audio_1", "Only One")])
        listing = await facade.artifacts.prepare_downloads(
            ArtifactDownloadRequest("nb_1", ArtifactType.AUDIO)
        )
        failure = ArtifactListingFailure(
            ArtifactListingComponent.NOTE_BACKED_MIND_MAPS, "RPCError", "unavailable"
        )
        facade.artifacts.prepare_downloads = AsyncMock(
            return_value=ArtifactDownloadListing(listing.selections, False, (failure,))
        )
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1", **flags}, cwd=tmp_path)
        with pytest.raises(RPCError, match="incomplete"):
            await execute_download(
                plan,
                facade,
                notebook_resolver=_passthrough_notebook_resolver(),
                artifact_resolver=_artifact_resolver_identity,
            )
        facade.artifacts.download.assert_not_awaited()
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    async def test_exact_positive_identity_remains_usable_when_other_backing_failed(self, tmp_path):
        facade = _make_facade(artifacts=[_make_artifact("audio_1", "Only One")])
        listing = await facade.artifacts.prepare_downloads(
            ArtifactDownloadRequest("nb_1", ArtifactType.AUDIO)
        )
        failure = ArtifactListingFailure(
            ArtifactListingComponent.NOTE_BACKED_MIND_MAPS, "RPCError", "unavailable"
        )
        facade.artifacts.prepare_downloads = AsyncMock(
            return_value=ArtifactDownloadListing(listing.selections, False, (failure,))
        )
        plan = build_download_plan(
            _AUDIO_SPEC, {"notebook_id": "nb_1", "artifact_id": "audio_1"}, cwd=tmp_path
        )
        result = await execute_download(
            plan,
            facade,
            notebook_resolver=_passthrough_notebook_resolver(),
            artifact_resolver=_artifact_resolver_identity,
        )
        assert result.outcome is DownloadOutcome.SINGLE_DOWNLOADED
        assert facade.artifacts.download.call_args.args[0] is listing.selections[0]

    @pytest.mark.asyncio
    async def test_inconsistent_prepared_extension_fails_before_writing(self, tmp_path):
        from dataclasses import replace

        facade = _make_facade(artifacts=[_make_artifact("audio_1", "Only One")])
        listing = await facade.artifacts.prepare_downloads(
            ArtifactDownloadRequest("nb_1", ArtifactType.AUDIO)
        )
        wrong = replace(listing.selections[0], extension=".mp3")
        facade.artifacts.prepare_downloads = AsyncMock(
            return_value=ArtifactDownloadListing((wrong,), True)
        )
        plan = build_download_plan(_AUDIO_SPEC, {"notebook_id": "nb_1"}, cwd=tmp_path)
        with pytest.raises(ValidationError, match="file extension"):
            await execute_download(
                plan,
                facade,
                notebook_resolver=_passthrough_notebook_resolver(),
                artifact_resolver=_artifact_resolver_identity,
            )
        facade.artifacts.download.assert_not_awaited()


def test_format_extensions_map_contract():
    """The shared format→extension table stays stable across adapters."""
    assert FORMAT_EXTENSIONS == {"json": ".json", "markdown": ".md", "html": ".html"}
