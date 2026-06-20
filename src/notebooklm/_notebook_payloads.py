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
#
# KNOWN (live-verified on the consumer/labs cohort, issue #1612):
#   * REQUIRED: ``0`` / omitted makes the server return a gRPC ``INTERNAL`` error.
#   * Every value ``1..9`` returns a populated suggestion list. The titles vary
#     per call (LLM-generated, non-deterministic), but the *framing* is a STABLE,
#     reproducible function of ``mode`` — it selects which product surface the
#     suggestions are written for (live A/B: same notebook+query, only ``mode``
#     varies → mode 6 returns 100% debate scaffolding, nothing like mode 4). So
#     the values are NOT interchangeable.
#
# ``mode`` -> product surface (live-characterized, fixed notebook+query, 2026-06-20):
#   4 (default) -> "ask about the content" chat questions (History Focus, Feature
#                  Breakdown, For Students) — the web chat surface's own default.
#   5           -> critique / evaluate (Evaluate Narrative Flow, Analyze Originality).
#   6           -> audio overview / debate (Legal Ethics Debate; opening statement
#                  + cross-examination; point-counterpoint).
#   8           -> quiz ("Test knowledge on…", Quiz Platform Mechanics).
#   9           -> flashcards (front=feature / back=function, mnemonics).
#   1, 2, 3, 7  -> general Q&A, ~indistinguishable from 4.
#
# STRUCTURAL (web bundle ``boq-labs-tailwind`` build A5FPq5ae8CY): this request
# proto's ``C0`` has the closure enum allow-list ``[4, 5]`` (``IBb`` -> ``HRa(a, 4,
# HBb, b)`` with ``HBb = [4, 5]``) — the *chat* surface declares two members and
# only sends 4 or 5. ``GeneratePromptSuggestions`` is a GENERAL notebook-prompt
# endpoint: the web client wires only chat (4/5) into it and drives report prompts
# through a SEPARATE RPC (``GenerateReportSuggestions`` / ``ciyUvf``); audio/quiz/
# flashcards have no web caller. The higher codes (6/8/9) are surface codes the
# *backend* recognizes (used by some non-web client / future wiring), reachable by
# passing them here — they are real, distinct surfaces (proven live above), not
# something the web client sends.
#
# Stays a plain ``int``, NOT a named enum: the bundle yields the enum *values*
# (4, 5) but not Google's member *names*; the surface labels above are inferred
# from the LLM output, so naming the members would be fabrication ("don't invent
# enum values"). DEFAULT = 4 matches the web chat surface's default member (one
# of its two declared values), corroborated by the bundle enum — not arbitrary.
#
# RANGE: the lib caps at ``1..9``. The true server-valid range is ``1..10`` (0 and
# 11+ -> INTERNAL; mode 10 is a distinct "short titles/headlines" surface), but
# 10 is intentionally left unexposed — the cap is deliberately narrower than the
# server max, not a bug.
_PROMPT_SUGGESTIONS_DEFAULT_MODE = 4
_PROMPT_SUGGESTIONS_MODE_MIN = 1
_PROMPT_SUGGESTIONS_MODE_MAX = 9


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
            ``1..9`` (``0`` / omitted makes the server return ``INTERNAL``). An
            out-of-range value raises ``ValueError`` here rather than reaching
            the server. See ``_PROMPT_SUGGESTIONS_DEFAULT_MODE`` for the known /
            unknown semantics (label mapping unrecovered; default ``4`` is the
            issue's live-verified value, not a recovered default).
        query: Optional free-text steer; ``None`` (or an empty / whitespace-only
            string, normalised to ``None``) sends a null in slot 6.

    Raises:
        ValueError: if ``mode`` is outside the inclusive ``1..9`` range.
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
