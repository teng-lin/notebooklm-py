"""Unit tests for the transport-neutral ``notebooklm._app.generate_plans`` core.

These pin the relocated generate plan-building business logic at the ``_app``
boundary (independent of the Click adapter):

* :func:`build_generation_plan` dispatch + the per-kind builders (enum/format
  maps, language resolution, ``source_ids`` threading);
* the validation rules the CLI characterization snapshots reach only through
  ``CliRunner`` exit-code assertions (SPLIT): cinematic-video flag enforcement,
  ``--style custom`` / ``--style-prompt`` coupling, the report "smart custom"
  format coercion, and the ``--append`` no-op warning;
* :class:`GenerationPlanValidationError` shape (subclasses ``ValidationError``).

No Click / ``CliRunner`` — every test calls ``build_generation_plan`` directly.
The CLI ``--json`` / exit-code / stderr-routing assertions stay in
``tests/unit/cli/test_generate.py`` and
``tests/unit/cli/test_generate_characterization.py``.
"""

from __future__ import annotations

import pytest

from notebooklm._app.generate_plans import (
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)


def _audio_args(**overrides):
    base = {
        "notebook_id": "nb_1",
        "audio_format": "deep-dive",
        "audio_length": "default",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_generation_plan — common-field threading + dispatch.
# ---------------------------------------------------------------------------


class TestBuildGenerationPlanCommon:
    def test_unknown_kind_raises(self):
        with pytest.raises(GenerationPlanValidationError, match="Unknown generation kind"):
            build_generation_plan("not-a-kind", {"notebook_id": "nb_1"})

    def test_validation_error_is_validation_subclass(self):
        with pytest.raises(ValidationError):
            build_generation_plan("not-a-kind", {"notebook_id": "nb_1"})

    def test_validation_error_default_code(self):
        with pytest.raises(GenerationPlanValidationError) as exc:
            build_generation_plan("not-a-kind", {"notebook_id": "nb_1"})
        assert exc.value.code == "VALIDATION_ERROR"

    def test_common_fields_threaded(self):
        plan = build_generation_plan(
            "audio",
            _audio_args(
                description="desc",
                source_ids=["s1", "s2"],
                wait=True,
                timeout=99.0,
                interval=4.0,
                max_retries=5,
                json_output=True,
            ),
        )
        assert isinstance(plan, GenerationPlan)
        assert plan.notebook_id == "nb_1"
        assert plan.description == "desc"
        assert plan.source_ids == ("s1", "s2")
        assert plan.wait is True
        assert plan.timeout == 99.0
        assert plan.interval == 4.0
        assert plan.max_retries == 5
        assert plan.json_output is True

    def test_default_language_resolver_falls_back_to_en(self):
        plan = build_generation_plan("audio", _audio_args(language=None))
        assert plan.language == "en"

    def test_injected_language_resolver_used(self):
        plan = build_generation_plan(
            "audio",
            _audio_args(language="raw"),
            language_resolver=lambda _lang: "resolved_hl",
        )
        assert plan.language == "resolved_hl"


# ---------------------------------------------------------------------------
# Per-kind enum/format mapping.
# ---------------------------------------------------------------------------


class TestPerKindEnumMapping:
    def test_audio_enum_mapping(self):
        plan = build_generation_plan(
            "audio", _audio_args(audio_format="brief", audio_length="long")
        )
        assert plan.kind == "audio"
        assert plan.params["audio_format"] == AudioFormat.BRIEF
        assert plan.params["audio_length"] == AudioLength.LONG

    def test_video_enum_mapping(self):
        plan = build_generation_plan(
            "video",
            {
                "notebook_id": "nb_1",
                "video_format": "explainer",
                "style": "whiteboard",
            },
        )
        assert plan.kind == "video"
        assert plan.params["video_format"] == VideoFormat.EXPLAINER
        assert plan.params["video_style"] == VideoStyle.WHITEBOARD
        assert plan.params["style_prompt"] is None

    def test_video_short_format_mapping(self):
        # "short" rides the standard video path (not cinematic), but has a fixed
        # style, so it is generated without an explicit style (#1805).
        plan = build_generation_plan(
            "video",
            {
                "notebook_id": "nb_1",
                "video_format": "short",
            },
        )
        assert plan.kind == "video"
        assert plan.params["video_format"] == VideoFormat.SHORT
        assert plan.params["video_style"] == VideoStyle.AUTO_SELECT

    def test_slide_deck_enum_mapping(self):
        plan = build_generation_plan(
            "slide-deck",
            {"notebook_id": "nb_1", "deck_format": "presenter", "deck_length": "short"},
        )
        assert plan.params["slide_format"] == SlideDeckFormat.PRESENTER_SLIDES
        assert plan.params["slide_length"] == SlideDeckLength.SHORT

    def test_quiz_enum_mapping(self):
        plan = build_generation_plan(
            "quiz",
            {"notebook_id": "nb_1", "quantity": "more", "difficulty": "hard"},
        )
        assert plan.params["quantity"] == QuizQuantity.MORE
        assert plan.params["difficulty"] == QuizDifficulty.HARD
        # Quiz does not accept a language.
        assert plan.language is None

    def test_flashcards_enum_mapping(self):
        plan = build_generation_plan(
            "flashcards",
            {"notebook_id": "nb_1", "quantity": "fewer", "difficulty": "easy"},
        )
        assert plan.params["quantity"] == QuizQuantity.FEWER
        assert plan.params["difficulty"] == QuizDifficulty.EASY
        assert plan.language is None

    def test_infographic_enum_mapping(self):
        plan = build_generation_plan(
            "infographic",
            {
                "notebook_id": "nb_1",
                "orientation": "portrait",
                "detail": "detailed",
                "style": "professional",
            },
        )
        assert plan.params["orientation"] == InfographicOrientation.PORTRAIT
        assert plan.params["detail_level"] == InfographicDetail.DETAILED
        assert plan.params["style"] == InfographicStyle.PROFESSIONAL

    def test_data_table_requires_no_enum_but_threads_language(self):
        plan = build_generation_plan(
            "data-table",
            {"notebook_id": "nb_1", "description": "Compare", "language": "fr"},
            language_resolver=lambda lang: lang or "en",
        )
        assert plan.kind == "data-table"
        assert plan.params == {}
        assert plan.language == "fr"

    def test_revise_slide_never_resolves_sources(self):
        plan = build_generation_plan(
            "revise-slide",
            {
                "notebook_id": "nb_1",
                "description": "move title",
                "artifact_id": "art_1",
                "slide_index": "2",
                "source_ids": ["s1"],  # ignored
            },
        )
        assert plan.kind == "revise-slide"
        assert plan.source_ids == ()
        assert plan.language is None
        assert plan.params["artifact_id"] == "art_1"
        assert plan.params["slide_index"] == 2  # coerced to int
        assert plan.params["prompt"] == "move title"


# ---------------------------------------------------------------------------
# Video / cinematic-video flag enforcement (SPLIT from CLI exit-code snapshots).
# ---------------------------------------------------------------------------


class TestVideoFlagValidation:
    def test_cinematic_video_rejects_style_prompt(self):
        with pytest.raises(
            GenerationPlanValidationError,
            match="--style-prompt cannot be used with cinematic video",
        ):
            build_generation_plan(
                "video",
                {"notebook_id": "nb_1", "video_format": "cinematic", "style_prompt": "foo"},
            )

    def test_style_custom_requires_style_prompt(self):
        with pytest.raises(
            GenerationPlanValidationError, match="--style custom requires --style-prompt"
        ):
            build_generation_plan("video", {"notebook_id": "nb_1", "style": "custom"})

    def test_style_prompt_requires_style_custom(self):
        with pytest.raises(
            GenerationPlanValidationError, match="--style-prompt requires --style custom"
        ):
            build_generation_plan("video", {"notebook_id": "nb_1", "style_prompt": "foo"})

    def test_style_custom_with_prompt_succeeds(self):
        plan = build_generation_plan(
            "video",
            {"notebook_id": "nb_1", "style": "custom", "style_prompt": "  painterly  "},
        )
        assert plan.params["video_style"] == VideoStyle.CUSTOM
        assert plan.params["style_prompt"] == "painterly"  # stripped

    def test_short_rejects_explicit_style(self):
        # #1805: short has a fixed style; an explicit style is rejected, not ignored.
        with pytest.raises(
            GenerationPlanValidationError,
            match="cannot be used with --format short",
        ):
            build_generation_plan(
                "video",
                {"notebook_id": "nb_1", "video_format": "short", "style": "anime"},
            )

    def test_short_rejects_style_prompt(self):
        with pytest.raises(
            GenerationPlanValidationError,
            match="cannot be used with --format short",
        ):
            build_generation_plan(
                "video",
                {"notebook_id": "nb_1", "video_format": "short", "style_prompt": "foo"},
            )

    def test_short_style_custom_yields_short_message_not_custom_rule(self):
        # Ordering precedence: the short reject runs before the generic
        # "custom requires --style-prompt" rule, so short's message wins.
        with pytest.raises(
            GenerationPlanValidationError,
            match="cannot be used with --format short",
        ):
            build_generation_plan(
                "video",
                {"notebook_id": "nb_1", "video_format": "short", "style": "custom"},
            )

    def test_short_with_default_auto_style_succeeds(self):
        # No explicit style (auto default) is fine — short just uses its fixed style.
        plan = build_generation_plan(
            "video",
            {"notebook_id": "nb_1", "video_format": "short"},
        )
        assert plan.params["video_format"] == VideoFormat.SHORT
        assert plan.params["video_style"] == VideoStyle.AUTO_SELECT

    def test_cinematic_alias_rejects_non_cinematic_format(self):
        # parameter_explicit reports the format flag was passed on the CLI.
        with pytest.raises(
            GenerationPlanValidationError,
            match="--format must be 'cinematic' for the cinematic-video subcommand",
        ):
            build_generation_plan(
                "cinematic-video",
                {"notebook_id": "nb_1", "video_format": "explainer"},
                lambda name: name == "video_format",
            )

    def test_cinematic_alias_forces_cinematic_kind(self):
        plan = build_generation_plan(
            "cinematic-video",
            {"notebook_id": "nb_1", "description": "scene"},
        )
        assert plan.kind == "cinematic-video"
        assert plan.params == {}

    def test_cinematic_default_timeout_applied_when_not_explicit(self):
        plan = build_generation_plan(
            "cinematic-video",
            {"notebook_id": "nb_1"},
            lambda _name: False,  # timeout not explicit
        )
        assert plan.timeout == 3600.0

    def test_cinematic_respects_explicit_timeout(self):
        plan = build_generation_plan(
            "cinematic-video",
            {"notebook_id": "nb_1", "timeout": 120.0},
            lambda name: name == "timeout",
        )
        assert plan.timeout == 120.0


# ---------------------------------------------------------------------------
# Report smart-custom coercion + --append warning (SPLIT from characterization).
# ---------------------------------------------------------------------------


class TestReportPlan:
    def test_bare_description_with_default_format_becomes_custom(self):
        plan = build_generation_plan(
            "report",
            {"notebook_id": "nb_1", "description": "My custom prompt"},
        )
        assert plan.params["report_format"] == ReportFormat.CUSTOM
        assert plan.params["custom_prompt"] == "My custom prompt"
        assert plan.params["extra_instructions"] is None
        assert plan.display_name == "custom report"

    def test_explicit_format_with_description_keeps_format(self):
        plan = build_generation_plan(
            "report",
            {
                "notebook_id": "nb_1",
                "report_format": "study-guide",
                "description": "extra focus",
            },
        )
        assert plan.params["report_format"] == ReportFormat.STUDY_GUIDE
        assert plan.params["custom_prompt"] == "extra focus"
        assert plan.display_name == "study guide"

    def test_no_description_keeps_briefing_doc(self):
        plan = build_generation_plan(
            "report",
            {"notebook_id": "nb_1"},
        )
        assert plan.params["report_format"] == ReportFormat.BRIEFING_DOC
        assert plan.params["custom_prompt"] is None
        assert plan.display_name == "briefing document"

    def test_append_with_custom_format_emits_warning_and_clears(self):
        plan = build_generation_plan(
            "report",
            {
                "notebook_id": "nb_1",
                "report_format": "custom",
                "append_instructions": "more",
            },
        )
        assert plan.params["report_format"] == ReportFormat.CUSTOM
        assert plan.params["extra_instructions"] is None  # suppressed
        assert len(plan.warnings) == 1
        assert "--append has no effect with --format custom" in plan.warnings[0]

    def test_append_with_non_custom_format_is_preserved(self):
        plan = build_generation_plan(
            "report",
            {
                "notebook_id": "nb_1",
                "report_format": "study-guide",
                "append_instructions": "more",
            },
        )
        assert plan.params["extra_instructions"] == "more"
        assert plan.warnings == ()


# ---------------------------------------------------------------------------
# Mind-map plan: interactive default + instructions threaded for both kinds.
# ---------------------------------------------------------------------------


class TestMindMapPlan:
    def test_interactive_default_and_no_wait(self):
        plan = build_generation_plan(
            "mind-map",
            {"notebook_id": "nb_1", "source_ids": ["s1"]},
        )
        assert plan.kind == "mind-map"
        assert plan.params["kind"] == "interactive"
        assert plan.wait is False  # mind-map renders synchronously
        assert plan.max_retries == 0
        assert plan.description == ""

    def test_interactive_keeps_instructions(self):
        # The interactive CREATE_ARTIFACT payload DOES carry a prompt slot
        # ([9][1][2], server-verified), so --instructions must be threaded
        # through rather than dropped with a warning (the old behavior).
        plan = build_generation_plan(
            "mind-map",
            {"notebook_id": "nb_1", "map_kind": "interactive", "instructions": "focus on X"},
        )
        assert plan.params["instructions"] == "focus on X"
        assert plan.stderr_warnings == ()

    def test_note_backed_keeps_instructions(self):
        plan = build_generation_plan(
            "mind-map",
            {"notebook_id": "nb_1", "map_kind": "note-backed", "instructions": "focus on X"},
        )
        assert plan.params["kind"] == "note-backed"
        assert plan.params["instructions"] == "focus on X"
        assert plan.stderr_warnings == ()
