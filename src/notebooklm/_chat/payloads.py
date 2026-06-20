"""Chat-feature ``batchexecute`` RPC payload builders.

Distinct from :mod:`._chat.wire`, which builds/parses the *streamed*
``GenerateFreeFormStreamed`` chat endpoint (not a ``batchexecute`` RPC). The
builders here construct the positional param arrays for regular ``batchexecute``
chat RPCs dispatched through ``RpcCaller.rpc_call`` — currently just
``SUGGEST_PROMPTS`` (``otmP3b`` / ``GeneratePromptSuggestions``).
"""

from __future__ import annotations

from typing import Any

from ..rpc import nest_source_ids

# The required ``C0`` "mode/surface" enum (field 4 of the request).
#
# What is KNOWN (live-verified on the consumer/labs cohort, issue #1612):
#   * The field is REQUIRED: ``0`` / omitted makes the server return a gRPC
#     ``INTERNAL`` error.
#   * Every value in the inclusive ``1..9`` range returns a populated
#     suggestion list; values outside it are not accepted.
#   * The returned titles vary per call (the suggestions are LLM-generated and
#     non-deterministic), so ``mode`` reads as a *surface/context* selector
#     rather than a strict, stable style — two calls with the same ``mode`` do
#     not return identical rows.
#
# What is UNKNOWN: the label↔int mapping. The web bundle's request builder
# (``JPa`` / ``EBb``…``IBb``) does not spell the enum members out as readable
# names, and no member list survives in any capture we hold (checked the
# embedded ``WIZ_global_data`` in every cassette + issues #1612/#1599). So this
# stays a plain ``int`` rather than a named enum — adding fabricated member
# names would violate the "don't invent enum values" rule, and the server does
# NOT validate the *meaning* of the code (it accepts any 1..9), so a bundle
# display-switch value is not proof of a selectable option.
#
# DEFAULT = 4: the single value exercised end-to-end in the issue's
# live-verified capture (its worked example used ``C0=4``). It is a documented,
# arbitrary-within-the-working-range pick, not a recovered "default" semantic —
# resuming the exploration needs live auth to probe what each 1..9 value steers
# (see the ``mode`` arg docstring). Until then, every value behaves
# interchangeably from the client's view (all return a 3-row suggestion list).
_PROMPT_SUGGESTIONS_DEFAULT_MODE = 4
_PROMPT_SUGGESTIONS_MODE_MIN = 1
_PROMPT_SUGGESTIONS_MODE_MAX = 9


def _client_context() -> list[Any]:
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
        _client_context(),
        notebook_id,
        nest_source_ids(source_ids, 1),
        mode,
        None,
        resolved_query,
    ]
