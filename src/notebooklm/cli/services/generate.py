"""CLI adapter for ``generate`` command orchestration — thin wrapper over ``_app``.

All Click-free generation business logic — the enum/format maps, plan
validation + coercion, the per-kind builders, the retry-with-backoff loop, the
wait orchestration, and the typed :class:`GenerationExecutionResult` /
:class:`GenerationOutcome` — now lives in the transport-neutral
:mod:`notebooklm._app.generate`. This module is the CLI-side adapter that:

* re-exports the typed plan/result/error names + ``build_generation_plan`` so
  existing ``notebooklm.cli.services.generate`` importers (the command layer in
  ``cli/generate_cmd.py`` and the direct-import tests in
  ``tests/unit/test_generate_service.py``) keep resolving unchanged, and
* injects the Click/``rich``-coupled :func:`resolve_notebook_id` /
  :func:`resolve_source_ids` resolvers into the neutral
  :func:`notebooklm._app.generate.execute_generation`.

The two resolvers are imported **inside** :func:`execute_generation` from
``cli.resolve`` and read at call time, so the historical
``monkeypatch.setattr(resolve_module, "resolve_notebook_id", ...)`` test seam
keeps landing. Command-layer rendering + exit codes live in
``cli/generate_cmd.py`` per ADR-0008.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from ..._app.generate import (
    GenerationExecutionResult,
    GenerationKind,
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
)
from ..._app.generate import (
    execute_generation as _execute_generation,
)

# ``_INFOGRAPHIC_STYLE_MAP`` is re-exported (via redundant alias, the explicit
# re-export idiom) because ``cli/generate_cmd.py`` imports the private name
# directly from this module.
from .generate_plans import (
    _INFOGRAPHIC_STYLE_MAP as _INFOGRAPHIC_STYLE_MAP,
)

if TYPE_CHECKING:
    from ...client import NotebookLMClient


async def execute_generation(
    plan: GenerationPlan,
    client: NotebookLMClient,
    *,
    retry_sink: Callable[[Any], None] | None = None,
    wait_context: Callable[[str, str], AbstractAsyncContextManager[None]] | None = None,
    wait_start_sink: Callable[[str], None] | None = None,
    mind_map_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> GenerationExecutionResult:
    """Drive a single generation request end-to-end (CLI adapter).

    Thin wrapper over the neutral executor that injects the Click/``rich``-
    coupled notebook/source resolvers. ``cli.resolve`` is imported here so the
    resolvers are read at call time — preserving the
    ``monkeypatch.setattr(resolve_module, "resolve_notebook_id", ...)`` test
    seam — and their full-id fast paths keep the RPC call set stable. The four
    presentation sinks are passed straight through to the neutral core.
    """
    from .. import resolve as resolve_module

    return await _execute_generation(
        plan,
        client,
        notebook_resolver=resolve_module.resolve_notebook_id,
        source_resolver=resolve_module.resolve_source_ids,
        retry_sink=retry_sink,
        wait_context=wait_context,
        wait_start_sink=wait_start_sink,
        mind_map_context=mind_map_context,
    )


__all__ = [
    "GenerationExecutionResult",
    "GenerationKind",
    "GenerationPlan",
    "GenerationPlanValidationError",
    "build_generation_plan",
    "execute_generation",
]
