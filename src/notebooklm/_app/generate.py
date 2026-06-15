"""Transport-neutral artifact-generation executor.

This is the executor half of the Click-free ``generate`` core: it owns the
end-to-end :func:`execute_generation` dispatcher, the ``kind`` → API-method
map, the per-kind call-kwargs builder, and the typed
:class:`GenerationExecutionResult`. The plan-building half lives in
:mod:`notebooklm._app.generate_plans` and the retry/wait half in
:mod:`notebooklm._app.generate_retry`; this module re-exports their public
surface so ``_app.generate`` stays the single import point each transport
adapter (the Click CLI today, the FastMCP server / future HTTP later) drives.

Two boundary seams are worth calling out:

* **The notebook-id / source-id resolvers are injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` / ``resolve_source_ids`` raise
  ``click.ClickException`` and reach into ``rich`` consoles for their
  diagnostics, so this module cannot import either without breaking the
  ``_app`` boundary. :func:`execute_generation` takes ``notebook_resolver`` /
  ``source_resolver`` callables (the CLI wrapper passes its own, read at call
  time so the historical ``monkeypatch.setattr(resolve_module, ...)`` seam
  keeps landing). Their full-id fast paths live inside the injected resolvers,
  preserving the RPC call set so the recorded cassettes still match.

* **The long-running progress seams are neutral callables.** ``retry_sink`` /
  ``wait_start_sink`` are point notifications; ``wait_context`` /
  ``mind_map_context`` span the awaited poll with an enter/exit boundary (a
  spinner in the CLI). None of their signatures carries a transport type, so
  the adapter wires its Rich-coupled implementations in and this core stays
  presentation-neutral.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..types import MindMapKind
from .generate_plans import (
    GenerationKind,
    GenerationPlan,
    GenerationPlanValidationError,
    NotebookResolver,
    SourceResolver,
    build_generation_plan,
)
from .generate_retry import (
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_INITIAL_DELAY,
    RETRY_MAX_DELAY,
    GenerationOutcome,
    calculate_backoff_delay,
    generate_with_retry,
    generation_outcome_from_status,
    handle_generation_result,
)

if TYPE_CHECKING:
    from ..client import NotebookLMClient


@dataclass(frozen=True)
class GenerationExecutionResult:
    """Typed generation executor result for command-layer rendering."""

    kind: GenerationKind
    display_name: str
    generation: GenerationOutcome | None = None
    mind_map: Any = None


# ---------------------------------------------------------------------------
# Executor.
# ---------------------------------------------------------------------------


_KIND_TO_METHOD: Mapping[str, str] = {
    "audio": "generate_audio",
    "video": "generate_video",
    "cinematic-video": "generate_cinematic_video",
    "slide-deck": "generate_slide_deck",
    "revise-slide": "revise_slide",
    "quiz": "generate_quiz",
    "flashcards": "generate_flashcards",
    "infographic": "generate_infographic",
    "data-table": "generate_data_table",
    "mind-map": "generate_mind_map",
    "report": "generate_report",
}


def _build_call_kwargs(plan: GenerationPlan, *, notebook_id: str, sources: Any) -> dict[str, Any]:
    """Build the kwargs dict passed to ``client.artifacts.<method>(notebook_id, **kwargs)``.

    Common cross-kind kwargs (``source_ids``, ``language``, ``instructions``)
    are merged with kind-specific ``plan.params``. ``revise-slide`` and
    ``mind-map`` have bespoke shapes handled here.
    """
    if plan.kind == "revise-slide":
        # revise_slide(notebook_id, *, artifact_id, slide_index, prompt)
        return {
            "artifact_id": plan.params["artifact_id"],
            "slide_index": plan.params["slide_index"],
            "prompt": plan.params["prompt"],
        }

    if plan.kind == "mind-map":
        return {
            "source_ids": sources,
            "language": plan.language,
            "instructions": plan.params.get("instructions"),
        }

    if plan.kind == "cinematic-video":
        # cinematic-video API: (notebook_id, *, source_ids, language, instructions)
        return {
            "source_ids": sources,
            "language": plan.language,
            "instructions": plan.description or None,
        }

    base: dict[str, Any] = {"source_ids": sources}

    # Language: only kinds that accept it (plan.language not None).
    if plan.language is not None:
        base["language"] = plan.language

    # data-table requires ``instructions``; pass ``description`` (not
    # ``description or None``) since the Click layer enforces ``required=True``.
    if plan.kind == "data-table":
        base["instructions"] = plan.description

    # report packs report_format, custom_prompt, extra_instructions into
    # plan.params; it does NOT carry ``instructions``.
    elif plan.kind == "report":
        base["report_format"] = plan.params["report_format"]
        base["custom_prompt"] = plan.params["custom_prompt"]
        base["extra_instructions"] = plan.params["extra_instructions"]

    else:
        # audio / video / slide-deck / quiz / flashcards / infographic all
        # take ``instructions = description or None``.
        base["instructions"] = plan.description or None

    # Merge kind-specific params LAST so they win on key conflicts (none in
    # practice, but defensive).
    base.update(
        {
            k: v
            for k, v in plan.params.items()
            if k not in ("report_format", "custom_prompt", "extra_instructions")
        }
    )
    return base


async def execute_generation(
    plan: GenerationPlan,
    client: NotebookLMClient,
    *,
    notebook_resolver: NotebookResolver,
    source_resolver: SourceResolver,
    retry_sink: Callable[[Any], None] | None = None,
    wait_context: Callable[[str, str], AbstractAsyncContextManager[None]] | None = None,
    wait_start_sink: Callable[[str], None] | None = None,
    mind_map_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> GenerationExecutionResult:
    """Drive a single generation request end-to-end.

    Caller responsibility: open and close the ``NotebookLMClient`` scope, and
    inject the notebook/source resolvers (the CLI passes its
    ``cli.resolve.resolve_notebook_id`` / ``resolve_source_ids``, whose full-id
    fast paths preserve the RPC call set). This function resolves the IDs,
    dispatches to the matching ``client.artifacts.<method>``, runs the
    retry-with-backoff loop, and returns a typed result for the command layer
    to render.
    """
    nb_id_resolved = await notebook_resolver(client, plan.notebook_id, json_output=plan.json_output)

    if plan.kind == "revise-slide":
        # revise-slide never resolves source IDs.
        sources: Any = None
    else:
        sources = await source_resolver(
            client, nb_id_resolved, plan.source_ids, json_output=plan.json_output
        )

    method_name = _KIND_TO_METHOD[plan.kind]
    api_method = getattr(client.artifacts, method_name)
    call_kwargs = _build_call_kwargs(plan, notebook_id=nb_id_resolved, sources=sources)

    async def _generate() -> Any:
        return await api_method(nb_id_resolved, **call_kwargs)

    if plan.kind == "mind-map":
        if plan.params.get("kind") == "interactive":
            # The interactive kind is a studio artifact (CREATE_ARTIFACT,
            # variant 4); route through the unified mind-map API, which polls
            # the async generation to completion and returns a MindMap whose
            # tree is populated (converged with the note-backed shape).
            async def _generate_mind_map() -> Any:
                return await client.mind_maps.generate(
                    nb_id_resolved,
                    source_ids=sources,
                    kind=MindMapKind.INTERACTIVE,
                    language=plan.language,
                    instructions=plan.params.get("instructions"),
                )
        else:
            _generate_mind_map = _generate
        if plan.json_output:
            result = await _generate_mind_map()
        else:
            context = mind_map_context or contextlib.nullcontext
            async with context():
                result = await _generate_mind_map()
        return GenerationExecutionResult(
            kind=plan.kind,
            display_name=plan.display_name,
            mind_map=result,
        )

    result = await generate_with_retry(
        _generate,
        plan.max_retries,
        plan.display_name,
        on_retry=retry_sink,
    )
    outcome = await handle_generation_result(
        client,
        nb_id_resolved,
        result,
        plan.display_name,
        plan.wait,
        timeout=plan.timeout,
        interval=plan.interval,
        wait_context=wait_context,
        wait_start_sink=wait_start_sink,
    )
    return GenerationExecutionResult(
        kind=plan.kind,
        display_name=plan.display_name,
        generation=outcome,
    )


__all__ = [
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "GenerationExecutionResult",
    "GenerationKind",
    "GenerationOutcome",
    "GenerationPlan",
    "GenerationPlanValidationError",
    "NotebookResolver",
    "SourceResolver",
    "build_generation_plan",
    "calculate_backoff_delay",
    "execute_generation",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
]
