"""Direct service-layer tests for ``cli/services/generate.py`` (P3.T1).

These tests exercise :func:`build_generation_plan` and
:func:`execute_generation` directly — no ``CliRunner``, no Click
context — so the public service surface is testable without the
Click decorator stack. Companion characterization tests in
``tests/unit/cli/test_generate_characterization.py`` lock in the
CLI-shell byte-for-byte behavior; these tests pin the service-layer
contract.

Coverage:

* ``build_generation_plan`` over all 11 internal kinds (10 leaf
  commands + the ``cinematic-video`` alias kind) — plan field
  population, enum mapping, warning queue.
* Per-kind validation: cinematic-video alias enforcement, video
  ``--style-prompt`` rules, report smart-custom coercion, report
  ``--append`` warning side-effect.
* ``execute_generation`` happy path: dispatches the right
  ``client.artifacts.<method>`` call with the expected kwargs.
* ``execute_generation`` mind-map path: dispatches to
  ``generate_mind_map`` and renders via the generate_cmd-level
  ``_output_mind_map_result`` (which the test asserts on).
* Warning emission stays with the command layer; service execution remains I/O-free.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import notebooklm.cli.resolve as resolve_module
from notebooklm.cli.services.generate import (
    GenerationExecutionResult,
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
    execute_generation,
)
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_source(_name: str) -> bool:
    """Stub that reports every parameter as not explicitly passed."""
    return False


def _identity_language(lang: str | None) -> str:
    """Stub resolver: pass through the literal value, ``"en"`` for ``None``."""
    return lang if isinstance(lang, str) else "en"


def _base_args(**overrides: Any) -> dict[str, Any]:
    """Return the cross-kind ``raw_args`` keys with sane test defaults."""
    args: dict[str, Any] = {
        "notebook_id": "nb_123",
        "description": "",
        "source_ids": (),
        "language": None,
        "wait": False,
        "timeout": 300,
        "interval": 2,
        "max_retries": 0,
        "json_output": False,
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# build_generation_plan — happy-path parametrized over all 10 kinds + alias
# ---------------------------------------------------------------------------

# Each row: (kind, extra raw_args, expected plan attrs to spot-check, expected
# enum keys in plan.params).
_PLAN_HAPPY_CASES: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = [
    (
        "audio",
        {"audio_format": "deep-dive", "audio_length": "default", "language": "en"},
        {"display_name": "audio", "language": "en"},
        {
            "audio_format": AudioFormat.DEEP_DIVE,
            "audio_length": AudioLength.DEFAULT,
        },
    ),
    (
        "video",
        {
            "video_format": "explainer",
            "style": "auto",
            "style_prompt": None,
            "language": "en",
        },
        {"display_name": "video", "language": "en"},
        {
            "video_format": VideoFormat.EXPLAINER,
            "video_style": VideoStyle.AUTO_SELECT,
            "style_prompt": None,
        },
    ),
    (
        "cinematic-video",
        {
            "video_format": "explainer",  # alias coerces to cinematic
            "style": "auto",
            "style_prompt": None,
            "language": "en",
        },
        {
            "display_name": "video",
            "language": "en",
            "timeout": 3600.0,  # cinematic-default override (source is DEFAULT)
        },
        {},  # cinematic-video carries no kind-specific params
    ),
    (
        "slide-deck",
        {"deck_format": "detailed", "deck_length": "default", "language": "en"},
        {"display_name": "slide deck", "language": "en"},
        {
            "slide_format": SlideDeckFormat.DETAILED_DECK,
            "slide_length": SlideDeckLength.DEFAULT,
        },
    ),
    (
        "revise-slide",
        {
            "description": "Move up",
            "artifact_id": "art_1",
            "slide_index": 0,
        },
        {"display_name": "slide revision", "language": None},
        {
            "artifact_id": "art_1",
            "slide_index": 0,
            "prompt": "Move up",
        },
    ),
    (
        "quiz",
        {"quantity": "standard", "difficulty": "medium"},
        {"display_name": "quiz", "language": None},
        {
            "quantity": QuizQuantity.STANDARD,
            "difficulty": QuizDifficulty.MEDIUM,
        },
    ),
    (
        "flashcards",
        {"quantity": "standard", "difficulty": "medium"},
        {"display_name": "flashcards", "language": None},
        {
            "quantity": QuizQuantity.STANDARD,
            "difficulty": QuizDifficulty.MEDIUM,
        },
    ),
    (
        "infographic",
        {
            "orientation": "landscape",
            "detail": "standard",
            "style": "auto",
            "language": "en",
        },
        {"display_name": "infographic", "language": "en"},
        {
            "orientation": InfographicOrientation.LANDSCAPE,
            "detail_level": InfographicDetail.STANDARD,
            "style": InfographicStyle.AUTO_SELECT,
        },
    ),
    (
        "data-table",
        {"description": "Compare", "language": "en"},
        {"display_name": "data table", "language": "en"},
        {},
    ),
    (
        "mind-map",
        {"map_kind": "note-backed", "instructions": "summarize", "language": "en"},
        {
            "display_name": "mind map",
            "wait": False,
            "max_retries": 0,
            "language": "en",
        },
        {"instructions": "summarize", "kind": "note-backed"},
    ),
    (
        # Omitting --kind now defaults to interactive (#1272); interactive drops
        # --instructions (none passed here), so params carry instructions=None.
        "mind-map",
        {"language": "en"},
        {
            "display_name": "mind map",
            "wait": False,
            "max_retries": 0,
            "language": "en",
        },
        {"instructions": None, "kind": "interactive"},
    ),
    (
        "report",
        {
            "report_format": "study-guide",
            "language": "en",
            "append_instructions": None,
        },
        {"display_name": "study guide", "language": "en"},
        {
            "report_format": ReportFormat.STUDY_GUIDE,
            "custom_prompt": None,
            "extra_instructions": None,
        },
    ),
]


@pytest.mark.parametrize("kind,extra,plan_attrs,params", _PLAN_HAPPY_CASES)
def test_build_plan_happy_path(
    kind: str,
    extra: dict[str, Any],
    plan_attrs: dict[str, Any],
    params: dict[str, Any],
) -> None:
    """Every supported kind builds a frozen plan with enums mapped correctly."""
    args = _base_args(**extra)
    plan = build_generation_plan(
        kind,
        args,
        parameter_explicit=_default_source,
        language_resolver=_identity_language,
    )
    assert isinstance(plan, GenerationPlan)
    assert plan.kind == kind
    assert plan.notebook_id == "nb_123"
    for attr, value in plan_attrs.items():
        assert getattr(plan, attr) == value, f"{kind}: plan.{attr}"
    for key, value in params.items():
        assert plan.params[key] == value, f"{kind}: params[{key!r}]"


def test_build_plan_unknown_kind_raises() -> None:
    """Plan builder rejects an unrecognized kind with a typed validation error.

    ``GenerationPlanValidationError`` subclasses
    :class:`notebooklm.exceptions.ValidationError` so ``_app.errors.classify``
    covers it uniformly (no bare ``ValueError`` escapes the ``_app`` boundary).
    """
    with pytest.raises(GenerationPlanValidationError, match="Unknown generation kind"):
        build_generation_plan("not-a-kind", _base_args())


# ---------------------------------------------------------------------------
# Cinematic-video alias validation
# ---------------------------------------------------------------------------


def test_cinematic_video_rejects_explicit_non_cinematic_format() -> None:
    """``cinematic-video --format explainer`` returns a typed validation error."""

    def source(name: str) -> bool:
        return name == "video_format"

    args = _base_args(video_format="explainer", style="auto", style_prompt=None, language="en")
    with pytest.raises(GenerationPlanValidationError) as exc_info:
        build_generation_plan(
            "cinematic-video", args, parameter_explicit=source, language_resolver=_identity_language
        )
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "--format must be 'cinematic'" in exc_info.value.message


def test_cinematic_video_explicit_cinematic_format_is_accepted() -> None:
    """``cinematic-video --format cinematic`` (explicit) is fine."""

    def source(name: str) -> bool:
        return name == "video_format"

    args = _base_args(video_format="cinematic", style="auto", style_prompt=None, language="en")
    plan = build_generation_plan(
        "cinematic-video", args, parameter_explicit=source, language_resolver=_identity_language
    )
    assert plan.kind == "cinematic-video"
    assert plan.timeout == 3600.0  # default cinematic timeout


def test_cinematic_video_explicit_timeout_wins_over_default() -> None:
    """When the user passes ``--timeout``, the cinematic 3600s default does
    NOT clobber it."""

    def source(name: str) -> bool:
        # User passed --timeout explicitly; --format not.
        return name == "timeout"

    args = _base_args(
        video_format="explainer", style="auto", style_prompt=None, language="en", timeout=60
    )
    plan = build_generation_plan(
        "cinematic-video", args, parameter_explicit=source, language_resolver=_identity_language
    )
    assert plan.timeout == 60.0  # user override wins


def test_video_explicit_timeout_wins_over_default() -> None:
    """When the user passes ``--timeout``, the video 1800s default does not clobber it."""

    def source(name: str) -> bool:
        return name == "timeout"

    args = _base_args(
        video_format="explainer", style="auto", style_prompt=None, language="en", timeout=90
    )
    plan = build_generation_plan(
        "video", args, parameter_explicit=source, language_resolver=_identity_language
    )
    assert plan.timeout == 90.0


def test_video_raw_timeout_is_preserved_when_not_commandline() -> None:
    """The service must not overwrite programmatic standard-video timeouts."""

    args = _base_args(
        video_format="explainer", style="auto", style_prompt=None, language="en", timeout=90
    )
    plan = build_generation_plan(
        "video", args, parameter_explicit=_default_source, language_resolver=_identity_language
    )
    assert plan.timeout == 90.0


# ---------------------------------------------------------------------------
# Video --style / --style-prompt validation
# ---------------------------------------------------------------------------


def test_video_cinematic_rejects_style_prompt() -> None:
    """Cinematic video + non-empty ``--style-prompt`` raises a typed error."""
    args = _base_args(
        video_format="cinematic",
        style="auto",
        style_prompt="foo",
        language="en",
    )
    with pytest.raises(GenerationPlanValidationError) as exc_info:
        build_generation_plan(
            "video", args, parameter_explicit=_default_source, language_resolver=_identity_language
        )
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "--style-prompt cannot be used" in exc_info.value.message


def test_video_style_custom_requires_style_prompt() -> None:
    """``--style custom`` without ``--style-prompt`` raises a typed error."""
    args = _base_args(video_format="explainer", style="custom", style_prompt=None, language="en")
    with pytest.raises(GenerationPlanValidationError) as exc_info:
        build_generation_plan(
            "video", args, parameter_explicit=_default_source, language_resolver=_identity_language
        )
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "--style custom requires --style-prompt" in exc_info.value.message


def test_video_style_prompt_requires_style_custom() -> None:
    """Non-empty ``--style-prompt`` with ``--style != custom`` raises a typed error."""
    args = _base_args(
        video_format="explainer", style="anime", style_prompt="hand-drawn", language="en"
    )
    with pytest.raises(GenerationPlanValidationError) as exc_info:
        build_generation_plan(
            "video", args, parameter_explicit=_default_source, language_resolver=_identity_language
        )
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "--style-prompt requires --style custom" in exc_info.value.message


def test_video_style_prompt_strips_whitespace() -> None:
    """Whitespace-only ``--style-prompt`` is treated as unset (still triggers
    the ``--style custom`` requirement when style is custom)."""
    args = _base_args(video_format="explainer", style="custom", style_prompt="   ", language="en")
    with pytest.raises(GenerationPlanValidationError) as exc_info:
        build_generation_plan(
            "video", args, parameter_explicit=_default_source, language_resolver=_identity_language
        )
    assert exc_info.value.code == "VALIDATION_ERROR"
    assert "--style custom requires --style-prompt" in exc_info.value.message


# ---------------------------------------------------------------------------
# Report smart-custom coercion + --append warning
# ---------------------------------------------------------------------------


def test_report_smart_custom_coercion_on_default_format() -> None:
    """A description on default ``--format briefing-doc`` swaps to custom."""
    args = _base_args(
        description="My white paper",
        report_format="briefing-doc",
        append_instructions=None,
    )
    plan = build_generation_plan(
        "report", args, parameter_explicit=_default_source, language_resolver=_identity_language
    )
    assert plan.params["report_format"] == ReportFormat.CUSTOM
    assert plan.params["custom_prompt"] == "My white paper"
    assert plan.display_name == "custom report"
    assert plan.warnings == ()


def test_report_explicit_format_with_description_keeps_format_but_sets_custom_prompt() -> None:
    """``--format study-guide`` + description keeps study-guide but passes
    the description as ``custom_prompt`` (pre-extraction behavior)."""
    args = _base_args(
        description="Brief audience: novices",
        report_format="study-guide",
        append_instructions=None,
    )
    plan = build_generation_plan(
        "report", args, parameter_explicit=_default_source, language_resolver=_identity_language
    )
    assert plan.params["report_format"] == ReportFormat.STUDY_GUIDE
    assert plan.params["custom_prompt"] == "Brief audience: novices"
    assert plan.display_name == "study guide"


def test_report_append_with_custom_format_queues_warning_and_drops_append() -> None:
    """``--append`` with ``--format custom`` queues a stderr warning and clears
    the append payload before the API call."""
    args = _base_args(
        description="desc",
        report_format="custom",
        append_instructions="extra",
    )
    plan = build_generation_plan(
        "report", args, parameter_explicit=_default_source, language_resolver=_identity_language
    )
    assert plan.params["extra_instructions"] is None
    assert plan.warnings == (
        "Warning: --append has no effect with --format custom. "
        "Use the description argument instead.",
    )


def test_report_append_with_non_custom_format_passes_through() -> None:
    """``--append`` with a non-custom format passes through unchanged."""
    args = _base_args(
        description="",
        report_format="study-guide",
        append_instructions="Target: novices",
    )
    plan = build_generation_plan(
        "report", args, parameter_explicit=_default_source, language_resolver=_identity_language
    )
    assert plan.params["report_format"] == ReportFormat.STUDY_GUIDE
    assert plan.params["custom_prompt"] is None
    assert plan.params["extra_instructions"] == "Target: novices"
    assert plan.warnings == ()


# ---------------------------------------------------------------------------
# execute_generation — happy path, mind-map path, warning emission
# ---------------------------------------------------------------------------


def _make_mock_client(method_name: str, return_value: Any) -> MagicMock:
    """Build a mock ``NotebookLMClient``-shaped object with the indicated
    ``artifacts.<method_name>`` attached as an AsyncMock and async
    enter/exit so callers can ``async with`` it (we never use the
    context manager here — caller passes the bare mock directly to
    ``execute_generation`` which expects an open client)."""
    client = MagicMock()
    client.artifacts = MagicMock()
    setattr(client.artifacts, method_name, AsyncMock(return_value=return_value))
    return client


@pytest.mark.parametrize(
    "kind,extra,method,expected_kwargs",
    [
        (
            "audio",
            {
                "description": "deep dive",
                "audio_format": "deep-dive",
                "audio_length": "default",
                "language": "en",
            },
            "generate_audio",
            {
                "source_ids": (),
                "language": "en",
                "instructions": "deep dive",
                "audio_format": AudioFormat.DEEP_DIVE,
                "audio_length": AudioLength.DEFAULT,
            },
        ),
        (
            "quiz",
            {"quantity": "standard", "difficulty": "medium"},
            "generate_quiz",
            {
                "source_ids": (),
                "instructions": None,
                "quantity": QuizQuantity.STANDARD,
                "difficulty": QuizDifficulty.MEDIUM,
            },
        ),
        (
            "data-table",
            {"description": "Compare", "language": "en"},
            "generate_data_table",
            {
                "source_ids": (),
                "language": "en",
                "instructions": "Compare",
            },
        ),
        (
            "report",
            {
                "report_format": "blog-post",
                "language": "en",
                "append_instructions": "Tone: casual",
            },
            "generate_report",
            {
                "source_ids": (),
                "language": "en",
                "report_format": ReportFormat.BLOG_POST,
                "custom_prompt": None,
                "extra_instructions": "Tone: casual",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_execute_generation_dispatches_with_expected_kwargs(
    kind: str,
    extra: dict[str, Any],
    method: str,
    expected_kwargs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor calls ``client.artifacts.<method>(notebook_id, **kwargs)``
    with the kwargs computed by the plan."""

    # Stub the resolve_* helpers so they return the inputs untouched.
    async def fake_resolve_notebook_id(_client, nb, *, json_output=False):
        return nb

    async def fake_resolve_source_ids(_client, _nb, sources, *, json_output=False):
        return tuple(sources)

    monkeypatch.setattr(resolve_module, "resolve_notebook_id", fake_resolve_notebook_id)
    monkeypatch.setattr(resolve_module, "resolve_source_ids", fake_resolve_source_ids)

    plan = build_generation_plan(
        kind,
        _base_args(**extra),
        parameter_explicit=_default_source,
        language_resolver=_identity_language,
    )
    client = _make_mock_client(method, {"task_id": "tid", "status": "processing"})

    result = await execute_generation(plan, client)

    api = getattr(client.artifacts, method)
    api.assert_awaited_once()
    call = api.await_args
    assert call.args == ("nb_123",)
    assert call.kwargs == expected_kwargs
    assert isinstance(result, GenerationExecutionResult)
    assert result.kind == kind
    assert result.generation is not None
    assert result.generation.status == "pending"
    assert result.generation.task_id == "tid"


@pytest.mark.asyncio
async def test_execute_generation_mind_map_dispatches_and_returns_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mind-map kind dispatches to ``generate_mind_map`` and returns payload for rendering."""

    async def fake_resolve_notebook_id(_client, nb, *, json_output=False):
        return nb

    async def fake_resolve_source_ids(_client, _nb, sources, *, json_output=False):
        return tuple(sources)

    monkeypatch.setattr(resolve_module, "resolve_notebook_id", fake_resolve_notebook_id)
    monkeypatch.setattr(resolve_module, "resolve_source_ids", fake_resolve_source_ids)
    plan = build_generation_plan(
        "mind-map",
        _base_args(
            map_kind="note-backed", instructions="summarize", language="en", json_output=True
        ),
        parameter_explicit=_default_source,
        language_resolver=_identity_language,
    )
    mind_map_payload = {"note_id": "n1", "mind_map": {"name": "Root", "children": []}}
    client = _make_mock_client("generate_mind_map", mind_map_payload)

    result = await execute_generation(plan, client)

    client.artifacts.generate_mind_map.assert_awaited_once_with(
        "nb_123",
        source_ids=(),
        language="en",
        instructions="summarize",
    )
    assert isinstance(result, GenerationExecutionResult)
    assert result.kind == "mind-map"
    assert result.mind_map == mind_map_payload


@pytest.mark.asyncio
async def test_execute_generation_leaves_plan_warnings_for_command_layer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Queued plan warnings stay on the plan while execution remains I/O-free."""

    async def fake_resolve_notebook_id(_client, nb, *, json_output=False):
        return nb

    async def fake_resolve_source_ids(_client, _nb, sources, *, json_output=False):
        return tuple(sources)

    monkeypatch.setattr(resolve_module, "resolve_notebook_id", fake_resolve_notebook_id)
    monkeypatch.setattr(resolve_module, "resolve_source_ids", fake_resolve_source_ids)

    plan = build_generation_plan(
        "report",
        _base_args(
            description="desc",
            report_format="custom",
            append_instructions="extra",
        ),
        parameter_explicit=_default_source,
        language_resolver=_identity_language,
    )
    assert plan.warnings  # sanity: plan queued at least one warning

    client = _make_mock_client("generate_report", {"task_id": "tid", "status": "processing"})

    await execute_generation(plan, client)

    client.artifacts.generate_report.assert_awaited_once()
    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_execute_generation_revise_slide_skips_source_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revise-slide kind never calls ``resolve_source_ids`` and forwards
    artifact_id / slide_index / prompt to the API."""
    resolve_calls: dict[str, int] = {"sources": 0}

    async def fake_resolve_notebook_id(_client, nb, *, json_output=False):
        return nb

    async def fake_resolve_source_ids(_client, _nb, sources, *, json_output=False):
        resolve_calls["sources"] += 1
        return tuple(sources)

    monkeypatch.setattr(resolve_module, "resolve_notebook_id", fake_resolve_notebook_id)
    monkeypatch.setattr(resolve_module, "resolve_source_ids", fake_resolve_source_ids)

    plan = build_generation_plan(
        "revise-slide",
        _base_args(description="Move up", artifact_id="art_1", slide_index=3),
        parameter_explicit=_default_source,
        language_resolver=_identity_language,
    )
    client = _make_mock_client("revise_slide", {"task_id": "tid", "status": "processing"})

    await execute_generation(plan, client)

    assert resolve_calls["sources"] == 0
    client.artifacts.revise_slide.assert_awaited_once_with(
        "nb_123",
        artifact_id="art_1",
        slide_index=3,
        prompt="Move up",
    )
