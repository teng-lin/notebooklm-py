"""Research import helpers shared by CLI commands."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from ..exceptions import NetworkError, RPCError, RPCTimeoutError
from ..research import select_cited_sources
from ..types import CitedSourceSelection
from . import rendering as rendering_helpers

if TYPE_CHECKING:
    from ..types import Source

console = rendering_helpers.console
logger = logging.getLogger(__name__)

ImportWithRetry = Callable[..., Awaitable[list[dict[str, str]]]]


@dataclass(frozen=True)
class ResearchImportResult:
    """Result of importing research sources from CLI commands."""

    imported: list[dict[str, str]]
    sources: list[dict]
    cited_selection: CitedSourceSelection | None = None


def _normalize_url(url: str) -> str:
    """Lowercase scheme + host and strip a trailing slash for comparison.

    Server-side URL storage normalizes case and trailing slashes; client-side
    requests may not. Compare via this helper to avoid false-negative misses
    when verifying that a requested URL appears post-import.
    """
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _source_url_norm(source: dict) -> str | None:
    url = source.get("url")
    if not isinstance(url, str) or not url:
        return None
    return _normalize_url(url)


def _requested_urls_norm(sources: list[dict]) -> set[str]:
    return {url for source in sources if (url := _source_url_norm(source))}


def _no_url_entry_count(sources: list[dict]) -> int:
    return sum(1 for source in sources if _source_url_norm(source) is None)


def _imported_source_entry(source: "Source") -> dict[str, str]:
    return {"id": source.id, "title": source.title or source.url or ""}


def _merge_imported_sources(
    imported: list[dict[str, str]],
    verified_imported: list[dict[str, str]],
    verified_imported_ids: set[str],
) -> list[dict[str, str]]:
    if not verified_imported:
        return imported
    return [
        *verified_imported,
        *(entry for entry in imported if entry.get("id") not in verified_imported_ids),
    ]


async def import_with_retry(
    client,
    notebook_id: str,
    task_id: str,
    sources: list[dict],
    *,
    max_elapsed: float = 1800,
    initial_delay: float = 5,
    backoff_factor: float = 2,
    max_delay: float = 60,
    json_output: bool = False,
    output_console: Any | None = None,
) -> list[dict[str, str]]:
    """Retry research import on RPC timeouts with exponential backoff.

    On RPC timeout, probes the notebook's source list to detect server-side
    imports that succeeded despite the client deadline firing. This avoids the
    duplicate-on-retry inflation that otherwise occurs when each retry re-adds
    a copy of the same sources (a single timeout cascade can otherwise inflate
    a 60-source import to 300+ sources across 5-6 retries).

    If the pre-import source snapshot is unavailable, retries still filter out
    URLs that are already visible after each timeout, but the returned list may
    undercount server-side imports because the function cannot prove those
    sources were absent before this call.

    This is intentionally CLI-only policy. Library consumers calling
    `client.research.import_sources()` directly still get one-shot behavior.
    """
    started_at = time.monotonic()
    status_console = console if output_console is None else output_console
    delay = initial_delay
    attempt = 1
    verified_imported: list[dict[str, str]] = []
    verified_imported_ids: set[str] = set()

    requested_urls_norm = _requested_urls_norm(sources)
    # Track how many non-URL entries (research reports, pasted text) the
    # request includes so concurrent no-URL additions cannot inflate the
    # synthesized return.
    requested_no_url_count = _no_url_entry_count(sources)

    # Snapshot baseline source IDs so the post-timeout probe can identify
    # truly-new sources. We anchor the verified-success condition on URLs of
    # *new* sources — not on a baseline→current URL delta — so concurrent
    # additions from another session and pre-existing URLs cannot satisfy it.
    baseline_ids: set[str] | None
    try:
        baseline = await client.sources.list(notebook_id, strict=True)
        baseline_ids = {src.id for src in baseline}
    except (NetworkError, RPCError) as snapshot_exc:
        logger.warning(
            "Pre-import sources.list snapshot failed for %s: %s; "
            "verified-success path disabled for this call",
            notebook_id,
            snapshot_exc,
        )
        baseline_ids = None

    while True:
        try:
            imported = await client.research.import_sources(notebook_id, task_id, sources)
            return _merge_imported_sources(imported, verified_imported, verified_imported_ids)
        except RPCTimeoutError:
            elapsed = time.monotonic() - started_at
            remaining = max_elapsed - elapsed

            # Verify server-side state before retrying. The IMPORT_RESEARCH RPC
            # frequently times out at the client (30s) after a successful
            # server-side write; retrying then duplicates every source.
            if requested_urls_norm:
                try:
                    current = await client.sources.list(notebook_id, strict=True)
                    new_sources = (
                        [src for src in current if src.id not in baseline_ids]
                        if baseline_ids is not None
                        else []
                    )
                    new_urls_norm = {_normalize_url(src.url) for src in new_sources if src.url}
                    current_urls_norm = {_normalize_url(src.url) for src in current if src.url}
                    # Success requires every requested URL to appear among the
                    # *new* sources. Trivial-true cases (pre-existing URLs) and
                    # concurrent unrelated additions both fail this check.
                    if baseline_ids is not None and requested_urls_norm.issubset(new_urls_norm):
                        logger.warning(
                            "IMPORT_RESEARCH timed out for notebook %s but "
                            "sources.list shows all %d requested URLs among "
                            "new sources; treating as success and skipping "
                            "retry to avoid duplicate inflation",
                            notebook_id,
                            len(requested_urls_norm),
                        )
                        if not json_output:
                            status_console.print(
                                f"[yellow]Import RPC timed out, but server-side "
                                f"verified {len(requested_urls_norm)} requested "
                                f"sources — skipping retry.[/yellow]"
                            )
                        else:
                            logger.debug(
                                "Import RPC timed out, but server-side verified "
                                "%d requested sources — skipping retry (json mode).",
                                len(requested_urls_norm),
                            )
                        # Return only new sources that match a requested URL.
                        timeout_verified: list[dict[str, str]] = []
                        remaining_no_url = requested_no_url_count
                        for src in new_sources:
                            if src.url and _normalize_url(src.url) in requested_urls_norm:
                                timeout_verified.append(_imported_source_entry(src))
                            elif not src.url and remaining_no_url > 0:
                                timeout_verified.append(_imported_source_entry(src))
                                remaining_no_url -= 1
                        return _merge_imported_sources(
                            timeout_verified, verified_imported, verified_imported_ids
                        )
                    source_norms = [(source, _source_url_norm(source)) for source in sources]
                    removed_urls_norm = {
                        url
                        for _, url in source_norms
                        if url is not None and url in current_urls_norm
                    }
                    filtered_sources = [
                        source for source, url in source_norms if url not in current_urls_norm
                    ]
                    if len(filtered_sources) != len(sources):
                        removed_count = len(sources) - len(filtered_sources)
                        for src in new_sources:
                            if (
                                src.url
                                and _normalize_url(src.url) in removed_urls_norm
                                and src.id not in verified_imported_ids
                            ):
                                verified_imported.append(_imported_source_entry(src))
                                verified_imported_ids.add(src.id)
                        sources = filtered_sources
                        requested_urls_norm = _requested_urls_norm(sources)
                        requested_no_url_count = _no_url_entry_count(sources)
                        if not sources:
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s but "
                                "sources.list shows all requested URLs already "
                                "present; treating as success and skipping retry "
                                "to avoid duplicate inflation",
                                notebook_id,
                            )
                            if not json_output:
                                status_console.print(
                                    "[yellow]Import RPC timed out, but all "
                                    "requested sources are already present — "
                                    "skipping retry.[/yellow]"
                                )
                            else:
                                logger.debug(
                                    "Import RPC timed out, but all requested "
                                    "sources are already present — skipping retry "
                                    "(json mode)."
                                )
                            return _merge_imported_sources(
                                [], verified_imported, verified_imported_ids
                            )
                        logger.warning(
                            "IMPORT_RESEARCH timed out for notebook %s after "
                            "%d requested source(s) were already present; retrying "
                            "with %d remaining source(s)",
                            notebook_id,
                            removed_count,
                            len(sources),
                        )
                except (NetworkError, RPCError) as probe_exc:
                    # CancelledError is a BaseException, not Exception, and is
                    # not in this tuple — it propagates naturally for callers
                    # that need to cancel the operation cleanly.
                    logger.warning(
                        "Failed to probe server state after timeout: %s; falling back to retry",
                        probe_exc,
                    )

            if remaining <= 0:
                raise

            # Report-only imports (no URLs to verify) can't use the success
            # check above. Cap retries at one to bound worst-case duplicate
            # inflation for report entries when timeouts persist.
            if not requested_urls_norm and attempt >= 2:
                logger.warning(
                    "IMPORT_RESEARCH timed out for notebook %s with no URLs to "
                    "verify; giving up after %d attempts to bound duplicate inflation",
                    notebook_id,
                    attempt,
                )
                raise

            sleep_for = min(delay, max_delay, remaining)
            logger.warning(
                "IMPORT_RESEARCH timed out for notebook %s; retrying in %.1fs "
                "(attempt %d, %.1fs elapsed)",
                notebook_id,
                sleep_for,
                attempt + 1,
                elapsed,
            )
            if not json_output:
                status_console.print(
                    f"[yellow]Import timed out; retrying in {sleep_for:.0f}s "
                    f"(attempt {attempt + 1})[/yellow]"
                )
            else:
                logger.debug(
                    "Import timed out; retrying in %.0fs (attempt %d) (json mode).",
                    sleep_for,
                    attempt + 1,
                )
            await asyncio.sleep(sleep_for)
            delay = min(delay * backoff_factor, max_delay)
            attempt += 1


def _select_research_sources_for_import(
    sources: list[dict], report: str, cited_only: bool
) -> tuple[list[dict], CitedSourceSelection | None]:
    if not cited_only or not sources:
        return sources, None

    cited_selection = select_cited_sources(sources, report)
    return cited_selection.sources, cited_selection


def _display_cited_import_selection(
    cited_selection: CitedSourceSelection | None,
    *,
    output_console: Any | None = None,
) -> None:
    if cited_selection is None:
        return

    status_console = console if output_console is None else output_console
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
    sources: list[dict],
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
