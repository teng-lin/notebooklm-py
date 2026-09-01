"""Stable ``batchexecute`` notebook RPC request payload builders.

Kept outside ``_web/notebooks.py`` so the web notebook implementation stays
under the ADR-0008 module-size budget; mirrors the ``_web.settings`` /
``_web.params.sources`` split.
"""

from __future__ import annotations

from typing import Any

from ...rpc import nest_source_ids
from .sources import build_template_block


def build_create_notebook_params(title: str) -> list[Any]:
    """Return the canonical CREATE_NOTEBOOK RPC payload.

    The trailing :func:`build_template_block` replaced the old flat ``[2], [1]``
    tail that migrated backends now reject with ``status=3`` (#1546).
    """
    return [title, None, None, build_template_block()]


def build_copy_notebook_params(source_notebook_id: str, title: str) -> list[Any]:
    """Return the canonical ``CopyProject`` (``te3DCe``) payload.

    The current web bundle serializes a request context at field 1, the source
    project id at field 2, and the new title at field 3. Unlike
    :func:`build_create_notebook_params`, the context therefore leads the
    positional request rather than trailing it.
    """
    return [build_template_block(), source_notebook_id, title]


def build_get_notebook_params(notebook_id: str) -> list[Any]:
    """Return the canonical GET_NOTEBOOK (``rLM1Ne``) RPC payload.

    The Gemini-3.5 rollout migrated the read path's trailing template block from
    the flat ``[2]`` to the same nested :func:`build_template_block` wrapper the
    write path adopted in #1548 (issue #1549). Live-verified forward-compatible:
    the nested shape returns a byte-identical decoded notebook (notebook id /
    title and every ``SourceRow``) as the flat ``[2]`` on an un-migrated account,
    so it is safe across cohorts. The trailing ``None, 0`` is unchanged — only
    the template block at position 2 is migrated (the narrow scope #1549 tracks).
    """
    return [notebook_id, None, build_template_block(), None, 0]


def build_update_notebook_params(
    notebook_id: str,
    *,
    title: str | None = None,
    emoji: str | None = None,
) -> list[Any]:
    """Return the ``MutateProject`` change-property payload.

    ``ProjectMutation.changeProperty`` is the fourth mutation variant. Inside
    that block, tag 2 is the title and tag 3 is the emoji, so the positional
    JSON is ``[None, title, emoji]``. The trailing emoji slot is omitted when
    it is not being changed, preserving the long-standing title-only request
    shape (and its recorded-cassette contract). ``None`` leaves a property
    unchanged; callers validate that at least one value is supplied.
    """
    change_property = [None, title]
    if emoji is not None:
        change_property.append(emoji)
    return [notebook_id, [[None, None, None, change_property]]]


# The required ``C0`` "mode/surface" enum (field 4 of the SUGGEST_PROMPTS request).
# ``0`` / omitted -> gRPC INTERNAL; ``1..10`` return a populated suggestion list;
# ``11+`` -> INTERNAL. Two complementary LIVE investigations characterize it; the
# full map and the method each was established by are inlined below:
#
# SURFACE axis (#1726, headless-browser + real web captures, 2026-07-01): each studio
# Customize dialog SENDS a specific mode for its format's prompt suggestions.
# Browser-verified by opening the Audio Customize dialog and clicking each format
# card (decoding the exact otmP3b mode), plus real Video web captures:
#   1 Audio·DeepDive   2 Audio·Brief   5 Audio·Critique   6 Audio·Debate
#   3 Video·Explainer  10 Video·Short   4 Chat/ask   8 Quiz   9 Flashcards
#   7 -> unidentified (no UI surface sends it).
#
# OUTPUT axis (#1612, live A/B 2026-06-20, fixed notebook+query, only ``mode`` varies):
# what the backend RETURNS for each mode. ``5`` critique / ``6`` debate scaffolding /
# ``8`` quiz / ``9`` flashcards are format-distinctive in the returned text; ``1, 2,
# 3, 7, 10`` return content-direction prompts (persona / format / topic) that read
# ~like the ``4`` default. That is CONSISTENT with the surface axis: for deep-dive /
# brief / explainer / short, NotebookLM steers the format via content direction (not
# format jargon), so their suggestion text is general by design. So the labels name
# the SURFACE (which format's dialog sends the mode), not a promise about output tone.
#
# Stays a plain ``int``, NOT a named enum (Google's member names aren't in the
# bundle). DEFAULT = 4 (the web chat surface's own default). The MCP
# ``suggest_prompts`` tool exposes friendly names over these modes.
_PROMPT_SUGGESTIONS_DEFAULT_MODE = 4
_PROMPT_SUGGESTIONS_MODE_MIN = 1
# Inclusive server-valid range is 1..10 (0 / 11+ -> INTERNAL). ``10`` = Video·Short.
_PROMPT_SUGGESTIONS_MODE_MAX = 10


def _prompt_suggestions_client_context() -> list[Any]:
    """Return the field-1 client-context block for ``SUGGEST_PROMPTS``.

    Same family as ``_web.params.artifacts._artifact_client_options`` but WITHOUT
    the trailing field-5 capability projection (``[[1, 4, 8, 2, 3, 6]]``): the
    live-verified ``otmP3b`` request carries only this 4-element capability
    envelope. Built fresh on each call so the returned (nested-mutable) list is
    never shared across requests.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def build_prompt_suggestions_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    mode: int = _PROMPT_SUGGESTIONS_DEFAULT_MODE,
    query: str | None = None,
) -> list[Any]:
    """Build ``SUGGEST_PROMPTS`` (``otmP3b``) params.

    Positional shape (live-verified)::

        [ ctx, notebook_id, [[source_id], ...], mode, None, query ]
          f1    f2          f3                  f4   —    f6

    Args:
        notebook_id: The notebook to suggest prompts for.
        source_ids: Source ids to scope the suggestions to; each is wrapped as
            ``[source_id]`` (``nest_source_ids(..., 1)`` →
            ``[[sid1], [sid2], ...]``). An empty list yields ``[]``.
        mode: The required ``C0`` int "mode/surface" enum, inclusive range
            ``1..10`` (``0`` / omitted makes the server return ``INTERNAL``). An
            out-of-range value raises ``ValueError`` here rather than reaching
            the server. See ``_PROMPT_SUGGESTIONS_DEFAULT_MODE`` for the known /
            unknown semantics (label mapping unrecovered; default ``4`` is the
            issue's live-verified value, not a recovered default).
        query: Optional free-text steer; ``None`` (or an empty / whitespace-only
            string, normalised to ``None``) sends a null in slot 6.

    Raises:
        ValueError: if ``mode`` is outside the inclusive ``1..10`` range.
    """
    if not _PROMPT_SUGGESTIONS_MODE_MIN <= mode <= _PROMPT_SUGGESTIONS_MODE_MAX:
        raise ValueError(
            f"mode must be in the inclusive range "
            f"{_PROMPT_SUGGESTIONS_MODE_MIN}..{_PROMPT_SUGGESTIONS_MODE_MAX}, got {mode!r}"
        )
    # An empty / whitespace-only steer carries no signal — normalise to None so
    # the default request stays byte-identical and no blank prompt is sent
    # (mirrors ``_web.params.artifacts.build_interactive_mind_map_artifact_params``).
    resolved_query = query if query and query.strip() else None
    return [
        _prompt_suggestions_client_context(),
        notebook_id,
        nest_source_ids(source_ids, 1),
        mode,
        None,
        resolved_query,
    ]


def build_next_step_suggestions_params(
    notebook_id: str, source_ids: list[str] | None = None
) -> list[Any]:
    """Build ``SUGGEST_NEXT_STEPS`` (``OcvKNc`` / ``NextStepSuggestions``) params.

    Mobile proto (live-pinned, #2283): ``{ string project_id = 2; repeated
    InputSource sources = 3 }`` with ``InputSource { SourceId source_id = 1 }``
    — so field 1 is unused (leading ``None``) and each scoping source id is
    wrapped twice (``[[id]]``, :func:`nest_source_ids` depth 2). Omitting field
    3 scopes the suggestions to every source in the notebook.
    """
    params: list[Any] = [None, notebook_id]
    if source_ids:
        params.append(nest_source_ids(list(source_ids), 2))
    return params
