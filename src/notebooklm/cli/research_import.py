"""Research import helpers shared by CLI commands.

The one-send import and read-only inspection logic lives on
``ResearchAPI.import_sources_with_verification`` so Python API consumers get
the same safety contract. This module keeps the
CLI-specific selection/display helpers and a thin :func:`import_with_retry`
shim that delegates to the library method.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ..research import select_cited_sources
from ..types import CitedSourceSelection
from . import rendering as rendering_helpers

console = rendering_helpers.console
logger = logging.getLogger(__name__)

ImportWithRetry = Callable[..., Awaitable[list[dict[str, str]]]]


@dataclass(frozen=True)
class ResearchImportResult:
    """Result of importing research sources from CLI commands."""

    imported: list[dict[str, str]]
    sources: list[dict[str, Any]]
    cited_selection: CitedSourceSelection | None = None


async def import_with_retry(
    client,
    notebook_id: str,
    task_id: str,
    sources: list[dict[str, Any]],
    *,
    max_elapsed: float = 1800,
    initial_delay: float = 5,
    backoff_factor: float = 2,
    max_delay: float = 60,
    json_output: bool = False,  # noqa: ARG001 — retained for back-compat with CLI test mocks
    output_console: Any | None = None,  # noqa: ARG001 — retained for back-compat
) -> list[dict[str, str]]:
    """CLI delegate to :meth:`ResearchAPI.import_sources_with_verification`.

    The one-send import and bounded read-only inspection logic lives at the
    library layer so Python API users get the same safety contract as the CLI. This
    wrapper exists to preserve the CLI signature that the existing
    ``import_research_sources`` helper and unit-test mocks expect.

    ``json_output`` and ``output_console`` are accepted for back-compat but
    no longer affect behavior — inspection diagnostics are emitted via
    ``logger.warning`` on the ``notebooklm._research`` logger. Configure
    that logger to surface them to the user as needed.
    """
    return await client.research.import_sources_with_verification(
        notebook_id,
        task_id,
        sources,
        max_elapsed=max_elapsed,
        initial_delay=initial_delay,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )


def _select_research_sources_for_import(
    sources: list[dict[str, Any]], report: str, cited_only: bool
) -> tuple[list[dict[str, Any]], CitedSourceSelection | None]:
    if not cited_only or not sources:
        return sources, None

    cited_selection = select_cited_sources(sources, report)
    # CLI research import serializes typed task sources to dicts before
    # selection, so this re-narrowing is safe at the CLI boundary.
    return cast(list[dict[str, Any]], cited_selection.sources), cited_selection


def _display_cited_import_selection(
    cited_selection: CitedSourceSelection | None,
    *,
    output_console: Any | None = None,
    selected_count: int | None = None,
) -> None:
    """Report what cited-only selection chose, honestly under a later cap.

    ``selected_count`` is the number of rows actually being imported, for callers
    that narrow the selection further after this ran (``research import
    --max-sources``). Without it the natural counts are already final
    (``research wait --import-all`` has no cap), so it stays optional and that
    caller's output is unchanged.

    When the cap bit, BOTH numbers are row counts of ``cited_selection.sources``
    — deliberately not ``matched_url_source_count``, which counts only cited URL
    rows and so excludes a preserved deep-research report row. Mixing the two
    produced arithmetic that could not be read literally: three cited URLs plus a
    report under ``--max-sources 2`` would have claimed "2 of 3 cited source(s)"
    while importing the report and one URL. The capped wording therefore says
    "selected source(s)", which is what the count actually measures.
    """
    if cited_selection is None:
        return

    status_console = console if output_console is None else output_console
    selected_rows = len(cited_selection.sources)
    capped = selected_count is not None and selected_count < selected_rows

    if capped:
        tally = f"{selected_count} of {selected_rows} selected source(s)"
        if cited_selection.used_fallback:
            status_console.print(
                f"[yellow]Could not resolve cited sources; importing {tally}.[/yellow]"
            )
        else:
            status_console.print(f"[dim]Importing {tally}[/dim]")
        return

    if cited_selection.used_fallback:
        status_console.print(
            "[yellow]Could not resolve cited sources; importing all sources.[/yellow]"
        )
        return

    status_console.print(
        f"[dim]Importing {cited_selection.matched_url_source_count} cited source(s)[/dim]"
    )


async def import_research_sources(
    client,
    notebook_id: str,
    task_id: str,
    sources: list[dict[str, Any]],
    *,
    report: str = "",
    cited_only: bool = False,
    max_elapsed: float = 1800,
    json_output: bool = False,
    status_message: str | None = None,
    import_func: ImportWithRetry | None = None,
    output_console: Any | None = None,
) -> ResearchImportResult:
    """Select and import research sources using shared CLI policy."""
    status_console = console if output_console is None else output_console
    sources_to_import, cited_selection = _select_research_sources_for_import(
        sources, report, cited_only
    )
    if not sources_to_import:
        return ResearchImportResult([], sources_to_import, cited_selection)

    if not json_output:
        _display_cited_import_selection(cited_selection, output_console=status_console)

    retry_kwargs: dict[str, Any] = {"max_elapsed": max_elapsed}
    if json_output:
        retry_kwargs["json_output"] = True

    async def _import_selected() -> list[dict[str, str]]:
        if import_func is not None:
            return await import_func(
                client,
                notebook_id,
                task_id,
                sources_to_import,
                **retry_kwargs,
            )
        if output_console is not None:
            retry_kwargs["output_console"] = status_console
        return await import_with_retry(
            client,
            notebook_id,
            task_id,
            sources_to_import,
            **retry_kwargs,
        )

    if status_message and not json_output:
        with status_console.status(status_message):
            imported = await _import_selected()
    else:
        imported = await _import_selected()

    return ResearchImportResult(imported, sources_to_import, cited_selection)
