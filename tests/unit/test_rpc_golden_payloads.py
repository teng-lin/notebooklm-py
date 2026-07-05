"""Golden RPC envelope + decoder coverage, parameterised over ``RPCMethod``.

This module pins, for every member of :class:`notebooklm.rpc.types.RPCMethod`:

1. The string method ID itself (catches enum-value drift).
2. The ``batchexecute`` ``f.req`` request envelope produced by
   :func:`notebooklm.rpc.encoder.encode_rpc_request` for a representative
   parameter list (catches encoder format drift and param-order regressions).
3. The Python payload returned by
   :func:`notebooklm.rpc.decoder.decode_response` when given a synthetic
   scrubbed response chunk for that method (catches decoder format drift).

For methods that have a documented downstream parser / dataclass mapper,
the fixture additionally pins the mapper output shape so the seam between
the raw decoded payload and the feature-level dataclass is also covered.

Each method has a fixture file at
``tests/fixtures/rpc_golden/<METHOD_NAME>.json``. A test that detects a
missing fixture fails the suite loudly so that adding a new ``RPCMethod``
member without also adding a fixture is a hard failure rather than a silent
gap.

Fixture schema is documented in ``tests/fixtures/rpc_golden/README.md``.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import warnings
from pathlib import Path
from typing import Any, cast

import pytest

from notebooklm._app.serialize import to_jsonable
from notebooklm._artifact.payloads import (
    build_audio_artifact_params,
    build_cinematic_video_artifact_params,
    build_data_table_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_mind_map_params,
    build_quiz_artifact_params,
    build_report_artifact_params,
    build_retry_artifact_params,
    build_revise_slide_params,
    build_slide_deck_artifact_params,
    build_suggest_reports_params,
    build_video_artifact_params,
)
from notebooklm._row_adapters.artifacts import ArtifactRow
from notebooklm._row_adapters.notes import NoteRow
from notebooklm._row_adapters.sources import SourceRow, SourceRowShape
from notebooklm._source.upload_payloads import (
    build_register_file_source_params,
    build_rename_source_params,
    build_resumable_upload_start_request,
)
from notebooklm._types.artifacts import Artifact, ArtifactType
from notebooklm._types.sources import Source, SourceType
from notebooklm.exceptions import (
    ClientError,
    RateLimitError,
    RPCError,
    UnknownRPCMethodError,
)
from notebooklm.rpc.decoder import (
    collect_rpc_ids,
    decode_response,
    parse_chunked_response,
    strip_anti_xssi,
)
from notebooklm.rpc.encoder import encode_rpc_request
from notebooklm.rpc.types import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
    SourceStatus,
    VideoFormat,
    VideoStyle,
)

FIXTURE_ROOT: Path = Path(__file__).parents[1] / "fixtures" / "rpc_golden"

_ARTIFACT_CLIENT_OPTIONS: list[Any] = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1]],
    [[1, 4, 8, 2, 3, 6]],
]


class _FixtureSchemaError(AssertionError):
    """Raised when a fixture is missing a required field or has the wrong type.

    Carries the file path and the dotted field name so a malformed fixture
    surfaces a structured failure instead of a raw KeyError / TypeError
    from elsewhere in the test. Inherits from :class:`AssertionError` (not
    :class:`ValueError`) so pytest renders it with the same friendly
    rewriting it applies to ``assert`` failures, and so any caller that
    handles ``AssertionError`` (e.g. pytest hooks, ``--tb=short``) treats
    it as a structured test failure rather than a generic exception.
    """


def test_report_payload_unknown_format_raises_contextual_value_error() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_report_artifact_params(
            "nb_payload",
            ["src_alpha"],
            report_format=cast(ReportFormat, "future-report-format"),
            language="en",
            custom_prompt=None,
            extra_instructions=None,
        )

    message = str(exc_info.value)
    assert "Unsupported report format" in message
    assert "future-report-format" in message
    assert "briefing_doc" in message
    assert "custom" in message


def _fixture_path(method: RPCMethod) -> Path:
    return FIXTURE_ROOT / f"{method.name}.json"


def _load_fixture(method: RPCMethod) -> dict[str, Any]:
    path = _fixture_path(method)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing golden fixture for RPCMethod.{method.name} at {path}. "
            f"Every RPCMethod enum value must have a fixture under "
            f"tests/fixtures/rpc_golden/. See the README in that directory "
            f"for the schema."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _require_field(
    fixture: dict[str, Any],
    dotted: str,
    expected_type: type | tuple[type, ...],
    *,
    method: RPCMethod,
) -> Any:
    """Look up ``dotted`` (e.g. ``"request.params"``) in ``fixture``.

    Raises :class:`_FixtureSchemaError` with the method name and field
    path if the key is missing or the value is the wrong type. Keeps the
    failure message structured so a malformed fixture is debuggable
    without grepping through raw stack traces.
    """
    current: Any = fixture
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if not isinstance(current, dict) or part not in current:
            raise _FixtureSchemaError(
                f"Fixture for RPCMethod.{method.name} is missing required "
                f"field {'.'.join(parts[: i + 1])!r} (file: {_fixture_path(method)})."
            )
        current = current[part]
    if not isinstance(current, expected_type):
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} field {dotted!r} has type "
            f"{type(current).__name__}, expected "
            f"{getattr(expected_type, '__name__', expected_type)} "
            f"(file: {_fixture_path(method)})."
        )
    return current


def _build_wire_response(chunks: list[Any]) -> str:
    """Serialise structured chunks back into the chunked-response wire format.

    Mirrors the shape that :func:`decode_response` expects after
    :func:`strip_anti_xssi`: each chunk is a JSON line preceded by a
    byte-count line. The full body starts with the canonical anti-XSSI
    prefix ``)]}'\\n`` so the decoder's prefix-stripping path is also
    exercised.
    """
    parts: list[str] = [")]}'"]
    for chunk in chunks:
        chunk_json = json.dumps(chunk, separators=(",", ":"))
        parts.append(str(len(chunk_json.encode("utf-8"))))
        parts.append(chunk_json)
    return "\n".join(parts) + "\n"


def _resolve_mapper(dotted: str, *, method: RPCMethod) -> Any:
    """Resolve ``"module.path:attr"`` to a callable.

    Used by fixtures that pin a downstream mapper / parser output shape in
    addition to the raw decoded payload. Wraps the import + getattr step
    in structured :class:`_FixtureSchemaError` so a missing module or
    attribute surfaces the fixture file path rather than a raw
    ``ModuleNotFoundError`` / ``AttributeError``.
    """
    module_name, _, attr = dotted.partition(":")
    if not module_name or not attr:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} declares mapper {dotted!r} "
            f"but it is not in 'module.path:attribute' form "
            f"(file: {_fixture_path(method)})."
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} mapper {dotted!r} references "
            f"unknown module {module_name!r} (file: {_fixture_path(method)})."
        ) from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} mapper {dotted!r}: module "
            f"{module_name!r} has no attribute {attr!r} "
            f"(file: {_fixture_path(method)})."
        ) from exc


ALL_METHODS: list[RPCMethod] = list(RPCMethod)


def _expected_rpc_envelope(method: RPCMethod, params: list[Any]) -> list[Any]:
    return [[[method.value, json.dumps(params, separators=(",", ":")), None, "generic"]]]


@pytest.mark.parametrize(
    ("case_name", "params", "expected"),
    [
        (
            "audio_defaults",
            build_audio_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions=None,
                audio_format=None,
                audio_length=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    1,
                    [[["src_alpha"]]],
                    None,
                    None,
                    [
                        None,
                        [None, 2, None, [["src_alpha"]], "en", None, 1],
                    ],
                ],
            ],
        ),
        (
            "audio_explicit_options",
            build_audio_artifact_params(
                "nb_payload",
                ["src_alpha", "src_beta"],
                language="es",
                instructions="Focus on terminology",
                audio_format=AudioFormat.BRIEF,
                audio_length=AudioLength.SHORT,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    1,
                    [[["src_alpha"]], [["src_beta"]]],
                    None,
                    None,
                    [
                        None,
                        [
                            "Focus on terminology",
                            1,
                            None,
                            [["src_alpha"], ["src_beta"]],
                            "es",
                            None,
                            2,
                        ],
                    ],
                ],
            ],
        ),
        (
            "video_defaults",
            build_video_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions=None,
                video_format=None,
                video_style=None,
                style_prompt=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    3,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        None,
                        [[["src_alpha"]], "en", None, None, 1, 1],
                    ],
                ],
            ],
        ),
        (
            "video_custom_style",
            build_video_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="fr",
                instructions="Summarize visually",
                video_format=VideoFormat.EXPLAINER,
                video_style=VideoStyle.CUSTOM,
                style_prompt="blueprint line art",
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    3,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        None,
                        [
                            [["src_alpha"]],
                            "fr",
                            "Summarize visually",
                            None,
                            1,
                            None,
                            "blueprint line art",
                        ],
                    ],
                ],
            ],
        ),
        (
            "cinematic_video",
            build_cinematic_video_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="de",
                instructions=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    3,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    [None, None, [[["src_alpha"]], "de", None, None, 3]],
                ],
            ],
        ),
        (
            # #1805: SHORT (code 4) rides the STANDARD builder — format_code at
            # slot [4] == 4 with the style_code (AUTO_SELECT=1) still present,
            # NOT the cinematic special shape (which drops the style slot).
            "video_short_format",
            build_video_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions=None,
                video_format=VideoFormat.SHORT,
                video_style=None,
                style_prompt=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    3,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    [None, None, [[["src_alpha"]], "en", None, None, 4, 1]],
                ],
            ],
        ),
        (
            "video_non_contiguous_preset_style",
            build_video_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions="Make it playful",
                video_format=VideoFormat.BRIEF,
                video_style=VideoStyle.KAWAII,
                style_prompt=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    3,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        None,
                        [[["src_alpha"]], "en", "Make it playful", None, 2, 9],
                    ],
                ],
            ],
        ),
        (
            "briefing_report",
            build_report_artifact_params(
                "nb_payload",
                ["src_alpha"],
                report_format=ReportFormat.BRIEFING_DOC,
                language="en",
                custom_prompt=None,
                extra_instructions=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    2,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    [
                        None,
                        [
                            "Briefing Doc",
                            "Key insights and important quotes",
                            None,
                            [["src_alpha"]],
                            "en",
                            (
                                "Create a comprehensive briefing document that includes an "
                                "Executive Summary, detailed analysis of key themes, important "
                                "quotes with context, and actionable insights."
                            ),
                            None,
                            True,
                        ],
                    ],
                ],
            ],
        ),
        (
            "custom_report",
            build_report_artifact_params(
                "nb_payload",
                ["src_alpha"],
                report_format=ReportFormat.CUSTOM,
                language="en",
                custom_prompt="Compare the claims.",
                extra_instructions="Ignored for custom reports.",
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    2,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    [
                        None,
                        [
                            "Custom Report",
                            "Custom format",
                            None,
                            [["src_alpha"]],
                            "en",
                            "Compare the claims.",
                            None,
                            True,
                        ],
                    ],
                ],
            ],
        ),
        (
            "quiz_defaults",
            build_quiz_artifact_params(
                "nb_payload",
                ["src_alpha"],
                instructions=None,
                quantity=None,
                difficulty=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    4,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        [2, None, None, None, None, None, None, [2, 2]],
                    ],
                ],
            ],
        ),
        (
            "quiz_options",
            build_quiz_artifact_params(
                "nb_payload",
                ["src_alpha"],
                instructions="Make it practical",
                quantity=QuizQuantity.FEWER,
                difficulty=QuizDifficulty.HARD,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    4,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        [2, None, "Make it practical", None, None, None, None, [1, 3]],
                    ],
                ],
            ],
        ),
        (
            "flashcards_options",
            build_flashcards_artifact_params(
                "nb_payload",
                ["src_alpha"],
                instructions="Use short prompts",
                quantity=QuizQuantity.STANDARD,
                difficulty=QuizDifficulty.EASY,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    4,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        [1, None, "Use short prompts", None, None, None, [1, 2]],
                    ],
                ],
            ],
        ),
        (
            "infographic_defaults",
            build_infographic_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions=None,
                orientation=None,
                detail_level=None,
                style=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    7,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [[None, "en", None, 1, 2, 1]],
                ],
            ],
        ),
        (
            "infographic_visual_options",
            build_infographic_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="it",
                instructions="Prioritize the timeline",
                orientation=InfographicOrientation.PORTRAIT,
                detail_level=InfographicDetail.DETAILED,
                style=InfographicStyle.EDITORIAL,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    7,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [["Prioritize the timeline", "it", None, 2, 3, 5]],
                ],
            ],
        ),
        (
            "slide_deck_defaults",
            build_slide_deck_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="en",
                instructions=None,
                slide_format=None,
                slide_length=None,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    8,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [[None, "en", 1, 1]],
                ],
            ],
        ),
        (
            "slide_deck_options",
            build_slide_deck_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="pt",
                instructions="Board-level summary",
                slide_format=SlideDeckFormat.PRESENTER_SLIDES,
                slide_length=SlideDeckLength.SHORT,
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    8,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [["Board-level summary", "pt", 2, 2]],
                ],
            ],
        ),
        (
            "data_table",
            build_data_table_artifact_params(
                "nb_payload",
                ["src_alpha"],
                language="ja",
                instructions="Extract product comparisons",
            ),
            [
                _ARTIFACT_CLIENT_OPTIONS,
                "nb_payload",
                [
                    None,
                    None,
                    9,
                    [[["src_alpha"]]],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [None, ["Extract product comparisons", "ja"]],
                ],
            ],
        ),
        (
            "mind_map",
            build_mind_map_params(
                ["src_alpha"],
                language="en",
                instructions="Cluster by theme",
            ),
            [
                [[["src_alpha"]]],
                None,
                None,
                None,
                None,
                ["interactive_mindmap", [["[CONTEXT]", "Cluster by theme"]], "en"],
                None,
                [2, None, [1]],
            ],
        ),
    ],
)
def test_artifact_payload_builders_match_golden_rpc_envelopes(
    case_name: str,
    params: list[Any],
    expected: list[Any],
) -> None:
    method = RPCMethod.GENERATE_MIND_MAP if case_name == "mind_map" else RPCMethod.CREATE_ARTIFACT

    assert params == expected
    assert encode_rpc_request(method, params) == _expected_rpc_envelope(method, expected)


def test_video_style_values_match_live_web_ui() -> None:
    """Guard against drift in the Web UI's Video Overview style radio values."""
    assert {style: style.value for style in VideoStyle} == {
        VideoStyle.AUTO_SELECT: 1,
        VideoStyle.CUSTOM: 0,
        VideoStyle.CLASSIC: 2,
        VideoStyle.WHITEBOARD: 3,
        VideoStyle.KAWAII: 9,
        VideoStyle.ANIME: 7,
        VideoStyle.WATERCOLOR: 6,
        VideoStyle.RETRO_PRINT: 8,
        VideoStyle.HERITAGE: 4,
        VideoStyle.PAPER_CRAFT: 5,
    }


def test_video_style_prompt_slot_is_custom_only() -> None:
    """Preset styles must not emit the Web UI's custom visual-style prompt slot."""
    params = build_video_artifact_params(
        "nb_payload",
        ["src_alpha"],
        language="en",
        instructions="Make it playful",
        video_format=VideoFormat.EXPLAINER,
        video_style=VideoStyle.WHITEBOARD,
        style_prompt="ignored outside custom style",
    )

    video_config = params[2][8][2]
    assert video_config == [[["src_alpha"]], "en", "Make it playful", None, 1, 3]


def test_revise_slide_payload_builder_matches_golden_envelope() -> None:
    params = build_revise_slide_params("artifact_payload", 2, "Tighten the summary")

    assert params == [[2], "artifact_payload", [[[2, "Tighten the summary"]]]]
    assert encode_rpc_request(RPCMethod.REVISE_SLIDE, params) == _expected_rpc_envelope(
        RPCMethod.REVISE_SLIDE,
        params,
    )


def test_retry_artifact_payload_builder_matches_golden_envelope() -> None:
    params = build_retry_artifact_params("artifact_payload")

    # The type-agnostic client-options literal is sent verbatim (issue #1319;
    # also confirmed for CREATE_ARTIFACT on 2026-06-15).
    assert params == [_ARTIFACT_CLIENT_OPTIONS, "artifact_payload"]
    encoded = encode_rpc_request(RPCMethod.RETRY_ARTIFACT, params)
    assert encoded == _expected_rpc_envelope(RPCMethod.RETRY_ARTIFACT, params)
    # The encoded envelope must carry the confirmed wire ID.
    assert encoded[0][0][0] == "Rytqqe"


def test_suggest_reports_payload_builder_matches_golden_envelope() -> None:
    params = build_suggest_reports_params("nb_payload")

    assert params == [[2], "nb_payload"]
    assert encode_rpc_request(RPCMethod.GET_SUGGESTED_REPORTS, params) == _expected_rpc_envelope(
        RPCMethod.GET_SUGGESTED_REPORTS,
        params,
    )


def test_source_upload_rpc_payload_builders_match_golden_envelopes() -> None:
    register_params = build_register_file_source_params("research.pdf", "nb_payload")
    rename_params = build_rename_source_params("src_payload", "Renamed source")

    # Nested template block per the Gemini-3.5 wire migration (#1546).
    assert register_params == [
        [["research.pdf"]],
        "nb_payload",
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    ]
    assert encode_rpc_request(RPCMethod.ADD_SOURCE_FILE, register_params) == _expected_rpc_envelope(
        RPCMethod.ADD_SOURCE_FILE,
        register_params,
    )
    assert rename_params == [None, ["src_payload"], [[["Renamed source"]]]]
    assert encode_rpc_request(RPCMethod.UPDATE_SOURCE, rename_params) == _expected_rpc_envelope(
        RPCMethod.UPDATE_SOURCE,
        rename_params,
    )


def test_resumable_upload_start_request_matches_golden_payload() -> None:
    request = build_resumable_upload_start_request(
        notebook_id="nb_payload",
        filename="research.pdf",
        file_size=4096,
        source_id="src_payload",
        content_type="application/pdf",
        base_url="https://notebooklm.google.com",
        upload_url="https://notebooklm.google.com/_/upload",
        authuser_query="authuser=1",
        authuser_header="1",
    )

    assert request.url == "https://notebooklm.google.com/_/upload?authuser=1"
    assert request.headers == {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://notebooklm.google.com",
        "Referer": "https://notebooklm.google.com/",
        "x-goog-authuser": "1",
        "x-goog-upload-command": "start",
        "x-goog-upload-header-content-length": "4096",
        "x-goog-upload-header-content-type": "application/pdf",
        "x-goog-upload-protocol": "resumable",
    }
    assert request.body == (
        '{"PROJECT_ID": "nb_payload", "SOURCE_NAME": "research.pdf", "SOURCE_ID": "src_payload"}'
    )


def test_every_rpc_method_has_a_fixture() -> None:
    """Adding a new ``RPCMethod`` without a fixture must fail the suite.

    This is the load-bearing guard called out in the task spec: future enum
    additions fail loudly rather than silently leaving coverage gaps.
    """
    missing = [m.name for m in ALL_METHODS if not _fixture_path(m).is_file()]
    assert not missing, (
        f"Missing golden fixtures for: {missing}. Add a JSON fixture under "
        f"tests/fixtures/rpc_golden/ for each. See the README there for the "
        f"schema."
    )


# Substrings that MUST NOT appear inside any fixture file. Catches future
# edits that paste real account / cookie / OAuth material into a fixture by
# mistake. The list mirrors the placeholder taxonomy in the directory README
# — anything that would never appear in a synthetic scrubbed payload.
_FORBIDDEN_FIXTURE_SUBSTRINGS: tuple[str, ...] = (
    "@gmail.com",
    "@google.com",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "Bearer ",
    "ya29.",  # OAuth access-token prefix
    "drive.google.com/",
    "docs.google.com/",
    "AIza",  # Google API-key prefix
)


def test_fixture_corpus_is_scrubbed() -> None:
    """Lint guard: no fixture may contain real-credential / real-PII substrings.

    The fixture directory sits outside the cassette-scrubber pipeline (per
    ADR-0006) because these payloads are synthetic by construction. This
    lint enforces that posture: if anyone edits a fixture and pastes a real
    cookie / OAuth token / Drive URL / email, this test fails before the
    leak lands in a commit. Pair the lint with the placeholder taxonomy
    documented in tests/fixtures/rpc_golden/README.md.
    """
    leaks: list[tuple[str, str]] = []
    # Scan JSON fixtures AND the README — the README contains worked
    # examples of placeholder shapes and is the most likely place for a
    # well-meaning contributor to paste a "real-looking" URL when updating
    # the schema docs.
    for path in sorted(FIXTURE_ROOT.iterdir()):
        if path.suffix not in (".json", ".md"):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_FIXTURE_SUBSTRINGS:
            if needle in text:
                leaks.append((path.name, needle))
    assert not leaks, (
        f"Forbidden non-scrubbed substring(s) found in fixture corpus: "
        f"{leaks}. Replace with the synthetic placeholders documented in "
        f"tests/fixtures/rpc_golden/README.md."
    )


def test_fixture_directory_has_no_orphans() -> None:
    """Every fixture file must correspond to a live ``RPCMethod`` member.

    Catches the inverse drift: a method is renamed/removed but its fixture
    file is left behind. Without this guard, the orphan would silently
    persist in the corpus.
    """
    valid_names = {m.name for m in ALL_METHODS}
    orphans = [path.stem for path in FIXTURE_ROOT.glob("*.json") if path.stem not in valid_names]
    assert not orphans, (
        f"Orphan fixtures with no corresponding RPCMethod member: {orphans}. "
        f"Remove the fixture file or restore the enum member."
    )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_fixture_has_required_schema(method: RPCMethod) -> None:
    """Every fixture must expose the required top-level schema fields.

    Runs the structural validator once per method so a malformed fixture
    surfaces as a clear ``_FixtureSchemaError`` here instead of a raw
    ``KeyError`` / ``TypeError`` from one of the downstream tests.
    """
    fixture = _load_fixture(method)
    _require_field(fixture, "method_name", str, method=method)
    _require_field(fixture, "method_id", str, method=method)
    _require_field(fixture, "request.params", list, method=method)
    _require_field(fixture, "request.expected_f_req", list, method=method)
    _require_field(fixture, "response.chunks", list, method=method)
    # expected_decoded is allowed to be None (allow_null path); we only
    # require the key to be present.
    if "expected_decoded" not in fixture.get("response", {}):
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} is missing required field "
            f"'response.expected_decoded' (file: {_fixture_path(method)})."
        )
    if "mapper" in fixture:
        _require_field(fixture, "mapper", str, method=method)
        if "mapper_expected" not in fixture:
            raise _FixtureSchemaError(
                f"Fixture for RPCMethod.{method.name} declares 'mapper' "
                f"but is missing 'mapper_expected' (file: {_fixture_path(method)})."
            )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_method_id_matches_fixture(method: RPCMethod) -> None:
    """The fixture's recorded ``method_id`` must match the enum value.

    This is the primary guard against accidental enum-value edits: any
    change to ``RPCMethod.<NAME>.value`` requires a matching fixture edit.
    """
    fixture = _load_fixture(method)
    fixture_method_name = _require_field(fixture, "method_name", str, method=method)
    fixture_method_id = _require_field(fixture, "method_id", str, method=method)
    assert fixture_method_name == method.name, (
        f"Fixture method_name {fixture_method_name!r} does not match "
        f"enum name {method.name!r} (file mislabelled?)"
    )
    assert fixture_method_id == method.value, (
        f"Fixture method_id {fixture_method_id!r} for {method.name} "
        f"does not match enum value {method.value!r}. If the wire ID truly "
        f"changed, update both rpc/types.py and the fixture together."
    )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_request_envelope_matches_fixture(method: RPCMethod) -> None:
    """The ``batchexecute`` ``f.req`` envelope must match the fixture.

    The fixture records both the input ``params`` and the expected encoded
    envelope. We re-encode and compare against the recorded envelope so any
    drift in :func:`encode_rpc_request` (nesting depth, param JSON-encoding,
    trailing markers) trips this assertion.
    """
    fixture = _load_fixture(method)
    params = _require_field(fixture, "request.params", list, method=method)
    expected = _require_field(fixture, "request.expected_f_req", list, method=method)

    encoded = encode_rpc_request(method, params)

    assert encoded == expected, (
        f"Encoded f.req envelope for {method.name} drifted from the fixture. "
        f"Got: {encoded!r}\nExpected: {expected!r}"
    )

    # Shape invariants — strictly redundant once the equality assertion above
    # passes (since ``encoded == expected`` means both share these properties),
    # but kept as a machine-checked specification of the batchexecute wire
    # format that survives even if a future contributor accidentally copies a
    # regressed encoder output into the fixture.
    assert isinstance(encoded, list) and len(encoded) == 1
    assert isinstance(encoded[0], list) and len(encoded[0]) == 1
    inner = encoded[0][0]
    assert inner[0] == method.value
    assert inner[2] is None
    assert inner[3] == "generic"
    # inner[1] is the JSON-encoded params string — re-decode and verify
    # round-trip equivalence to params.
    assert json.loads(inner[1]) == params


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_response_decoder_returns_expected_payload(method: RPCMethod) -> None:
    """The decoder must return the expected raw payload for the fixture.

    Builds a wire-format response from the fixture's structured ``chunks``
    field, feeds it to :func:`decode_response`, and compares the returned
    Python value to the fixture's ``expected_decoded`` value.

    Methods that legitimately return ``None`` on success (e.g. fire-and-
    forget RPCs whose ``wrb.fr`` payload is ``null``) opt into
    ``allow_null: true`` in the fixture; the decoder receives the same
    flag. For those, this test ALSO asserts that the synthetic response
    actually contains a ``wrb.fr`` row for ``method.value`` — without that
    cross-check, an ``allow_null=True`` fixture would silently pass even
    if its chunks named a wrong (or no) RPC ID, since
    :func:`decode_response` returns ``None`` either way under ``allow_null``.
    """
    fixture = _load_fixture(method)
    chunks = _require_field(fixture, "response.chunks", list, method=method)
    response = fixture["response"]
    allow_null = response.get("allow_null", False)
    if "expected_decoded" not in response:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} is missing required field "
            f"'response.expected_decoded' (file: {_fixture_path(method)})."
        )
    expected = response["expected_decoded"]

    raw_response = _build_wire_response(chunks)
    decoded = decode_response(raw_response, method.value, allow_null=allow_null)

    assert decoded == expected, (
        f"decode_response({method.name}) returned a payload that does not "
        f"match the fixture's expected_decoded.\n"
        f"Got: {decoded!r}\nExpected: {expected!r}"
    )

    # Independent of allow_null, the response chunks MUST contain a row
    # naming this method's RPC ID. This guards against the silent
    # pass-through that allow_null=True otherwise enables.
    parsed = parse_chunked_response(strip_anti_xssi(raw_response))
    found_ids = collect_rpc_ids(parsed)
    assert method.value in found_ids, (
        f"Synthetic response for {method.name} does not include a "
        f"'wrb.fr'/'er' row naming {method.value!r}; the fixture chunks "
        f"would let an allow_null decode silently pass. Found IDs: {found_ids!r}"
    )


def _mapper_item_repr(item: Any) -> Any:
    """Project one mapped item into its fixture-comparable shape.

    ``to_public_dict()`` wins when present (research-task models expose it);
    otherwise a dataclass instance is run through the transport-neutral
    :func:`to_jsonable` (the same serializer the public ``--json`` / MCP / HTTP
    envelopes use), and anything else passes through unchanged.
    """
    if hasattr(item, "to_public_dict"):
        return item.to_public_dict()
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return to_jsonable(item)
    return item


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_mapper_output_shape_when_documented(method: RPCMethod) -> None:
    """Methods that document a downstream mapper must also pin its output.

    Most methods have inline ``safe_index`` extraction at the feature level
    and no centralised mapper; for those, this test is a no-op (the fixture
    omits ``mapper`` / ``mapper_expected``). For methods that DO have a
    clean importable mapper (e.g. research-task parsing), we invoke it on
    the decoded payload and assert against the fixture's recorded shape so
    the seam between decoder and feature-level dataclass is also covered.
    """
    fixture = _load_fixture(method)
    mapper_ref = fixture.get("mapper")
    if not mapper_ref:
        pytest.skip(f"{method.name}: no documented downstream mapper")

    if "mapper_expected" not in fixture:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} declares 'mapper' "
            f"but is missing 'mapper_expected' (file: {_fixture_path(method)})."
        )
    expected = fixture["mapper_expected"]
    mapper = _resolve_mapper(mapper_ref, method=method)
    decoded = fixture["response"]["expected_decoded"]
    mapped = mapper(decoded)

    # Mappers commonly return dataclass instances or lists thereof; compare
    # via the fixture-recorded shape (typically the public dict form or a
    # list of public dicts). The fixture decides the representation.
    #
    # Resolution order, per item:
    #   1. ``to_public_dict()`` when present (research-task models expose it);
    #   2. otherwise the transport-neutral :func:`to_jsonable` projection for a
    #      dataclass instance — the same serializer the CLI/MCP/HTTP adapters
    #      use, so the golden shape matches the public ``--json`` envelope;
    #   3. otherwise the raw return value (primitives / dicts / lists).
    if isinstance(mapped, list) and mapped:
        mapped_repr: Any = [_mapper_item_repr(item) for item in mapped]
    else:
        mapped_repr = _mapper_item_repr(mapped)

    assert mapped_repr == expected, (
        f"Mapper {mapper_ref!r} for {method.name} returned a shape that "
        f"does not match the fixture's mapper_expected.\n"
        f"Got: {mapped_repr!r}\nExpected: {expected!r}"
    )


# Methods whose fixtures are expected to carry a wired ``mapper`` /
# ``mapper_expected`` pair so the decoder->dataclass seam is golden-pinned (not
# merely skipped). The guard below fails loudly if any of them loses its mapper
# wiring, converting the historical silent skip into a zero-cost ratchet.
#
# Only methods whose feature path has a CLEAN single-payload mapper are listed.
# The remaining methods stay honestly skipped because their feature path either:
#   * returns ``None`` on success (fire-and-forget mutations: CREATE_NOTE,
#     DELETE_*, RENAME_*, SHARE_*, SET_USER_SETTINGS, REMOVE_RECENTLY_VIEWED,
#     RETRY_ARTIFACT, REVISE_SLIDE, the *_RESEARCH starters, …);
#   * extracts inline via ``safe_index`` with no centralised mapper
#     (GET_SOURCE's field-by-field ``SourceFulltext`` build, GET_SOURCE_GUIDE,
#     conversation/user-settings/tier reads, GET_INTERACTIVE_HTML, …);
#   * has no decoded payload to map (UPDATE_SOURCE decodes to ``null``); or
#   * reconciles the raw decode against client-side state rather than returning
#     it directly (CREATE_NOTEBOOK feeds the payload to ``Notebook.from_api_response``
#     but the feature return comes from a baseline-id-diff + ``_probe`` step, so
#     the raw-decode shape is not the public return the fixture would pin).
# Wiring those would require contorting the harness or adding production code
# for tests, so they are deliberately exempt rather than forced.
_MAPPER_COVERED_METHODS: tuple[RPCMethod, ...] = (
    RPCMethod.POLL_RESEARCH,
    RPCMethod.LIST_NOTEBOOKS,
    RPCMethod.GET_NOTEBOOK,
    RPCMethod.ADD_SOURCE,
    RPCMethod.LIST_ARTIFACTS,
    RPCMethod.LIST_LABELS,
    RPCMethod.GET_SHARE_STATUS,
    RPCMethod.GET_SUGGESTED_REPORTS,
    RPCMethod.SUGGEST_PROMPTS,
)


def test_mapper_covered_methods_have_mappers() -> None:
    """Methods listed as mapper-covered must keep their wired mapper goldens.

    Mirrors ``test_drift_prone_methods_have_drift_cases``: if a future edit
    drops the ``mapper`` / ``mapper_expected`` pair from one of these fixtures,
    the suite fails loudly here rather than silently degrading the
    ``test_mapper_output_shape_when_documented`` row back into a skip.
    """
    missing = []
    for method in _MAPPER_COVERED_METHODS:
        fixture = _load_fixture(method)
        if not fixture.get("mapper") or "mapper_expected" not in fixture:
            missing.append(method.name)
    assert not missing, (
        f"Mapper-covered methods missing a 'mapper' / 'mapper_expected' pair: "
        f"{missing}. Restore the decoder->dataclass golden for each (see "
        f"tests/unit/_golden_mappers.py)."
    )


# Drift-case exception names a fixture may declare, mapped to the concrete
# decoder exception class. Restricting the allowed names keeps a fixture from
# silently asserting against a typo'd / non-existent exception.
_DRIFT_EXCEPTION_TYPES: dict[str, type[RPCError]] = {
    "RPCError": RPCError,
    "ClientError": ClientError,
    "RateLimitError": RateLimitError,
    "UnknownRPCMethodError": UnknownRPCMethodError,
}

# Methods whose fixtures are expected to carry a ``drift_cases`` block. These
# are the drift-prone methods called out in the gap review: artifact creation,
# source attach, a research start, and the notebook list. The guard below fails
# loudly if any of them loses its drift coverage.
_DRIFT_COVERED_METHODS: tuple[RPCMethod, ...] = (
    RPCMethod.CREATE_ARTIFACT,
    RPCMethod.ADD_SOURCE,
    RPCMethod.START_FAST_RESEARCH,
    RPCMethod.LIST_NOTEBOOKS,
)


def _collect_drift_cases() -> list[tuple[str, RPCMethod, dict[str, Any]]]:
    """Flatten every fixture's ``drift_cases`` into parametrize tuples.

    Returns ``(case_id, method, case)`` triples so each drift scenario is an
    individually addressable test row.
    """
    collected: list[tuple[str, RPCMethod, dict[str, Any]]] = []
    for method in ALL_METHODS:
        # Runs at import/collection time, so a missing or malformed fixture must
        # not abort collection here — the dedicated guard tests
        # (test_every_rpc_method_has_a_fixture / the schema checks) own those
        # failures and emit far clearer messages than a collection-time crash.
        try:
            fixture = _load_fixture(method)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        for case in fixture.get("drift_cases", []):
            name = case.get("name", "unnamed")
            collected.append((f"{method.name}-{name}", method, case))
    return collected


_DRIFT_CASES = _collect_drift_cases()


def test_drift_prone_methods_have_drift_cases() -> None:
    """The drift-prone methods must each ship a non-empty ``drift_cases`` block.

    This is the load-bearing guard for the error-path coverage: if a future
    edit drops the ``drift_cases`` from one of these fixtures, the suite fails
    loudly rather than silently losing the decoder's error-path goldens.
    """
    missing = []
    for method in _DRIFT_COVERED_METHODS:
        fixture = _load_fixture(method)
        cases = fixture.get("drift_cases")
        if not isinstance(cases, list) or not cases:
            missing.append(method.name)
    assert not missing, (
        f"Drift-prone methods missing a non-empty 'drift_cases' block: "
        f"{missing}. Restore the decoder error-path goldens for each."
    )


@pytest.mark.parametrize(
    ("method", "case"),
    [(method, case) for _, method, case in _DRIFT_CASES],
    ids=[case_id for case_id, _, _ in _DRIFT_CASES],
)
def test_decoder_drift_case_behaviour(method: RPCMethod, case: dict[str, Any]) -> None:
    """Each drift case asserts the decoder's exact error / multi-frame result.

    A case declares **exactly one** of ``expected_exception`` (the decoder
    must raise that class) or ``expected_decoded`` (the decoder must return
    that payload — used for multi-frame placeholder-then-final responses).
    """
    chunks = case["chunks"]
    allow_null = case.get("allow_null", False)
    raw_response = _build_wire_response(chunks)

    has_exception = "expected_exception" in case
    has_decoded = "expected_decoded" in case
    if has_exception == has_decoded:
        raise _FixtureSchemaError(
            f"Drift case {case.get('name')!r} for RPCMethod.{method.name} must "
            f"declare exactly one of 'expected_exception' / 'expected_decoded' "
            f"(file: {_fixture_path(method)})."
        )

    if has_exception:
        exc_name = case["expected_exception"]
        if exc_name not in _DRIFT_EXCEPTION_TYPES:
            raise _FixtureSchemaError(
                f"Drift case {case.get('name')!r} for RPCMethod.{method.name} "
                f"declares unknown expected_exception {exc_name!r}; allowed: "
                f"{sorted(_DRIFT_EXCEPTION_TYPES)} (file: {_fixture_path(method)})."
            )
        exc_type = _DRIFT_EXCEPTION_TYPES[exc_name]
        with pytest.raises(exc_type) as exc_info:
            decode_response(raw_response, method.value, allow_null=allow_null)
        # Assert the EXACT class, not just an IS-A match: ClientError /
        # RateLimitError / UnknownRPCMethodError all subclass RPCError, so a
        # bare ``pytest.raises(RPCError)`` would not catch a regression that
        # raised the wrong (broader/narrower) subtype.
        assert type(exc_info.value) is exc_type, (
            f"Drift case {case['name']!r} for {method.name} expected exactly "
            f"{exc_name}, got {type(exc_info.value).__name__}."
        )
        substring = case.get("expected_message_substring")
        if substring is not None:
            assert substring.lower() in str(exc_info.value).lower(), (
                f"Drift case {case['name']!r} for {method.name}: message "
                f"{str(exc_info.value)!r} does not contain {substring!r}."
            )
        return

    expected = case["expected_decoded"]
    decoded = decode_response(raw_response, method.value, allow_null=allow_null)
    assert decoded == expected, (
        f"Drift case {case['name']!r} for {method.name} returned a payload "
        f"that does not match expected_decoded.\n"
        f"Got: {decoded!r}\nExpected: {expected!r}"
    )


# ===========================================================================
# Adapter field-position ground truth (PR-C, #1452)
# ===========================================================================
#
# The cli_vcr cassette-derived deep asserts (PR-B) catch fabrication, drop, and
# miscount, but they CANNOT catch *field-position* errors — a title<->url swap,
# a wrong nesting depth, a type<->status confusion all keep the right *count* of
# values, so a per-field equality on a recorded cassette would still pass while
# the wrong field was read. That residual is closed here with PINNED synthetic
# rows: every slot carries a DISTINCT, self-identifying literal (id="ID_AT_0",
# title="TITLE_AT_1", url="URL_AT_7_0", ...) so a wrong-slot read produces an
# obviously-wrong value, and a MUTATION half that swaps/shifts slots and proves
# the adapter reacts — i.e. "if the decoder regressed to read the wrong slot,
# THIS test would fail."
#
# These are unit tests over synthetic arrays (not cli_vcr cassettes), so the
# re-record-safety lint does not apply and pinned literals are correct here.
#
# Historical break-classes covered (the repo's #1 breakage class — see CLAUDE.md
# "Common Pitfalls": source id [id] vs [[[[id]]]], url precedence, type/status):
#   * Source id nesting ([id] vs drive [None, True, [id]] vs deeper fallback)
#   * Source URL precedence (metadata[7] > metadata[5] youtube > bare metadata[0])
#   * Source kind (type code -> SourceType) + status block (raw[3][1])
#   * Artifact variant ([9][1][0]) + media-url slots (audio [6][5], slide [16][3]/[4])
#   * Note current vs deleted vs legacy shapes (+ mind-map from_mind_map)
#
# Already covered elsewhere (intentionally NOT duplicated here):
#   * Position-constant pins + soft-degrade/strict edges live in
#     tests/unit/test_row_adapters.py (TestPositionContract, TestNoteRow*, ...).
#     This module adds the *planted-distinct-value* + *mutation-pair* angle the
#     constant pins do not: a constant pin proves "_URL_POS == 7", but not that a
#     real title and a real url cannot be confused at decode time.


def _make_source_metadata(
    *,
    bare0: Any = None,
    timestamp: float | None = None,
    type_code: int | None = None,
    youtube_url: Any = None,
    canonical_url: Any = None,
) -> list[Any]:
    """Build a source ``metadata`` sub-list with distinct values per slot.

    Mirrors the ``SourceRow`` metadata contract: ``[0]`` bare (legacy http),
    ``[2][0]`` timestamp, ``[4]`` type code, ``[5][0]`` youtube url, ``[7][0]``
    canonical url. Every other slot is left ``None`` so a wrong-slot read lands
    on a recognisably empty position.
    """
    meta: list[Any] = [None] * 8
    meta[0] = bare0
    meta[2] = [timestamp] if timestamp is not None else None
    meta[4] = type_code
    meta[5] = [youtube_url] if youtube_url is not None else None
    meta[7] = [canonical_url] if canonical_url is not None else None
    return meta


def _make_artifact_row(
    *,
    artifact_id: str = "ID_AT_0",
    title: str = "TITLE_AT_1",
    type_code: int = ArtifactTypeCode.AUDIO.value,
    status: int = ArtifactStatus.COMPLETED.value,
    variant: int | None = None,
    audio_media_list: list[Any] | None = None,
    slide_pdf: Any = None,
    slide_pptx: Any = None,
) -> list[Any]:
    """Build an artifact row long enough to carry every pinned slot.

    Slots filled at their canonical ``ArtifactRow`` positions: id ``[0]``,
    title ``[1]``, type ``[2]``, status ``[4]``, variant ``[9][1][0]``, audio
    media list ``[6][5]``, slide-deck pdf ``[16][3]`` / pptx ``[16][4]``.

    ``audio_media_list`` is the media list that lands at ``[6][5]`` (the inner
    list of ``[url, kind, mime]`` entries), NOT the outer ``[6]`` audio-metadata
    block — the factory wraps it under a fresh ``[6]`` envelope.
    """
    row: list[Any] = [None] * 19
    row[0] = artifact_id
    row[1] = title
    row[2] = type_code
    row[4] = status
    if variant is not None:
        row[9] = [None, [variant]]
    if audio_media_list is not None:
        audio_block: list[Any] = [None] * 6
        audio_block[5] = audio_media_list
        row[6] = audio_block
    if slide_pdf is not None or slide_pptx is not None:
        row[16] = [None, None, None, slide_pdf, slide_pptx]
    return row


# ---------------------------------------------------------------------------
# Source: id nesting ground truth ([id] vs drive [None, True, [id]] vs fallback)
# ---------------------------------------------------------------------------


class TestSourceIdNestingGroundTruth:
    """Pin the id at each nesting depth the ``SourceRow`` id-unwrap supports."""

    def test_plain_id_envelope_at_slot_0(self) -> None:
        # Typical wrapping: raw[0] == ["id"], id read at envelope plain pos 0.
        row = SourceRow.from_entry([["ID_AT_PLAIN_0"], "TITLE_AT_1", None])
        assert row.id == "ID_AT_PLAIN_0"

    def test_bare_string_id_envelope_flat_shape(self) -> None:
        # Flat shape: raw[0] is the bare id string itself.
        row = SourceRow.from_unknown_shape(["BARE_ID", "TITLE_AT_1"])
        assert row.shape is SourceRowShape.FLAT
        assert row.id == "BARE_ID"

    def test_drive_backed_id_nested_at_2_0(self) -> None:
        # Drive-backed entries nest the id one level deeper at raw[0][2][0].
        row = SourceRow.from_entry([[None, True, ["DRIVE_ID_AT_2_0"]], "TITLE_AT_1", None])
        assert row.id == "DRIVE_ID_AT_2_0"

    def test_medium_nested_dispatch_unwraps_one_level(self) -> None:
        # Medium nested [[[id], title, meta]] -> entry at data[0].
        row = SourceRow.from_unknown_shape([[["MED_ID"], "TITLE_AT_1", None]])
        assert row.shape is SourceRowShape.MEDIUM_NESTED
        assert row.id == "MED_ID"

    def test_deeply_nested_dispatch_unwraps_two_levels(self) -> None:
        # Deeply nested [[[[id], title, meta]]] -> entry at data[0][0]. This is
        # the [[[[id]]]]-style fallback called out in CLAUDE.md pitfall #3.
        row = SourceRow.from_unknown_shape([[[["DEEP_ID"], "TITLE_AT_1", None]]])
        assert row.shape is SourceRowShape.DEEPLY_NESTED
        assert row.id == "DEEP_ID"


# ---------------------------------------------------------------------------
# Source: URL precedence + youtube fallback ground truth
# ---------------------------------------------------------------------------


class TestSourceUrlPrecedenceGroundTruth:
    """Pin the url-resolution order: canonical[7] > youtube[5] > bare[0]."""

    def test_canonical_url_at_7_wins_over_youtube_at_5(self) -> None:
        meta = _make_source_metadata(
            canonical_url="https://canonical.example/AT_7_0",
            youtube_url="https://youtu.be/AT_5_0",
        )
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", meta])
        assert row.url == "https://canonical.example/AT_7_0"

    def test_youtube_at_5_used_when_canonical_absent(self) -> None:
        # No metadata[7] -> youtube block at metadata[5][0] is the fallback.
        meta = _make_source_metadata(youtube_url="https://youtu.be/AT_5_0")
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", meta])
        assert row.url == "https://youtu.be/AT_5_0"

    def test_bare_http_at_0_only_on_deeply_nested(self) -> None:
        # Bare http at metadata[0] is honored ONLY on the deeply-nested shape
        # (url_allow_bare_http=True). On a medium/entry shape it must be ignored.
        meta = _make_source_metadata(bare0="https://bare.example/AT_0")
        entry = SourceRow.from_entry([["ID"], "TITLE_AT_1", meta])
        assert entry.url is None
        deep = SourceRow.from_unknown_shape([[[["ID"], "TITLE_AT_1", meta]]])
        assert deep.url_allow_bare_http is True
        assert deep.url == "https://bare.example/AT_0"

    def test_youtube_block_non_string_first_element_not_a_url(self) -> None:
        # The youtube block's first element must be a *string* to count as a url;
        # a non-string (e.g. a video-id int) at [5][0] is not a url.
        meta: list[Any] = [None] * 8
        meta[5] = [12345, "ignored"]
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", meta])
        assert row.url is None


# ---------------------------------------------------------------------------
# Source: kind (type code -> SourceType) + status block ground truth
# ---------------------------------------------------------------------------


class TestSourceKindAndStatusGroundTruth:
    """Pin the type-code -> SourceType mapping and the status-block decode."""

    @pytest.mark.parametrize(
        ("type_code", "expected_kind"),
        [
            (1, SourceType.GOOGLE_DOCS),
            (3, SourceType.PDF),
            (4, SourceType.PASTED_TEXT),
            (5, SourceType.WEB_PAGE),
            (9, SourceType.YOUTUBE),
        ],
    )
    def test_type_code_at_metadata_4_maps_to_kind(
        self, type_code: int, expected_kind: SourceType
    ) -> None:
        meta = _make_source_metadata(type_code=type_code)
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", meta])
        assert row.type_code == type_code
        # The kind enum is derived from the same metadata[4] slot.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            source = Source.from_row(row)
        assert source.kind is expected_kind

    @pytest.mark.parametrize(
        ("status_code", "expected_status"),
        [
            (1, SourceStatus.PROCESSING),
            (2, SourceStatus.READY),
            (3, SourceStatus.ERROR),
            (5, SourceStatus.PREPARING),
        ],
    )
    def test_status_code_at_raw_3_1_maps_to_status(
        self, status_code: int, expected_status: SourceStatus
    ) -> None:
        # The status code lives at raw[3][1]; raw[3][0] is a decoy that must be
        # ignored so a [3][0]/[3][1] confusion would be caught.
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", None, ["DECOY_AT_3_0", status_code]])
        assert row.status is expected_status

    def test_unknown_status_code_falls_back_to_ready(self) -> None:
        row = SourceRow.from_entry([["ID"], "TITLE_AT_1", None, [None, 99]])
        assert row.status is SourceStatus.READY


# ---------------------------------------------------------------------------
# Artifact: variant + media-url position ground truth
# ---------------------------------------------------------------------------


class TestArtifactVariantGroundTruth:
    """Pin the quiz/flashcards/mind-map variant code at [9][1][0]."""

    @pytest.mark.parametrize(
        ("variant", "expected_kind"),
        [
            (FLASHCARDS_VARIANT, ArtifactType.FLASHCARDS),
            (QUIZ_VARIANT, ArtifactType.QUIZ),
            (INTERACTIVE_MIND_MAP_VARIANT, ArtifactType.MIND_MAP),
        ],
    )
    def test_variant_at_9_1_0_drives_type4_kind(
        self, variant: int, expected_kind: ArtifactType
    ) -> None:
        row = _make_artifact_row(type_code=ArtifactTypeCode.QUIZ.value, variant=variant)
        assert ArtifactRow(row).variant == variant
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            artifact = Artifact.from_api_response(row)
        assert artifact.kind is expected_kind


class TestArtifactMediaUrlGroundTruth:
    """Pin the media-url slots: audio [6][5], slide-deck pdf [16][3]/pptx [16][4]."""

    def test_audio_url_prefers_mp4_entry_in_6_5(self) -> None:
        media = [
            ["https://media.example/OGG_AT_0", 1, "audio/ogg"],
            ["https://media.example/MP4_AT_0", 2, "audio/mp4"],
        ]
        row = _make_artifact_row(type_code=ArtifactTypeCode.AUDIO.value, audio_media_list=media)
        assert ArtifactRow(row).audio_url == "https://media.example/MP4_AT_0"

    def test_slide_deck_pdf_and_pptx_read_distinct_slots(self) -> None:
        row = _make_artifact_row(
            type_code=ArtifactTypeCode.SLIDE_DECK.value,
            slide_pdf="https://slides.example/PDF_AT_16_3",
            slide_pptx="https://slides.example/PPTX_AT_16_4",
        )
        ar = ArtifactRow(row)
        assert ar.slide_deck_pdf_url == "https://slides.example/PDF_AT_16_3"
        assert ar.slide_deck_pptx_url == "https://slides.example/PPTX_AT_16_4"

    def test_infographic_url_scans_url_bearing_content_block(self) -> None:
        # infographic_url scans for item[2][0][1] -> a url-bearing list.
        #
        # No paired mutation test exists for the infographic accessor (unlike
        # audio / slide-deck / variant): it does not read a single pinned slot
        # but scans every top-level item for the first ``item[2][0][1]``
        # url-bearing list, so there is no fixed index to "shift" for a
        # slot-confusion mutation. The pinned ground truth here — a distinct
        # IMG_URL literal recovered from a precise nested shape — is the
        # available field-position assertion for this accessor.
        row = _make_artifact_row(type_code=ArtifactTypeCode.INFOGRAPHIC.value)
        row[7] = [None, None, [[None, ["https://infographic.example/IMG_URL"]]]]
        assert ArtifactRow(row).infographic_url == "https://infographic.example/IMG_URL"


# ---------------------------------------------------------------------------
# Note: current vs deleted vs legacy shape ground truth
# ---------------------------------------------------------------------------


class TestNoteShapeGroundTruth:
    """Pin the three note wire shapes the ``NoteRow`` adapter absorbs."""

    def test_legacy_shape_content_at_1(self) -> None:
        # Legacy: [id, content_string]. No title slot -> "".
        row = NoteRow(["NOTE_ID", "CONTENT_AT_1"])
        assert row.id == "NOTE_ID"
        assert row.content == "CONTENT_AT_1"
        assert row.title == ""
        assert row.is_deleted is False

    def test_current_shape_content_at_1_1_title_at_1_4(self) -> None:
        # Current: [id, [id, content, meta, None, title]].
        inner = ["NOTE_ID", "CONTENT_AT_1_1", [1, "u", [1700000000, 0]], None, "TITLE_AT_1_4"]
        row = NoteRow(["NOTE_ID", inner])
        assert row.id == "NOTE_ID"
        assert row.content == "CONTENT_AT_1_1"
        assert row.title == "TITLE_AT_1_4"
        assert row.is_deleted is False

    def test_deleted_shape_sentinel_at_2(self) -> None:
        # Deleted: [id, None, 2] -> is_deleted, content/title degrade.
        row = NoteRow(["NOTE_ID", None, 2])
        assert row.is_deleted is True
        assert row.content is None
        assert row.title == ""

    def test_mind_map_current_shape_via_from_mind_map(self) -> None:
        inner = ["MM_ID", '{"nodes": []}', [1, "u", [1700000000, 0]], None, "MM_TITLE_AT_1_4"]
        artifact = Artifact.from_mind_map(["MM_ID", inner])
        assert artifact is not None
        assert artifact.id == "MM_ID"
        assert artifact.title == "MM_TITLE_AT_1_4"
        assert artifact.kind is ArtifactType.MIND_MAP

    def test_mind_map_deleted_shape_returns_none(self) -> None:
        assert Artifact.from_mind_map(["MM_ID", None, 2]) is None


# ===========================================================================
# MUTATION tests — prove the field-position contracts have TEETH
# ===========================================================================
#
# Each mutation takes a CORRECT synthetic row, moves one value to a different
# slot (or shifts a nesting depth), and asserts the adapter reacts: a swapped
# value is now read from the wrong place, a shifted id is NOT silently
# mis-extracted, a swapped type/status flips the decoded enums. If the decoder
# ever regressed to read the (now-mutated) wrong slot, the paired assertion
# would fail.


class TestSourceFieldConfusionHasTeeth:
    """A title<->url swap and an id-nesting shift must be detectable."""

    def test_title_url_swap_is_detectable(self) -> None:
        url = "https://real.example/page"
        title = "My Source Title"
        # Correct row: title at [1], url at metadata[7][0].
        correct = SourceRow.from_entry([["ID"], title, _make_source_metadata(canonical_url=url)])
        assert correct.title == title
        assert correct.url == url
        assert (correct.url or "").startswith("http")

        # Mutated row: the two values are swapped between their slots. A decoder
        # that confused title<->url would now surface the title where the url
        # belongs — so the url no longer parses as a URL.
        mutated = SourceRow.from_entry([["ID"], url, _make_source_metadata(canonical_url=title)])
        assert mutated.title == url
        # The teeth are in the line above: the title string now occupies the url
        # slot, so ``mutated.url`` returns it verbatim. The ``startswith`` check
        # below is a derived property of that planted title literal (not a probe
        # of any adapter url-validation logic) — kept only as a human-readable
        # restatement that a title in the url slot is not URL-shaped.
        assert mutated.url == title
        assert not (mutated.url or "").startswith("http"), (
            "field-confusion teeth: a title in the url slot must NOT pass as a URL"
        )

    def test_id_nesting_shift_is_not_silently_mis_extracted(self) -> None:
        # Correct: id is a bare string inside the plain envelope ["id"].
        correct = SourceRow.from_entry([["GOOD_ID"], "T", None])
        assert correct.id == "GOOD_ID"

        # Mutation A: id nested one level too deep -> [["TOO_DEEP"]]. The adapter
        # must NOT silently surface "TOO_DEEP"; it stringifies the wrong-shaped
        # envelope instead, so the corruption is visible.
        too_deep = SourceRow.from_entry([[["TOO_DEEP_ID"]], "T", None])
        assert too_deep.id != "TOO_DEEP_ID"

        # Mutation B: a drive-style envelope with the id shifted OUT of [2][0]
        # (placed shallow at [1]) must not be picked up from the wrong slot.
        drive_shifted = SourceRow.from_entry([[None, "SHALLOW_ID", []], "T", None])
        assert drive_shifted.id != "SHALLOW_ID"
        assert drive_shifted.id == ""

    @pytest.mark.parametrize(
        ("type_code", "status_code", "expected_kind", "expected_status"),
        [
            # correct pairing
            (9, 1, SourceType.YOUTUBE, SourceStatus.PROCESSING),
            # swapped: the YOUTUBE code now sits in the status slot and vice
            # versa, so kind/status must change accordingly.
            (1, 9, SourceType.GOOGLE_DOCS, SourceStatus.READY),
        ],
    )
    def test_type_status_swap_flips_decoded_enums(
        self,
        type_code: int,
        status_code: int,
        expected_kind: SourceType,
        expected_status: SourceStatus,
    ) -> None:
        meta = _make_source_metadata(type_code=type_code)
        row = SourceRow.from_entry([["ID"], "T", meta, [None, status_code]])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            source = Source.from_row(row)
        assert source.kind is expected_kind
        assert source.status is expected_status


class TestArtifactFieldConfusionHasTeeth:
    """A type<->status swap and a variant-nesting shift must be detectable."""

    @pytest.mark.parametrize(
        ("type_code", "status_code", "expected_type", "expected_status"),
        [
            # correct: AUDIO type, COMPLETED status
            (
                ArtifactTypeCode.AUDIO.value,
                ArtifactStatus.COMPLETED.value,
                ArtifactTypeCode.AUDIO.value,
                ArtifactStatus.COMPLETED.value,
            ),
            # swapped: the codes trade slots ([2] type <-> [4] status), so the
            # decoded type_code and status must trade too.
            (
                ArtifactStatus.COMPLETED.value,
                ArtifactTypeCode.AUDIO.value,
                ArtifactStatus.COMPLETED.value,
                ArtifactTypeCode.AUDIO.value,
            ),
        ],
    )
    def test_type_status_swap_flips_decoded_codes(
        self,
        type_code: int,
        status_code: int,
        expected_type: int,
        expected_status: int,
    ) -> None:
        row = _make_artifact_row(type_code=type_code, status=status_code)
        ar = ArtifactRow(row)
        assert ar.type_code == expected_type
        assert ar.status == expected_status

    def test_variant_nesting_shift_raises_strict_drift(self) -> None:
        # Correct: variant at [9][1][0].
        correct = _make_artifact_row(type_code=ArtifactTypeCode.QUIZ.value, variant=QUIZ_VARIANT)
        assert ArtifactRow(correct).variant == QUIZ_VARIANT

        # Mutation: shift the variant one level shallower so [9] == [None, 2].
        # The adapter descends [1][0] through ``safe_index`` and the int at [1]
        # is not indexable -> strict-mode UnknownRPCMethodError. A regression
        # that read [9][1] directly would silently return 2 instead of raising.
        shifted = _make_artifact_row(type_code=ArtifactTypeCode.QUIZ.value)
        shifted[9] = [None, QUIZ_VARIANT]
        with pytest.raises(UnknownRPCMethodError):
            _ = ArtifactRow(shifted).variant

    def test_slide_deck_pdf_pptx_slot_swap_is_detectable(self) -> None:
        # Correct: pdf at [16][3], pptx at [16][4].
        correct = _make_artifact_row(
            type_code=ArtifactTypeCode.SLIDE_DECK.value,
            slide_pdf="https://slides.example/PDF",
            slide_pptx="https://slides.example/PPTX",
        )
        ar = ArtifactRow(correct)
        assert ar.slide_deck_pdf_url == "https://slides.example/PDF"
        assert ar.slide_deck_pptx_url == "https://slides.example/PPTX"

        # Mutation: swap the two urls between [16][3] and [16][4]. The pdf
        # accessor now returns the PPTX url and vice versa — a [3]/[4] confusion
        # is visible.
        swapped = _make_artifact_row(
            type_code=ArtifactTypeCode.SLIDE_DECK.value,
            slide_pdf="https://slides.example/PPTX",
            slide_pptx="https://slides.example/PDF",
        )
        sw = ArtifactRow(swapped)
        assert sw.slide_deck_pdf_url == "https://slides.example/PPTX"
        assert sw.slide_deck_pptx_url == "https://slides.example/PDF"


class TestNoteShapeConfusionHasTeeth:
    """A current/deleted shape confusion must change the decoded classification."""

    def test_deleted_sentinel_shift_changes_classification(self) -> None:
        # Correct deleted shape: [id, None, 2].
        assert NoteRow(["ID", None, 2]).is_deleted is True

        # Mutation A: move the sentinel off slot 2 -> not deleted.
        assert NoteRow(["ID", None, 99]).is_deleted is False

        # Mutation B: a non-None content slot with the sentinel at [2] is also
        # not a delete (deletion requires BOTH the None content and the
        # sentinel) -> the [1]-is-None half of the contract has teeth.
        assert NoteRow(["ID", "still here", 2]).is_deleted is False

    def test_content_title_inner_slot_swap_is_detectable(self) -> None:
        # Correct current shape: content at [1][1], title at [1][4].
        good_inner = ["ID", "REAL_CONTENT", [1, "u", [0, 0]], None, "REAL_TITLE"]
        good = NoteRow(["ID", good_inner])
        assert good.content == "REAL_CONTENT"
        assert good.title == "REAL_TITLE"

        # Mutation: swap content<->title between [1][1] and [1][4]. A decoder
        # that confused the inner content/title slots would now surface the
        # title as content and vice versa.
        swapped_inner = ["ID", "REAL_TITLE", [1, "u", [0, 0]], None, "REAL_CONTENT"]
        swapped = NoteRow(["ID", swapped_inner])
        assert swapped.content == "REAL_TITLE"
        assert swapped.title == "REAL_CONTENT"
