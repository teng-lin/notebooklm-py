"""Service layer for ``notebooklm generate`` commands (ADR-0008).

This module owns the Click-free orchestration for all 11 ``generate``
leaf commands:

* ``audio``, ``video``, ``cinematic-video``, ``slide-deck``,
  ``revise-slide``, ``quiz``, ``flashcards``, ``infographic``,
  ``data-table``, ``mind-map``, ``report``

The split mirrors the ``services/source_add.py`` / ``services/login.py``
shape established by earlier ADR-0008 extractions:

* :func:`build_generation_plan` does all Click-time validation, parameter
  coercion (e.g. report smart-custom detection, cinematic-video alias
  enforcement), enum mapping, and the cinematic-video timeout default.
  It returns a frozen :class:`GenerationPlan` dataclass. The plan-building
  half lives in ``services/generate_plans.py``; this module re-exports it
  so existing ``notebooklm.cli.services.generate`` importers are unaffected.
* :func:`execute_generation` is the async orchestration: open-client
  scope is the caller's; this function resolves notebook/source IDs,
  dispatches to the right ``client.artifacts.*`` method, runs the
  retry-with-backoff loop via the existing ``services/artifact_generation.py``
  core, and returns a typed result for command-layer rendering.

The Click handlers in ``cli/generate_cmd.py`` shrink to a thin shell:
build the raw_args dict from Click params, call
``build_generation_plan(kind, raw_args, parameter_explicit)``, then call
``execute_generation(plan, client)`` inside an ``async with
NotebookLMClient(...) as client:`` block.

This module does NOT introduce parallel abstractions to
``services/artifact_generation.py`` (that module's
``generate_with_retry`` + ``handle_generation_result`` is the retry-core
and is reused as-is).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...types import MindMapKind

# ``_INFOGRAPHIC_STYLE_MAP`` is re-exported (via redundant alias, the explicit
# re-export idiom) because ``cli/generate_cmd.py`` imports the private name
# directly from this module; ``GenerationKind`` etc. are re-exported via
# ``__all__``.
from .generate_plans import (
    _INFOGRAPHIC_STYLE_MAP as _INFOGRAPHIC_STYLE_MAP,
)
from .generate_plans import (
    GenerationKind,
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
)

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from .artifact_generation import GenerationOutcome


@dataclass(frozen=True)
class GenerationExecutionResult:
    """Typed generation executor result for command-layer rendering."""

    kind: GenerationKind
    display_name: str
    generation: GenerationOutcome | None = None
    mind_map: Any = None


# ---------------------------------------------------------------------------
# Executor
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
    retry_sink: Callable[[Any], None] | None = None,
    wait_context: Callable[[str, str], AbstractAsyncContextManager[None]] | None = None,
    wait_start_sink: Callable[[str], None] | None = None,
    mind_map_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> GenerationExecutionResult:
    """Drive a single generation request end-to-end.

    Caller responsibility: open and close the ``NotebookLMClient`` scope.
    This function resolves notebook/source IDs, dispatches to the matching
    ``client.artifacts.<method>``, runs the retry-with-backoff loop, and
    returns a typed result for the command layer to render.
    """
    from ..resolve import resolve_notebook_id, resolve_source_ids
    from .artifact_generation import generate_with_retry, handle_generation_result

    nb_id_resolved = await resolve_notebook_id(
        client, plan.notebook_id, json_output=plan.json_output
    )

    if plan.kind == "revise-slide":
        # revise-slide never resolves source IDs.
        sources: Any = None
    else:
        sources = await resolve_source_ids(
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
    "GenerationKind",
    "GenerationExecutionResult",
    "GenerationPlan",
    "GenerationPlanValidationError",
    "build_generation_plan",
    "execute_generation",
]
