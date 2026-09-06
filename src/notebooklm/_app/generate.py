"""Transport-neutral artifact-generation executor.

This is the executor half of the Click-free ``generate`` core: it owns the
end-to-end :func:`execute_generation` dispatcher, the ``kind`` → API-method
map, the per-kind call-kwargs builder, and the typed
:class:`GenerationExecutionResult`. Frozen requests live in the sibling
:mod:`notebooklm._app.generation_requests`; retry/wait orchestration lives in
:mod:`notebooklm._app.generate_retry`.

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
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..types import MindMap, MindMapKind, MindMapResult
from .generate_retry import (
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_INITIAL_DELAY,
    RETRY_MAX_DELAY,
    GenerationOutcome,
    GenerationWaitStarted,
    calculate_backoff_delay,
    generate_with_retry,
    generation_outcome_from_status,
    handle_generation_result,
)
from .generation_requests import (
    UNSET,
    AudioGenerationRequest,
    CinematicVideoGenerationRequest,
    DataTableGenerationRequest,
    FlashcardsGenerationRequest,
    GenerationKind,
    GenerationRequest,
    InfographicGenerationRequest,
    MindMapGenerationRequest,
    QuizGenerationRequest,
    ReportGenerationRequest,
    ReviseSlideGenerationRequest,
    SlideDeckGenerationRequest,
    VideoGenerationRequest,
)

if TYPE_CHECKING:
    from ..client import NotebookLMClient

NotebookResolver = Callable[["NotebookLMClient", str], Awaitable[str]]
SourceResolver = Callable[["NotebookLMClient", str, tuple[str, ...]], Awaitable[list[str] | None]]


@dataclass(frozen=True)
class GenerationExecutionResult:
    """Typed generation executor result for command-layer rendering."""

    kind: GenerationKind
    generation: GenerationOutcome | None = None
    mind_map: MindMap | MindMapResult | None = None


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


def _set_optional(kwargs: dict[str, Any], name: str, value: object) -> None:
    if value is not UNSET:
        kwargs[name] = value


def _build_call_kwargs(request: GenerationRequest, *, sources: list[str] | None) -> dict[str, Any]:
    """Build call kwargs by narrowing the frozen discriminated union."""

    if isinstance(request, ReviseSlideGenerationRequest):
        return {
            "artifact_id": request.artifact_id,
            "slide_index": request.slide_index,
            "prompt": request.prompt,
        }
    base: dict[str, Any] = {"source_ids": sources}
    if isinstance(request, AudioGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
        base["audio_format"] = request.audio_format
        base["audio_length"] = request.audio_length
    elif isinstance(request, VideoGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
        _set_optional(base, "style_prompt", request.style_prompt)
        base["video_format"] = request.video_format
        base["video_style"] = request.video_style
    elif isinstance(request, CinematicVideoGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
    elif isinstance(request, SlideDeckGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
        base["slide_format"] = request.slide_format
        base["slide_length"] = request.slide_length
    elif isinstance(request, (QuizGenerationRequest, FlashcardsGenerationRequest)):
        _set_optional(base, "instructions", request.instructions)
        base["quantity"] = request.quantity
        base["difficulty"] = request.difficulty
    elif isinstance(request, InfographicGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
        base["orientation"] = request.orientation
        base["detail_level"] = request.detail_level
        base["style"] = request.style
    elif isinstance(request, DataTableGenerationRequest):
        _set_optional(base, "language", request.language)
        base["instructions"] = request.instructions
    elif isinstance(request, MindMapGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "instructions", request.instructions)
    elif isinstance(request, ReportGenerationRequest):
        _set_optional(base, "language", request.language)
        _set_optional(base, "custom_prompt", request.custom_prompt)
        _set_optional(base, "extra_instructions", request.extra_instructions)
        base["report_format"] = request.report_format
    else:  # pragma: no cover - closed union exhaustiveness
        raise AssertionError(f"unhandled generation request: {request!r}")
    return base


async def execute_generation(
    request: GenerationRequest,
    client: NotebookLMClient,
    *,
    notebook_resolver: NotebookResolver,
    source_resolver: SourceResolver,
    retry_sink: Callable[[Any], None] | None = None,
    wait_context: (
        Callable[[GenerationWaitStarted], AbstractAsyncContextManager[None]] | None
    ) = None,
    wait_start_sink: Callable[[GenerationWaitStarted], None] | None = None,
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
    nb_id_resolved = await notebook_resolver(client, request.notebook_id)

    if isinstance(request, ReviseSlideGenerationRequest):
        sources: list[str] | None = None
    else:
        source_ids = request.source_ids
        if source_ids is UNSET:
            sources = None
        elif not source_ids:
            sources = []
        else:
            sources = await source_resolver(client, nb_id_resolved, source_ids)

    method_name = _KIND_TO_METHOD[request.kind]
    api_method = getattr(client.artifacts, method_name)
    call_kwargs = _build_call_kwargs(request, sources=sources)

    async def _generate() -> Any:
        return await api_method(nb_id_resolved, **call_kwargs)

    if isinstance(request, MindMapGenerationRequest):
        if request.map_kind is MindMapKind.INTERACTIVE:
            # The interactive kind is a studio artifact (CREATE_ARTIFACT,
            # variant 4); route through the unified mind-map API, which polls
            # the async generation to completion and returns a MindMap whose
            # tree is populated (converged with the note-backed shape).
            async def _generate_mind_map() -> Any:
                return await client.mind_maps.generate(
                    nb_id_resolved,
                    kind=MindMapKind.INTERACTIVE,
                    **call_kwargs,
                )
        else:
            _generate_mind_map = _generate
        context = mind_map_context or contextlib.nullcontext
        async with context():
            result = await _generate_mind_map()
        return GenerationExecutionResult(
            kind=request.kind,
            mind_map=result,
        )

    result = await generate_with_retry(
        _generate,
        request.max_retries,
        on_retry=retry_sink,
    )
    outcome = await handle_generation_result(
        client,
        nb_id_resolved,
        result,
        request.kind,
        request.wait,
        timeout=request.timeout,
        interval=request.interval,
        wait_context=wait_context,
        wait_start_sink=wait_start_sink,
    )
    return GenerationExecutionResult(
        kind=request.kind,
        generation=outcome,
    )


__all__ = [
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "GenerationExecutionResult",
    "GenerationKind",
    "GenerationOutcome",
    "GenerationRequest",
    "GenerationWaitStarted",
    "NotebookResolver",
    "SourceResolver",
    "calculate_backoff_delay",
    "execute_generation",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
]
