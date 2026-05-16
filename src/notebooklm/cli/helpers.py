"""CLI helper utilities.

Provides common functionality for all CLI commands:
- Authentication handling (get_client)
- Async execution (run_async)
- Error handling
- JSON/Rich output formatting
- Context management (current notebook/conversation)
- @with_client decorator for command boilerplate reduction
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import click
from filelock import FileLock
from rich.console import Console
from rich.table import Table

from ..auth import AuthTokens, build_cookie_jar, load_auth_from_storage
from ..exceptions import NetworkError, RPCError, RPCTimeoutError
from ..io import atomic_update_json, atomic_write_json
from ..paths import get_context_path
from ..research import select_cited_sources
from ..types import ArtifactType, CitedSourceSelection
from ._encoding import safe_echo

if TYPE_CHECKING:
    from ..types import Artifact, Source

console = Console()
# stderr_console is used for diagnostic / status output when --json mode is
# active so that stdout stays parseable JSON for automation. See ``emit_status``.
stderr_console = Console(stderr=True)
logger = logging.getLogger(__name__)


def emit_status(msg: str, *, json_output: bool, style: str | None = None) -> None:
    """Emit a status / diagnostic line.

    When ``json_output`` is True, the message is written to stderr via a
    dedicated Rich console so that stdout remains pure JSON for downstream
    automation. Otherwise the message goes through the regular stdout console.

    Args:
        msg: The message text. Rich markup is honored by both consoles.
        json_output: True when the current command is producing JSON on stdout.
        style: Optional Rich style to apply (e.g., ``"yellow"``, ``"dim"``).
    """
    target = stderr_console if json_output else console
    if style is not None:
        target.print(msg, style=style)
    else:
        target.print(msg)


# CLI artifact type name aliases
_CLI_ARTIFACT_ALIASES = {
    "flashcard": "flashcards",  # CLI uses singular, enum uses plural
}


@dataclass(frozen=True)
class ResearchImportResult:
    """Result of importing research sources from CLI commands."""

    imported: list[dict[str, str]]
    sources: list[dict]
    cited_selection: CitedSourceSelection | None = None


def cli_name_to_artifact_type(name: str) -> ArtifactType | None:
    """Convert CLI artifact type name to ArtifactType enum.

    Args:
        name: CLI artifact type name (e.g., "video", "slide-deck", "flashcard").
            Use "all" to get None (no filter).

    Returns:
        ArtifactType enum member, or None if name is "all".

    Raises:
        KeyError: If name is not a valid artifact type.
    """
    if name == "all":
        return None

    # Handle aliases
    name = _CLI_ARTIFACT_ALIASES.get(name, name)

    # Convert kebab-case to snake_case and uppercase for enum lookup
    enum_name = name.upper().replace("-", "_")
    return ArtifactType[enum_name]


# =============================================================================
# ASYNC EXECUTION
# =============================================================================


def run_async(coro):
    """Run async coroutine in sync context.

    Guards against being called from inside an already-running event loop.
    ``asyncio.run`` raises ``RuntimeError`` in that case ("asyncio.run() cannot
    be called from a running event loop"); we re-raise with a CLI-shaped
    message and explicitly close the coroutine first so the caller does not
    see a ``RuntimeWarning: coroutine '...' was never awaited``.

    Nested event loops are intentionally not supported (no ``nest_asyncio``,
    no ``loop.run_until_complete`` fallback): the CLI assumes a single
    top-level ``asyncio.run`` invariant.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        # Distinguish "loop already running" from other RuntimeErrors (e.g.,
        # programmer errors inside the coroutine that surface as RuntimeError).
        # Only the running-loop case requires us to close the coroutine — in
        # every other case ``asyncio.run`` has already driven it to completion
        # or cancellation, and calling ``close()`` would be a no-op at best
        # (and could mask a still-pending state at worst).
        if "running event loop" not in str(exc):
            raise
        coro.close()
        raise RuntimeError(
            "Cannot run sync CLI command from within an existing event loop. "
            "Use the async API (``async with NotebookLMClient(...)``) directly "
            "instead of invoking the sync CLI helper from async code."
        ) from exc


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
            parsed.fragment,
        )
    )


def _source_url_norm(source: dict) -> str | None:
    url = source.get("url")
    if not isinstance(url, str) or not url:
        return None
    return _normalize_url(url)


def _requested_urls_norm(sources: list[dict]) -> set[str]:
    return {url for source in sources if (url := _source_url_norm(source))}


def _has_no_url_entry(sources: list[dict]) -> bool:
    return any(_source_url_norm(source) is None for source in sources)


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
    delay = initial_delay
    attempt = 1
    verified_imported: list[dict[str, str]] = []
    verified_imported_ids: set[str] = set()

    requested_urls_norm = _requested_urls_norm(sources)
    # Track whether the request itself includes any non-URL entries (research
    # reports, pasted text). If it doesn't, we must NOT include concurrent
    # no-URL additions in the synthesized return — those would be unrelated
    # sources reported as "imported" by this call.
    requested_has_no_url_entry = _has_no_url_entry(sources)

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
                            console.print(
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
                        # No-URL new sources (research reports, pasted text)
                        # are included only if the request itself had no-URL
                        # entries — otherwise they're concurrent unrelated
                        # additions and don't belong in the return.
                        imported = [
                            _imported_source_entry(src)
                            for src in new_sources
                            if (src.url and _normalize_url(src.url) in requested_urls_norm)
                            or (not src.url and requested_has_no_url_entry)
                        ]
                        return _merge_imported_sources(
                            imported, verified_imported, verified_imported_ids
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
                        requested_has_no_url_entry = _has_no_url_entry(sources)
                        if not sources:
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s but "
                                "sources.list shows all requested URLs already "
                                "present; treating as success and skipping retry "
                                "to avoid duplicate inflation",
                                notebook_id,
                            )
                            if not json_output:
                                console.print(
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
                "IMPORT_RESEARCH timed out for notebook %s; retrying in %.1fs (attempt %d, %.1fs elapsed)",
                notebook_id,
                sleep_for,
                attempt + 1,
                elapsed,
            )
            if not json_output:
                console.print(
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


def _display_cited_import_selection(cited_selection: CitedSourceSelection | None) -> None:
    if cited_selection is None:
        return

    if cited_selection.used_fallback:
        console.print("[yellow]Could not resolve cited sources; importing all sources.[/yellow]")
        return

    console.print(
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
) -> ResearchImportResult:
    """Select and import research sources using shared CLI policy."""
    sources_to_import, cited_selection = _select_research_sources_for_import(
        sources, report, cited_only
    )
    if not sources_to_import:
        return ResearchImportResult([], sources_to_import, cited_selection)

    if not json_output:
        _display_cited_import_selection(cited_selection)

    retry_kwargs: dict[str, Any] = {"max_elapsed": max_elapsed}
    if json_output:
        retry_kwargs["json_output"] = True

    async def _import_selected() -> list[dict[str, str]]:
        return await import_with_retry(
            client,
            notebook_id,
            task_id,
            sources_to_import,
            **retry_kwargs,
        )

    if status_message and not json_output:
        with console.status(status_message):
            imported = await _import_selected()
    else:
        imported = await _import_selected()

    return ResearchImportResult(imported, sources_to_import, cited_selection)


# =============================================================================
# AUTHENTICATION
# =============================================================================


def get_client(ctx) -> tuple[dict, str, str]:
    """Get auth components from context.

    Args:
        ctx: Click context with optional storage_path in obj

    Returns:
        Tuple of (cookies, csrf_token, session_id)

    Raises:
        FileNotFoundError: If auth storage not found
    """
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    profile = ctx.obj.get("profile") if ctx.obj else None

    resolved_storage_path = storage_path
    if resolved_storage_path is None and not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        from ..paths import get_storage_path

        resolved_storage_path = get_storage_path(profile=profile)

    # Load from storage (which respects NOTEBOOKLM_AUTH_JSON if resolved path is None)
    cookies = load_auth_from_storage(resolved_storage_path)

    from ..auth import fetch_tokens_with_domains

    csrf, session_id = run_async(fetch_tokens_with_domains(resolved_storage_path, profile))
    return cookies, csrf, session_id


def get_auth_tokens(ctx) -> AuthTokens:
    """Get AuthTokens object from context.

    Args:
        ctx: Click context

    Returns:
        AuthTokens ready for client construction
    """
    cookies, csrf, session_id = get_client(ctx)
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    profile = ctx.obj.get("profile") if ctx.obj else None

    resolved_storage_path = storage_path
    if resolved_storage_path is None and not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        from ..paths import get_storage_path

        resolved_storage_path = get_storage_path(profile=profile)

    if os.environ.get("NOTEBOOKLM_AUTH_JSON") and storage_path is None:
        from ..auth import build_httpx_cookies_from_storage

        jar = build_httpx_cookies_from_storage(None)
    else:
        jar = build_cookie_jar(cookies=cookies, storage_path=resolved_storage_path)

    # Read persisted account routing so RPC URLs target the same Google
    # account the tokens were minted for.
    from ..auth import get_account_email_for_storage, get_authuser_for_storage

    return AuthTokens(
        cookies=cookies,
        csrf_token=csrf,
        session_id=session_id,
        storage_path=resolved_storage_path,
        cookie_jar=jar,
        authuser=get_authuser_for_storage(resolved_storage_path),
        account_email=get_account_email_for_storage(resolved_storage_path),
    )


# =============================================================================
# CONTEXT MANAGEMENT
# =============================================================================


def _current_storage_override() -> Path | None:
    """Resolve the active ``--storage`` override from the current Click context.

    Returns the explicit storage path stored in ``ctx.obj["storage_path"]`` by
    the root CLI group (see ``notebooklm_cli.cli``), canonicalized via
    ``expanduser().resolve()`` so two representations of the same file (e.g.,
    ``~/foo.json`` and ``/home/user/foo.json``) share one sibling-context
    namespace. When no Click context is active (library usage) or no override
    was passed, returns ``None``.

    ``click.get_current_context(silent=True)`` already returns ``None`` outside
    of a Click invocation, so no try/except is needed.
    """
    ctx = click.get_current_context(silent=True)
    if ctx is None or not ctx.obj:
        return None
    storage = ctx.obj.get("storage_path")
    if storage is None:
        return None
    # Defensive normalization: ``notebooklm_cli`` already wraps strings via
    # ``Path(storage)``, but accept either form so future callers (tests,
    # library wrappers populating ``ctx.obj`` directly) don't get a
    # string-vs-``Path`` mismatch crash inside ``get_context_path``.
    return Path(storage).expanduser().resolve()


def _get_context_value(key: str) -> str | None:
    """Read a single value from context.json."""
    context_file = get_context_path(storage_path=_current_storage_override())
    if not context_file.exists():
        return None
    try:
        data = json.loads(context_file.read_text(encoding="utf-8"))
        return data.get(key)
    except json.JSONDecodeError:
        logger.warning(
            "Context file %s is corrupted; cannot read '%s'. Run 'notebooklm clear' to reset.",
            context_file,
            key,
        )
        return None
    except OSError as e:
        logger.warning("Cannot read context file %s: %s", context_file, e)
        return None


def _set_context_value(key: str, value: str | None) -> None:
    """Set or clear a single value in context.json.

    Uses ``atomic_update_json`` so concurrent CLI invocations cannot lose
    updates by interleaving read-modify-write cycles.

    Note: the ``context_file.exists()`` pre-check below is a TOCTOU window —
    a concurrent ``clear`` could delete the file between this check and the
    lock acquire, but the worst case is one unnecessary lock + create cycle,
    not data loss. We accept the minor inefficiency to keep the early-return
    fast path: most invocations have no context file at all.
    """
    context_file = get_context_path(storage_path=_current_storage_override())
    if not context_file.exists():
        return

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        if value is not None:
            data[key] = value
        elif key in data:
            del data[key]
        return data

    try:
        atomic_update_json(context_file, _mutate)
    except json.JSONDecodeError:
        logger.warning(
            "Context file %s is corrupted; cannot update '%s'. Run 'notebooklm clear' to reset.",
            context_file,
            key,
        )
    except OSError as e:
        logger.warning("Failed to write context file %s for key '%s': %s", context_file, key, e)


def get_current_notebook() -> str | None:
    """Get the current notebook ID from context."""
    return _get_context_value("notebook_id")


def set_current_notebook(
    notebook_id: str,
    title: str | None = None,
    is_owner: bool | None = None,
    created_at: str | None = None,
):
    """Set the current notebook context.

    conversation_id is never preserved — the server owns the canonical ID per
    notebook, and a stale local value would silently use the wrong UUID.

    Uses ``atomic_update_json`` so the account-metadata preservation and the
    notebook-context overwrite happen as one lock-protected critical section.
    """
    context_file = get_context_path(storage_path=_current_storage_override())

    def _mutate(existing: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        # Preserve account metadata used for multi-account auth routing.
        if isinstance(existing.get("account"), dict):
            data["account"] = existing["account"]
        data["notebook_id"] = notebook_id
        if title:
            data["title"] = title
        if is_owner is not None:
            data["is_owner"] = is_owner
        if created_at:
            data["created_at"] = created_at
        return data

    # ``recover_from_corrupt=True`` keeps the empty-dict fallback **inside**
    # the file lock. An outside-the-lock unlink-and-retry would race a
    # concurrent process that wrote a valid payload between our raise and
    # our retry, causing us to delete their good write (see PR #465 review).
    atomic_update_json(context_file, _mutate, recover_from_corrupt=True)


def clear_context(*, clear_account: bool = False) -> bool:
    """Clear the current context.

    By default, only notebook/conversation fields are cleared; account
    metadata used for multi-account auth routing is preserved. ``auth logout``
    passes ``clear_account=True`` to remove the whole file.

    Holds the same sibling ``.lock`` file used by :func:`atomic_update_json`
    so concurrent writers don't lose updates against our clear-and-delete.

    Returns True if a context file was changed or removed, False if none existed.
    """
    context_file = get_context_path(storage_path=_current_storage_override())
    if not context_file.exists():
        return False
    lock_path = context_file.with_suffix(context_file.suffix + ".lock")
    context_file.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=10.0):
        # Re-check existence under the lock — another writer may have
        # removed it between the early-return check and the lock acquire.
        if not context_file.exists():
            return False
        if clear_account:
            context_file.unlink()
            return True
        try:
            data = json.loads(context_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            context_file.unlink()
            return True
        if not isinstance(data, dict):
            context_file.unlink()
            return True
        original = dict(data)
        for key in ("notebook_id", "title", "is_owner", "created_at", "conversation_id"):
            data.pop(key, None)
        if not data:
            context_file.unlink()
            return True
        if data != original:
            # atomic_update_json would re-acquire the lock; instead use the
            # atomic write directly since we already hold the lock here.
            atomic_write_json(context_file, data)
            return True
        return False


def get_current_conversation() -> str | None:
    """Get the current conversation ID from context."""
    return _get_context_value("conversation_id")


def set_current_conversation(conversation_id: str | None):
    """Set or clear the current conversation ID in context."""
    _set_context_value("conversation_id", conversation_id)


def validate_id(entity_id: str, entity_name: str = "ID") -> str:
    """Validate and normalize an entity ID.

    Args:
        entity_id: The ID to validate
        entity_name: Name for error messages (e.g., "notebook", "source")

    Returns:
        Stripped ID

    Raises:
        click.ClickException: If ID is empty or whitespace-only
    """
    if not entity_id or not entity_id.strip():
        raise click.ClickException(f"{entity_name} ID cannot be empty")
    return entity_id.strip()


def require_notebook(notebook_id: str | None) -> str:
    """Get notebook ID from argument, env var, or active context.

    Resolution order (env-var precedence):

    1. ``notebook_id`` argument (the resolved value of the ``-n/--notebook``
       Click flag — already env-var-aware via ``cli/options.py:notebook_option``,
       which declares ``envvar="NOTEBOOKLM_NOTEBOOK"``).
    2. ``NOTEBOOKLM_NOTEBOOK`` environment variable. Re-checked here so direct
       callers that don't pass through the Click flag (programmatic usage,
       legacy code paths, tests) honor the same precedence ladder.
    3. The persisted active-notebook context written by ``notebooklm use``.
    4. Hard error → ``SystemExit(1)`` with a discoverability hint listing all
       three resolution paths.

    Args:
        notebook_id: Optional notebook ID from command argument. When the
            Click flag was omitted AND the env var was unset, this is ``None``.

    Returns:
        Notebook ID (from argument, env var, or context), validated and stripped.

    Raises:
        SystemExit: If no notebook ID can be resolved from any source.
        click.ClickException: If the resolved notebook ID is empty/whitespace
            after stripping.
    """
    if notebook_id:
        return validate_id(notebook_id, "Notebook")
    # Env-var fallback runs BEFORE the active-context lookup so per-shell
    # overrides (e.g. ``NOTEBOOKLM_NOTEBOOK=other notebooklm ask "..."``)
    # compose without clobbering the persisted ``notebooklm use`` selection.
    # Empty / whitespace-only values are treated as unset (consistent with
    # ``NOTEBOOKLM_HL``'s same-shape handling) — the next fallback wins.
    env_value = os.environ.get("NOTEBOOKLM_NOTEBOOK")
    if env_value and env_value.strip():
        return validate_id(env_value, "Notebook")
    current = get_current_notebook()
    if current:
        return validate_id(current, "Notebook")
    console.print(
        "[red]No notebook specified. Use 'notebooklm use <id>' to set context, "
        "pass -n/--notebook, or set NOTEBOOKLM_NOTEBOOK.[/red]"
    )
    raise SystemExit(1)


async def _resolve_partial_id(
    partial_id: str,
    list_fn,
    entity_name: str,
    list_command: str,
    *,
    json_output: bool = False,
) -> str:
    """Generic partial ID resolver.

    Allows users to type partial IDs like 'abc' instead of full UUIDs.
    Matches are case-insensitive prefix matches.

    Args:
        partial_id: Full or partial ID to resolve
        list_fn: Async function that returns list of items with id/title attributes
        entity_name: Name for error messages (e.g., "notebook", "source")
        list_command: CLI command to list items (e.g., "list", "source list")
        json_output: When True, the "Matched..." diagnostic is routed to stderr
            via ``emit_status`` so stdout stays parseable JSON.

    Returns:
        Full ID of the matched item

    Raises:
        click.ClickException: If ID is empty, no match, or ambiguous match
    """
    # Validate and normalize the ID
    partial_id = validate_id(partial_id, entity_name)

    # Skip resolution for IDs that look complete (20+ chars)
    if len(partial_id) >= 20:
        return partial_id

    items = await list_fn()
    matches = [item for item in items if item.id.lower().startswith(partial_id.lower())]

    if len(matches) == 1:
        if matches[0].id != partial_id:
            title = matches[0].title or "(untitled)"
            emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
            )
        return matches[0].id
    elif len(matches) == 0:
        raise click.ClickException(
            f"No {entity_name} found starting with '{partial_id}'. "
            f"Run 'notebooklm {list_command}' to see available {entity_name}s."
        )
    else:
        lines = [f"Ambiguous ID '{partial_id}' matches {len(matches)} {entity_name}s:"]
        for item in matches[:5]:
            title = item.title or "(untitled)"
            lines.append(f"  {item.id[:12]}... {title}")
        if len(matches) > 5:
            lines.append(f"  ... and {len(matches) - 5} more")
        lines.append("\nSpecify more characters to narrow down.")
        raise click.ClickException("\n".join(lines))


async def resolve_notebook_id(client, partial_id: str, *, json_output: bool = False) -> str:
    """Resolve partial notebook ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notebooks.list(),
        entity_name="notebook",
        list_command="list",
        json_output=json_output,
    )


async def resolve_source_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial source ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.sources.list(notebook_id),
        entity_name="source",
        list_command="source list",
        json_output=json_output,
    )


async def resolve_artifact_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial artifact ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.artifacts.list(notebook_id),
        entity_name="artifact",
        list_command="artifact list",
        json_output=json_output,
    )


async def resolve_note_id(
    client, notebook_id: str, partial_id: str, *, json_output: bool = False
) -> str:
    """Resolve partial note ID to full ID.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    return await _resolve_partial_id(
        partial_id,
        list_fn=lambda: client.notes.list(notebook_id),
        entity_name="note",
        list_command="note list",
        json_output=json_output,
    )


async def resolve_source_ids(
    client,
    notebook_id: str,
    source_ids: tuple[str, ...],
    *,
    json_output: bool = False,
) -> list[str] | None:
    """Resolve multiple partial source IDs to full IDs.

    Args:
        client: NotebookLM client
        notebook_id: Resolved notebook ID
        source_ids: Tuple of partial source IDs from CLI
        json_output: When True, "Matched..." diagnostics for partial matches
            are routed to stderr so stdout stays parseable JSON.

    Returns:
        List of resolved source IDs, or None if no source IDs provided
    """
    if not source_ids:
        return None
    resolved = []
    for sid in source_ids:
        resolved.append(await resolve_source_id(client, notebook_id, sid, json_output=json_output))
    return resolved


def read_stdin_text(*, source_label: str = "stdin") -> str:
    """Read all of stdin as UTF-8 text and strip surrounding whitespace.

    Centralizes the Unix ``-`` (stdin) convention used by ``ask``, ``note
    create``, ``source add``, and ``--prompt-file -``. Uses
    ``click.get_text_stream("stdin").read()`` so ``CliRunner.invoke(input=...)``
    in tests is honored without monkey-patching ``sys.stdin``.

    Args:
        source_label: Label used in error messages (e.g. ``"prompt file"``)
            so the failure mode tells the user which input was empty.

    Raises:
        click.ClickException: stdin yields a non-UTF-8 byte sequence.
    """
    try:
        text = click.get_text_stream("stdin").read()
    except UnicodeDecodeError as e:
        raise click.ClickException(f"{source_label} (stdin) is not valid UTF-8: {e}") from e
    return text.strip()


def resolve_prompt(
    argument_value: str | None,
    prompt_file: str | None,
    param_name: str = "prompt",
    *,
    required: bool = False,
) -> str:
    """Resolve prompt text from a positional argument or ``--prompt-file``.

    Exactly one source may be provided. The file is read as UTF-8 with surrounding
    whitespace stripped. When ``required`` is True and neither source yields
    text, a ``UsageError`` is raised; otherwise an empty string is returned.

    The literal ``-`` is recognized as "read stdin" for either source,
    matching the Unix convention.

    Args:
        argument_value: Value of the positional CLI argument (may be empty).
        prompt_file: Path passed via ``--prompt-file`` (may be ``None``).
        param_name: Name of the positional argument, used in error messages.
        required: When True, raise ``UsageError`` if both sources are empty.

    Raises:
        click.UsageError: Both sources provided, or ``required`` and both empty.
        click.ClickException: Prompt file unreadable or not valid UTF-8.
    """
    if argument_value and prompt_file:
        raise click.UsageError(
            f"Cannot use both the {param_name} argument and --prompt-file. Choose one."
        )

    if prompt_file == "-" or argument_value == "-":
        # Unix ``-`` convention: read text from stdin. The label hints which
        # input is the empty one if the required check fires below.
        label = "prompt file" if prompt_file == "-" else param_name
        text = read_stdin_text(source_label=label)
    elif prompt_file:
        path = Path(prompt_file)
        if not path.is_file():
            raise click.ClickException(f"Prompt file '{prompt_file}' is not a regular file.")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise click.ClickException(f"Failed to read prompt file '{prompt_file}': {e}") from e
        except UnicodeDecodeError as e:
            raise click.ClickException(
                f"Prompt file '{prompt_file}' is not valid UTF-8: {e}"
            ) from e
    else:
        text = argument_value or ""

    if required and not text:
        raise click.UsageError(f"Provide a {param_name} argument or --prompt-file.")
    return text


# =============================================================================
# ERROR HANDLING
# =============================================================================


def handle_error(e: Exception):
    """Handle and display errors consistently."""
    message = f"Error: {e}"
    try:
        console.print(f"[red]{message}[/red]")
    except UnicodeEncodeError:
        safe_echo(message, err=True)
    raise SystemExit(1)


def handle_auth_error(json_output: bool = False):
    """Handle authentication errors with helpful context."""
    from ..paths import get_path_info, get_storage_path

    storage_override = _current_storage_override()
    path_info = get_path_info(storage_path=storage_override)
    storage_path = storage_override if storage_override is not None else get_storage_path()
    has_env_var = bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    storage_source = path_info["home_source"]

    if json_output:
        json_error_response(
            "AUTH_REQUIRED",
            "Auth not found. Run 'notebooklm login' first.",
            extra={
                "checked_paths": {
                    "storage_file": str(storage_path),
                    "storage_source": storage_source,
                    "env_var": "NOTEBOOKLM_AUTH_JSON" if has_env_var else None,
                },
                "help": "Run 'notebooklm login' or set NOTEBOOKLM_AUTH_JSON",
            },
        )
    else:
        console.print("[red]Not logged in.[/red]\n")
        console.print("[dim]Checked locations:[/dim]")
        console.print(f"  • Storage file: [cyan]{storage_path}[/cyan]")
        if has_home_env:
            console.print("    [dim](via $NOTEBOOKLM_HOME)[/dim]")
        env_status = "[yellow]set but invalid[/yellow]" if has_env_var else "[dim]not set[/dim]"
        console.print(f"  • NOTEBOOKLM_AUTH_JSON: {env_status}")
        console.print("\n[bold]Options to authenticate:[/bold]")
        console.print("  1. Run: [green]notebooklm login[/green]")
        console.print("  2. Set [cyan]NOTEBOOKLM_AUTH_JSON[/cyan] env var (for CI/CD)")
        console.print("  3. Use [cyan]--storage /path/to/file.json[/cyan] flag")
        raise SystemExit(1)


# =============================================================================
# DECORATORS
# =============================================================================


def with_client(f):
    """Decorator that handles auth, async execution, and errors for CLI commands.

    This decorator eliminates boilerplate from commands that need:
    - Authentication (get AuthTokens from context)
    - Async execution (run coroutine with asyncio.run)
    - Error handling (auth errors, general exceptions)

    The decorated function stays SYNC (Click doesn't support async) but returns
    a coroutine. The decorator runs the coroutine and handles errors.

    Usage:
        @cli.command("list")
        @click.option("--json", "json_output", is_flag=True)
        @with_client
        def list_notebooks(ctx, json_output, client_auth):
            async def _run():
                async with NotebookLMClient(client_auth) as client:
                    notebooks = await client.notebooks.list()
                    output_notebooks(notebooks, json_output)
            return _run()

    Args:
        f: Function that accepts client_auth (AuthTokens) and returns a coroutine

    Returns:
        Decorated function with Click pass_context
    """

    @wraps(f)
    @click.pass_context
    def wrapper(ctx, *args, **kwargs):
        from .error_handler import handle_errors

        cmd_name = f.__name__
        start = time.monotonic()
        logger.debug("CLI command starting: %s", cmd_name)

        json_output = kwargs.get("json_output", False)
        # Verbose is captured on the root group via Click ``--verbose`` count.
        # Use ``find_root`` so nested subcommand contexts still see it.
        try:
            verbose_count = int(ctx.find_root().params.get("verbose", 0) or 0)
        except Exception:
            verbose_count = 0
        verbose = verbose_count >= 1

        def log_result(status: str, detail: str = "") -> float:
            elapsed = time.monotonic() - start
            if detail:
                logger.debug("CLI command %s: %s (%.3fs) - %s", status, cmd_name, elapsed, detail)
            else:
                logger.debug("CLI command %s: %s (%.3fs)", status, cmd_name, elapsed)
            return elapsed

        with handle_errors(verbose=verbose, json_output=json_output):
            # Auth bootstrap: FileNotFoundError here means the storage file is
            # missing — it has a dedicated rich UX via ``handle_auth_error``.
            # The narrow ``except FileNotFoundError`` ensures a FileNotFoundError
            # raised *inside* the command body (e.g., a missing ``--source-file``
            # argument; see issue #153) is NOT misclassified as an auth error —
            # it propagates to ``handle_errors``' UNEXPECTED_ERROR branch instead.
            # Any OTHER exception from the auth bootstrap (malformed storage JSON,
            # AuthError during token extraction, etc.) also reaches ``handle_errors``
            # so users get typed hints rather than a raw traceback.
            try:
                auth = get_auth_tokens(ctx)
            except FileNotFoundError:
                log_result("failed", "not authenticated")
                handle_auth_error(json_output)
                return  # unreachable — handle_auth_error raises SystemExit
            except Exception as e:
                # Non-FileNotFoundError bootstrap failures (AuthError, malformed
                # storage JSON, etc.) still need the structured debug-log entry;
                # ``handle_errors`` will translate the exception to a typed hint.
                log_result("failed", str(e))
                raise
            try:
                coro = f(ctx, *args, client_auth=auth, **kwargs)
                result = run_async(coro)
            except Exception as e:
                log_result("failed", str(e))
                raise
            log_result("completed")
            return result

    return wrapper


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================


def json_output_response(data: dict | list) -> None:
    """Print JSON response (no colors for machine parsing)."""
    click.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def json_error_response(code: str, message: str, extra: dict | None = None) -> None:
    """Print JSON error and exit (no colors for machine parsing).

    Args:
        code: Error code (e.g., "AUTH_REQUIRED", "ERROR")
        message: Human-readable error message
        extra: Optional additional data to include in response
    """
    response = {"error": True, "code": code, "message": message}
    if extra:
        response.update(extra)
    click.echo(json.dumps(response, indent=2, default=str, ensure_ascii=False))
    raise SystemExit(1)


_RESULT_TYPE_LABELS = {
    1: "Web",
    2: "Drive",
    5: "Report",
    "web": "Web",
    "drive": "Drive",
    "report": "Report",
}


def display_research_sources(sources: list[dict], max_display: int = 10) -> None:
    """Display research sources in a formatted table.

    Args:
        sources: List of source dicts with 'title', 'url', and optional 'result_type' keys
        max_display: Maximum sources to show before truncating (default 10)
    """
    console.print(f"[bold]Found {len(sources)} sources[/bold]")

    if sources:
        # Only show Type column if any source has result_type
        has_types = any("result_type" in s for s in sources)

        table = Table(show_header=True, header_style="bold")
        table.add_column("Title", style="cyan")
        if has_types:
            table.add_column("Type", style="yellow")
        table.add_column("URL", style="dim")
        for src in sources[:max_display]:
            row = [src.get("title", "Untitled")[:50]]
            if has_types:
                rt: int | None = src.get("result_type")
                label = (
                    _RESULT_TYPE_LABELS.get(rt, str(rt) if rt is not None else "")
                    if rt is not None
                    else ""
                )
                row.append(label)
            row.append(src.get("url", "")[:60])
            table.add_row(*row)
        if len(sources) > max_display:
            extra_row = [f"... and {len(sources) - max_display} more"]
            if has_types:
                extra_row.append("")
            extra_row.append("")
            table.add_row(*extra_row)
        console.print(table)


def display_report(report: str, max_chars: int = 1000, json_hint: bool = True) -> None:
    """Display a research report, truncated for terminal output.

    Args:
        report: The report markdown text.
        max_chars: Maximum characters to display (default 1000).
        json_hint: Whether to suggest --json for full output in truncation message.
    """
    if not report:
        return
    console.print("\n[bold]Report:[/bold]")
    console.print(report[:max_chars], markup=False)
    if len(report) > max_chars:
        hint = " use --json for full report" if json_hint else ""
        console.print(
            f"[dim]... (truncated,{hint})[/dim]" if hint else "[dim]... (truncated)[/dim]"
        )


# =============================================================================
# TYPE DISPLAY HELPERS
# =============================================================================


def get_artifact_type_display(artifact: "Artifact") -> str:
    """Get display string for artifact type.

    Args:
        artifact: Artifact object

    Returns:
        Display string with emoji
    """
    from notebooklm import ArtifactType

    kind = artifact.kind

    # Map ArtifactType enum to display strings
    display_map = {
        ArtifactType.AUDIO: "🎧 Audio",
        ArtifactType.VIDEO: "🎬 Video",
        ArtifactType.QUIZ: "📝 Quiz",
        ArtifactType.FLASHCARDS: "🃏 Flashcards",
        ArtifactType.MIND_MAP: "🧠 Mind Map",
        ArtifactType.INFOGRAPHIC: "🖼️ Infographic",
        ArtifactType.SLIDE_DECK: "📊 Slide Deck",
        ArtifactType.DATA_TABLE: "📈 Data Table",
    }

    # Handle report subtypes specially
    if kind == ArtifactType.REPORT:
        report_displays = {
            "briefing_doc": "📋 Briefing Doc",
            "study_guide": "📚 Study Guide",
            "blog_post": "✍️ Blog Post",
            "report": "📄 Report",
        }
        return report_displays.get(artifact.report_subtype or "report", "📄 Report")

    return display_map.get(kind, f"Unknown ({kind})")


def get_source_type_display(source_type: str) -> str:
    """Get display string for source type.

    Args:
        source_type: Type string from Source.kind (SourceType str enum)

    Returns:
        Display string with emoji
    """
    # Extract value if it's a SourceType enum, otherwise use as-is
    type_str = source_type.value if hasattr(source_type, "value") else str(source_type)
    type_map = {
        # From SourceType str enum values (types.py)
        "google_docs": "📄 Google Docs",
        "google_slides": "📊 Google Slides",
        "google_spreadsheet": "📊 Google Sheets",
        "pdf": "📄 PDF",
        "pasted_text": "📝 Pasted Text",
        "docx": "📝 DOCX",
        "web_page": "🌐 Web Page",
        "markdown": "📝 Markdown",
        "youtube": "🎬 YouTube",
        "media": "🎵 Media",
        "google_drive_audio": "🎧 Drive Audio",
        "google_drive_video": "🎬 Drive Video",
        "image": "🖼️ Image",
        "csv": "📊 CSV",
        "epub": "📕 EPUB",
        "unknown": "❓ Unknown",
    }
    return type_map.get(type_str, f"❓ {type_str}")
