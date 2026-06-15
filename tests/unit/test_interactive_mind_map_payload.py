"""Snapshot test for the interactive-mind-map CREATE_ARTIFACT payload builder.

The expected shape is verified live against the captured GUI request (#1256):
``[client_options, nb, [None,None,4,<triple src ids>,None,None,None,None,None,[None,[4]]]]``.
"""

from __future__ import annotations

from notebooklm._artifact.payloads import build_interactive_mind_map_artifact_params

_ARTIFACT_CLIENT_OPTIONS = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1]],
    [[1, 4, 8, 2, 3, 6]],
]


def test_single_source_exact_shape():
    params = build_interactive_mind_map_artifact_params("nb1", ["s1"])
    assert params == [
        _ARTIFACT_CLIENT_OPTIONS,
        "nb1",
        [None, None, 4, [[["s1"]]], None, None, None, None, None, [None, [4]]],
    ]


def test_distinct_from_quiz_and_flashcards_variants():
    spec = build_interactive_mind_map_artifact_params("nb1", ["s1"])[2]
    # type-4 family, but variant 4 (not 2=quiz / 1=flashcards) and no config tail.
    assert spec[2] == 4
    assert spec[9] == [None, [4]]


def test_instructions_injected_at_prompt_slot():
    # A custom prompt goes to [9][1][2] — the same slot quiz/flashcards use and
    # the slot ArtifactRow.generation_prompt reads back (server-verified to steer
    # the generated tree for variant 4).
    spec = build_interactive_mind_map_artifact_params(
        "nb1", ["s1"], instructions="focus only on the astronauts"
    )[2]
    assert spec[9] == [None, [4, None, "focus only on the astronauts"]]
    assert spec[9][1][0] == 4  # variant still at [9][1][0]


def test_none_instructions_keeps_bare_variant_shape():
    # Default / explicit None must stay byte-identical to the original request so
    # the no-prompt path (and its recorded cassettes / idempotency key) is unchanged.
    assert build_interactive_mind_map_artifact_params(
        "nb1", ["s1"], instructions=None
    ) == build_interactive_mind_map_artifact_params("nb1", ["s1"])


def test_empty_or_whitespace_instructions_keeps_bare_variant_shape():
    # Empty / whitespace-only instructions are treated as no prompt: same bare
    # [None, [4]] shape as None, so a blank prompt is never sent to the server.
    baseline = build_interactive_mind_map_artifact_params("nb1", ["s1"])
    for blank in ("", "   ", "\n\t "):
        assert build_interactive_mind_map_artifact_params("nb1", ["s1"], instructions=blank) == (
            baseline
        )
