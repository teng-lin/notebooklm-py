"""Snapshot test for the interactive-mind-map CREATE_ARTIFACT payload builder.

The expected shape is verified live against the captured GUI request (#1256):
``[[2], nb, [None,None,4,<triple src ids>,None,None,None,None,None,[None,[4]]]]``.
"""

from __future__ import annotations

from notebooklm._artifact_payloads import build_interactive_mind_map_artifact_params


def test_single_source_exact_shape():
    params = build_interactive_mind_map_artifact_params("nb1", ["s1"])
    assert params == [
        [2],
        "nb1",
        [None, None, 4, [[["s1"]]], None, None, None, None, None, [None, [4]]],
    ]


def test_distinct_from_quiz_and_flashcards_variants():
    spec = build_interactive_mind_map_artifact_params("nb1", ["s1"])[2]
    # type-4 family, but variant 4 (not 2=quiz / 1=flashcards) and no config tail.
    assert spec[2] == 4
    assert spec[9] == [None, [4]]
