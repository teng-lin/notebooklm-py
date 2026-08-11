"""Stable ``batchexecute`` notebook RPC request payload builders.

Currently the ``SUGGEST_PROMPTS`` (``otmP3b`` / ``GeneratePromptSuggestions``)
request builder backing :meth:`NotebooksAPI.suggest_prompts`. Kept in a sibling
module (rather than inline in ``_notebooks.py``) so the notebook RPC façade stays
under the ADR-0008 module-size budget; mirrors the ``_settings`` /
``_source.upload_payloads`` split.
"""

from __future__ import annotations

from typing import Any

from .rpc import nest_source_ids

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

    Same family as ``_artifact.payloads._artifact_client_options`` but WITHOUT
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
    # (mirrors ``_artifact.payloads.build_interactive_mind_map_artifact_params``).
    resolved_query = query if query and query.strip() else None
    return [
        _prompt_suggestions_client_context(),
        notebook_id,
        nest_source_ids(source_ids, 1),
        mode,
        None,
        resolved_query,
    ]
