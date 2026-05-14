"""Source management CLI commands.

Commands:
    list         List sources in a notebook
    add          Add a source (url, text, file, youtube)
    get          Get source details
    fulltext     Get full indexed text content of a source
    guide        Get AI-generated source summary and keywords
    stale        Check if a URL/Drive source needs refresh
    delete       Delete a source
    delete-by-title Delete a source by exact title
    rename       Rename a source
    refresh      Refresh a URL/Drive source
    add-drive    Add a Google Drive document
    add-research Search web/drive and add sources from results
"""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import source_status_to_str
from ..urls import is_youtube_url
from .helpers import (
    console,
    display_report,
    display_research_sources,
    emit_status,
    get_source_type_display,
    import_research_sources,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    resolve_prompt,
    resolve_source_id,
    validate_id,
    with_client,
)
from .options import prompt_file_option

# Titles matching this pattern indicate the source was blocked by an anti-bot
# gateway, CAPTCHA, or returned an HTTP error page instead of real content.
_GATEWAY_TITLE_PATTERN = re.compile(
    r"^\s*(access denied|403|404|forbidden|not found|502"
    r"|just a moment|attention required|security check|captcha)",
    re.IGNORECASE,
)

# Only sources with explicit "error" status are auto-cleaned. "unknown" is
# excluded on purpose: it covers status codes we don't recognize yet (future
# NotebookLM states) and missing-status responses, which must not be silently
# deleted.
_JUNK_STATUSES = frozenset({"error"})


def _validate_upload_path(content: str, follow_symlinks: bool) -> Path:
    """Validate a local-file path before uploading it as a source.

    Returns the resolved ``Path`` so the caller can forward exactly the
    artifact it validated (closing the time-of-check / time-of-use window
    that ``add_file()`` would otherwise re-open).

    Rules (default-deny):

    1. If ``follow_symlinks`` is ``False`` and **any** component of the
       path — the leaf or any parent directory — is a symlink, refuse.
       Checking parents matters: ``dir_link/secret.pdf`` would otherwise
       sneak past a leaf-only ``is_symlink()`` check.
    2. The symlink check runs **before** ``exists()`` so a broken symlink
       gets a clear "symlink" rejection instead of being treated as text
       content via the fall-through branch.
    3. After resolution the target must be a regular file (no
       directories, FIFOs, devices, sockets).

    Raises:
        click.ClickException: on any of the above failures. The message
            tells the user exactly which flag opts in.
    """
    raw = Path(content)

    # 1) Symlink gate (leaf + every parent). Catches broken symlinks too,
    # because ``is_symlink()`` is True for dangling links.
    if not follow_symlinks:
        for component in [raw, *raw.parents]:
            if component.is_symlink():
                raise click.ClickException(
                    "Path is a symlink; pass --follow-symlinks to follow it "
                    f"explicitly. Refusing to upload: {raw}"
                )

    # 2) Resolve only after the explicit --follow-symlinks opt-in (or after
    # the no-symlink check above has passed).
    file_path = raw.expanduser().resolve()

    # 3) Reject directories, pipes, devices, sockets, and non-existent
    # paths. Symlinks have already been handled above, so this guard now
    # covers the non-symlink "weird filetype" cases.
    if not file_path.is_file():
        raise click.ClickException(f"Not a regular file: {content}")

    return file_path


def _normalize_url_for_dedup(url: str) -> str:
    """Return a URL with only the fragment stripped, for dedup comparison.

    Scheme and host are lowercased per RFC 3986 (both are case-insensitive),
    so ``https://Example.com/a`` and ``https://example.com/a`` are recognised
    as the same resource. Query strings are preserved because they often
    disambiguate distinct resources (e.g. YouTube ``?v=ID``, Google Docs
    ``?id=ID``, arXiv versions), so collapsing them would falsely flag
    legitimate sources as duplicates.
    """
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


# Sort key sentinel: sources with no created_at go to the END so a real,
# dated copy is preferred as the "oldest" anchor during dedup.
_UNDATED_SORT_KEY = float("inf")


def _classify_junk_sources(sources: list) -> list[tuple[str, str, str, str]]:
    """Identify junk sources for cleanup.

    Returns a deterministically ordered list of ``(id, title, status, reason)``
    tuples for every source that should be deleted. ``reason`` is one of
    ``"error_status"``, ``"gateway_title"``, or ``"duplicate_of:<short-id>"``.
    The oldest non-junk copy of each URL is kept; later duplicates are flagged.
    """
    sorted_sources = sorted(
        sources,
        key=lambda s: s.created_at.timestamp() if s.created_at else _UNDATED_SORT_KEY,
    )

    candidates: list[tuple[str, str, str, str]] = []
    seen_urls: dict[str, str] = {}

    for s in sorted_sources:
        title = (s.title or "").strip()
        status = source_status_to_str(s.status) if s.status else "unknown"

        if status in _JUNK_STATUSES:
            candidates.append((s.id, title, status, "error_status"))
            continue

        if _GATEWAY_TITLE_PATTERN.match(title):
            candidates.append((s.id, title, status, "gateway_title"))
            continue

        url = s.url or ""
        if url:
            normalized = _normalize_url_for_dedup(url)
            kept = seen_urls.get(normalized)
            if kept is not None:
                candidates.append((s.id, title, status, f"duplicate_of:{kept[:8]}"))
                continue
            seen_urls[normalized] = s.id

    return candidates


def _print_clean_candidates(candidates: list[tuple[str, str, str, str]]) -> None:
    """Print a Rich table summarizing sources that will (or would) be deleted."""
    table = Table(title=f"{len(candidates)} source(s) flagged for cleanup")
    table.add_column("ID", style="dim", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Status")
    table.add_column("Reason")
    for sid, title, status, reason in candidates:
        display_title = title if title else "[dim](no title)[/dim]"
        table.add_row(sid[:8], display_title, status, reason)
    console.print(table)


@click.group()
def source():
    """Source management commands.

    \b
    Commands:
      list         List sources in a notebook
      add          Add a source (url, text, file, youtube)
      get          Get source details
      fulltext     Get full indexed text content
      guide        Get AI-generated source summary and keywords
      stale        Check if source needs refresh
      delete       Delete a source
      delete-by-title Delete a source by exact title
      rename       Rename a source
      refresh      Refresh a URL/Drive source

    \b
    Partial ID Support:
      SOURCE_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


def _build_id_ambiguity_error(source_id: str, matches) -> click.ClickException:
    """Build a consistent ambiguity error for source ID prefix matches."""
    lines = [f"Ambiguous ID '{source_id}' matches {len(matches)} sources:"]
    for item in matches[:5]:
        title = item.title or "(untitled)"
        lines.append(f"  {item.id[:12]}... {title}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    lines.append("Specify more characters to narrow down.")
    return click.ClickException("\n".join(lines))


def _looks_like_full_source_id(source_id: str) -> bool:
    """Return True for UUID-shaped source IDs that can skip list-based resolution."""
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            source_id,
        )
    )


async def _resolve_source_for_delete(
    client, notebook_id: str, source_id: str, *, json_output: bool = False
) -> str:
    """Resolve a source ID for delete, returning the full source ID string.

    Canonical UUIDs take a fast path and skip the live source list lookup.
    Partial IDs are resolved against the live list.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    source_id = validate_id(source_id, "source")
    if _looks_like_full_source_id(source_id):
        return source_id

    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.id.lower().startswith(source_id.lower())]

    if len(matches) == 1:
        if matches[0].id != source_id:
            title = matches[0].title or "(untitled)"
            emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
            )
        return matches[0].id

    if len(matches) > 1:
        raise _build_id_ambiguity_error(source_id, matches)

    title_matches = [item for item in sources if item.title == source_id]
    if title_matches:
        lines = [
            f"'{source_id}' matches {len(title_matches)} source title(s), not source IDs.",
            f"Use 'notebooklm source delete-by-title \"{source_id}\"' or delete by ID:",
        ]
        for item in title_matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(title_matches) > 5:
            lines.append(f"  ... and {len(title_matches) - 5} more")
        raise click.ClickException("\n".join(lines))

    raise click.ClickException(
        f"No source found starting with '{source_id}'. "
        "Run 'notebooklm source list' to see available sources."
    )


async def _resolve_source_by_exact_title(client, notebook_id: str, title: str):
    """Resolve a source by exact title for the explicit delete-by-title flow."""
    title = validate_id(title, "source title")
    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.title == title]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        lines = [f"Title '{title}' matches {len(matches)} sources. Delete by ID instead:"]
        for item in matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(matches) > 5:
            lines.append(f"  ... and {len(matches) - 5} more")
        raise click.ClickException("\n".join(lines))

    raise click.ClickException(
        f"No source found with title '{title}'. "
        "Run 'notebooklm source list' to see available sources."
    )


@source.command("list")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_list(ctx, notebook_id, json_output, client_auth):
    """List all sources in a notebook."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await client.sources.list(nb_id_resolved)
            nb = None
            if json_output:
                nb = await client.notebooks.get(nb_id_resolved)

            if json_output:
                data = {
                    "notebook_id": nb_id_resolved,
                    "notebook_title": nb.title if nb else None,
                    "sources": [
                        {
                            "index": i,
                            "id": src.id,
                            "title": src.title,
                            "type": str(src.kind),
                            "url": src.url,
                            "status": source_status_to_str(src.status),
                            "status_id": src.status,
                            "created_at": src.created_at.isoformat() if src.created_at else None,
                        }
                        for i, src in enumerate(sources, 1)
                    ],
                    "count": len(sources),
                }
                json_output_response(data)
                return

            table = Table(title=f"Sources in {nb_id_resolved}")
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Type")
            table.add_column("Created", style="dim")
            table.add_column("Status", style="yellow")

            for src in sources:
                type_display = get_source_type_display(src.kind)
                created = src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-"
                status = source_status_to_str(src.status)
                table.add_row(src.id, src.title or "-", type_display, created, status)

            console.print(table)

    return _run()


@source.command("add")
@click.argument("content")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["url", "text", "file", "youtube"]),
    default=None,
    help="Source type (auto-detected if not specified)",
)
@click.option("--title", help="Custom title for text and uploaded-file sources")
@click.option("--mime-type", help="MIME type for file sources")
@click.option(
    "--timeout",
    default=None,
    type=float,
    help=(
        "HTTP request timeout in seconds (default: 30, from the library). "
        "Increase when adding slow URLs or large files that exceed the default."
    ),
)
@click.option(
    "--follow-symlinks",
    is_flag=True,
    default=False,
    help=(
        "Follow symbolic links when uploading a file. By default, symlinks "
        "are rejected so a workspace symlink cannot silently exfiltrate the "
        "file it points at (e.g. ~/Downloads/foo.pdf -> /etc/passwd)."
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_add(
    ctx,
    content,
    notebook_id,
    source_type,
    title,
    mime_type,
    timeout,
    follow_symlinks,
    json_output,
    client_auth,
):
    """Add a source to a notebook.

    \b
    Source type is auto-detected:
      - URLs (http/https) -> url or youtube
      - Existing files (.txt, .md, etc.) -> file
      - Other content -> text (inline)
      - Use --type to override

    \b
    Examples:
      source add https://example.com              # URL
      source add ./doc.md                         # File content as text
      source add https://youtube.com/...          # YouTube video
      source add "My notes here"                  # Inline text
      source add "My notes" --title "Research"   # Text with custom title
    """
    nb_id = require_notebook(notebook_id)

    # Auto-detect source type if not specified
    detected_type = source_type
    file_content = None
    file_title = title
    # Set once the path passes ``_validate_upload_path`` so the upload uses
    # the resolved path we just validated (closes the TOCTOU window where a
    # symlink could be retargeted between validation and ``add_file()``).
    upload_path: Path | None = None

    if detected_type is None:
        if content.startswith(("http://", "https://")):
            detected_type = "youtube" if is_youtube_url(content) else "url"
        elif Path(content).exists() or Path(content).is_symlink():
            # ``is_symlink()`` short-circuits ``exists()`` for broken
            # symlinks (``exists()`` follows the link and reports False),
            # so we OR them together to make sure the symlink gate fires
            # on dangling links instead of letting them slip into the
            # text fall-through branch.
            upload_path = _validate_upload_path(content, follow_symlinks)
            detected_type = "file"
        else:
            detected_type = "text"
            file_title = title or "Pasted Text"
    elif detected_type == "file":
        # Explicit ``--type file``: the auto-detect block above is skipped,
        # so apply the same symlink + regular-file gate here. Without this,
        # ``source add link.pdf --type file`` would silently bypass the
        # ``--follow-symlinks`` opt-in and leak the symlink target.
        upload_path = _validate_upload_path(content, follow_symlinks)

    client_kwargs: dict = {}
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    async def _run():
        async with NotebookLMClient(client_auth, **client_kwargs) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            if detected_type == "url" or detected_type == "youtube":
                src = await client.sources.add_url(nb_id_resolved, content)
            elif detected_type == "text":
                text_content = file_content if file_content is not None else content
                text_title = file_title or "Untitled"
                src = await client.sources.add_text(nb_id_resolved, text_title, text_content)
            elif detected_type == "file":
                # ``upload_path`` is always populated by the validation
                # above when ``detected_type == "file"``. Pass the resolved
                # path string so ``add_file()`` opens exactly the file we
                # just validated (no second symlink hop).
                assert upload_path is not None, (
                    "upload_path must be set when detected_type == 'file'"
                )
                src = await client.sources.add_file(
                    nb_id_resolved, str(upload_path), mime_type, title=file_title
                )

            if json_output:
                data = {
                    "source": {
                        "id": src.id,
                        "title": src.title,
                        "type": str(src.kind),
                        "url": src.url,
                    }
                }
                json_output_response(data)
                return

            console.print(f"[green]Added source:[/green] {src.id}")

    if not json_output:
        with console.status(f"Adding {detected_type} source..."):
            return _run()
    return _run()


@source.command("get")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_get(ctx, source_id, notebook_id, client_auth):
    """Get source details.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id_resolved, source_id)
            src = await client.sources.get(nb_id_resolved, resolved_id)
            if src:
                console.print(f"[bold cyan]Source:[/bold cyan] {src.id}")
                console.print(f"[bold]Title:[/bold] {src.title}")
                console.print(f"[bold]Type:[/bold] {get_source_type_display(src.kind)}")
                if src.url:
                    console.print(f"[bold]URL:[/bold] {src.url}")
                if src.created_at:
                    console.print(
                        f"[bold]Created:[/bold] {src.created_at.strftime('%Y-%m-%d %H:%M')}"
                    )
            else:
                console.print("[yellow]Source not found[/yellow]")

    return _run()


@source.command("delete")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def source_delete(ctx, source_id, notebook_id, yes, client_auth):
    """Delete a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            resolved_id = await _resolve_source_for_delete(client, nb_id_resolved, source_id)

            if not yes and not click.confirm(f"Delete source {resolved_id}?"):
                return

            success = await client.sources.delete(nb_id_resolved, resolved_id)
            if success:
                console.print(f"[green]Deleted source:[/green] {resolved_id}")
            else:
                console.print("[yellow]Delete may have failed[/yellow]")

    return _run()


@source.command("delete-by-title")
@click.argument("title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def source_delete_by_title(ctx, title, notebook_id, yes, client_auth):
    """Delete a source by exact title."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            source = await _resolve_source_by_exact_title(client, nb_id_resolved, title)

            if not yes and not click.confirm(f"Delete source '{source.title}' ({source.id})?"):
                return

            success = await client.sources.delete(nb_id_resolved, source.id)
            if success:
                console.print(f"[green]Deleted source:[/green] {source.id}")
            else:
                console.print("[yellow]Delete may have failed[/yellow]")

    return _run()


@source.command("rename")
@click.argument("source_id")
@click.argument("new_title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_rename(ctx, source_id, new_title, notebook_id, client_auth):
    """Rename a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id_resolved, source_id)
            src = await client.sources.rename(nb_id_resolved, resolved_id, new_title)
            console.print(f"[green]Renamed source:[/green] {src.id}")
            console.print(f"[bold]New title:[/bold] {src.title}")

    return _run()


@source.command("refresh")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_refresh(ctx, source_id, notebook_id, client_auth):
    """Refresh a URL/Drive source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id_resolved, source_id)
            with console.status("Refreshing source..."):
                src = await client.sources.refresh(nb_id_resolved, resolved_id)

            if src and src is not True:
                console.print(f"[green]Source refreshed:[/green] {src.id}")
                console.print(f"[bold]Title:[/bold] {src.title}")
            elif src is True:
                console.print(f"[green]Source refreshed:[/green] {resolved_id}")
            else:
                console.print("[yellow]Refresh returned no result[/yellow]")

    return _run()


@source.command("add-drive")
@click.argument("file_id")
@click.argument("title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--mime-type",
    type=click.Choice(["google-doc", "google-slides", "google-sheets", "pdf"]),
    default="google-doc",
    help="Document type (default: google-doc)",
)
@with_client
def source_add_drive(ctx, file_id, title, notebook_id, mime_type, client_auth):
    """Add a Google Drive document as a source."""
    from ..types import DriveMimeType

    nb_id = require_notebook(notebook_id)
    mime_map = {
        "google-doc": DriveMimeType.GOOGLE_DOC.value,
        "google-slides": DriveMimeType.GOOGLE_SLIDES.value,
        "google-sheets": DriveMimeType.GOOGLE_SHEETS.value,
        "pdf": DriveMimeType.PDF.value,
    }
    mime = mime_map[mime_type]

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            with console.status("Adding Drive source..."):
                src = await client.sources.add_drive(nb_id_resolved, file_id, title, mime)

            console.print(f"[green]Added Drive source:[/green] {src.id}")
            console.print(f"[bold]Title:[/bold] {src.title}")

    return _run()


@source.command("add-research")
@click.argument("query", default="", required=False)
@prompt_file_option
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--from",
    "search_source",
    type=click.Choice(["web", "drive"]),
    default="web",
    help="Search source (default: web)",
)
@click.option(
    "--mode",
    type=click.Choice(["fast", "deep"]),
    default="fast",
    help="Search mode (default: fast)",
)
@click.option("--import-all", is_flag=True, help="Import all found sources")
@click.option("--cited-only", is_flag=True, help="With --import-all, import only cited sources")
@click.option(
    "--no-wait",
    is_flag=True,
    help="Start research and return immediately (use 'research status/wait' to monitor)",
)
@click.option(
    "--timeout",
    default=1800,
    type=int,
    help=(
        "Retry budget in seconds for --import-all when the IMPORT_RESEARCH RPC "
        "times out (default: 1800). Mirrors 'research wait --timeout'. "
        "Has no effect without --import-all."
    ),
)
@with_client
def source_add_research(
    ctx,
    query,
    prompt_file,
    notebook_id,
    search_source,
    mode,
    import_all,
    cited_only,
    no_wait,
    timeout,
    client_auth,
):
    """Search web or drive and add sources from results.

    \b
    Examples:
      source add-research "machine learning"              # Search web
      source add-research "project docs" --from drive     # Search Google Drive
      source add-research "AI papers" --mode deep         # Deep search
      source add-research "tutorials" --import-all        # Auto-import all results
      source add-research "topic" --import-all --cited-only
      source add-research "topic" --mode deep --no-wait   # Non-blocking deep search
      source add-research --prompt-file query.txt --mode deep   # Read query from file
    """
    query = resolve_prompt(query, prompt_file, "query", required=True)
    if cited_only and not import_all:
        raise click.UsageError("--cited-only requires --import-all")

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            console.print(f"[yellow]Starting {mode} research on {search_source}...[/yellow]")
            result = await client.research.start(nb_id_resolved, query, search_source, mode)
            if not result:
                console.print("[red]Research failed to start[/red]")
                raise SystemExit(1)

            task_id = result["task_id"]
            console.print(f"[dim]Task ID: {task_id}[/dim]")

            # Non-blocking mode: return immediately
            if no_wait:
                console.print(
                    "[green]Research started.[/green] "
                    "Use 'research status' or 'research wait' to monitor."
                )
                return

            status = None
            for _ in range(60):
                status = await client.research.poll(nb_id_resolved)
                if status.get("status") == "completed":
                    break
                elif status.get("status") == "no_research":
                    console.print("[red]Research failed to start[/red]")
                    raise SystemExit(1)
                await asyncio.sleep(5)
            else:
                status = {"status": "timeout"}

            if status.get("status") == "completed":
                sources = status.get("sources", [])
                console.print()
                display_research_sources(sources)

                display_report(status.get("report", ""), json_hint=False)

                if import_all and sources and task_id:
                    import_result = await import_research_sources(
                        client,
                        nb_id_resolved,
                        task_id,
                        sources,
                        report=status.get("report", ""),
                        cited_only=cited_only,
                        max_elapsed=timeout,
                    )
                    console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")
            else:
                console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

    return _run()


@source.command("fulltext")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--output", "-o", type=click.Path(), help="Write content to file")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["text", "markdown"]),
    default="text",
    help="Content format: text (default) or markdown",
)
@with_client
def source_fulltext(ctx, source_id, notebook_id, json_output, output, output_format, client_auth):
    """Get full content of a source.

    Retrieves the complete content from NotebookLM. Use --format markdown to get
    a rich version with headings, tables, links, and emphasis preserved.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source fulltext abc123                        # Show plaintext in terminal
      source fulltext abc123 -f markdown -o out.md  # Save markdown to file
      source fulltext abc123 --json                 # Output as JSON
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            async def _fetch():
                return await client.sources.get_fulltext(
                    nb_id_resolved, resolved_id, output_format=output_format
                )

            if json_output:
                fulltext = await _fetch()
            else:
                with console.status("Fetching fulltext content..."):
                    fulltext = await _fetch()

            if json_output:
                from dataclasses import asdict

                json_output_response(asdict(fulltext))
                return

            if output:
                Path(output).write_text(fulltext.content, encoding="utf-8")
                console.print(f"[green]Saved {fulltext.char_count} chars to {output}[/green]")
                return

            console.print(f"[bold cyan]Source:[/bold cyan] {fulltext.source_id}")
            console.print(f"[bold]Title:[/bold] {fulltext.title}")
            console.print(f"[bold]Characters:[/bold] {fulltext.char_count:,}")
            if fulltext.url:
                console.print(f"[bold]URL:[/bold] {fulltext.url}")
            console.print()
            console.print("[bold cyan]Content:[/bold cyan]")
            # markup=False so markdown links like `[text](url)` are not eaten by Rich's tag parser
            if len(fulltext.content) > 2000:
                console.print(fulltext.content[:2000], markup=False, highlight=False)
                console.print(
                    f"\n[dim]... ({fulltext.char_count - 2000:,} more chars, use -o to save full content)[/dim]"
                )
            else:
                console.print(fulltext.content, markup=False, highlight=False)

    return _run()


@source.command("guide")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_guide(ctx, source_id, notebook_id, json_output, client_auth):
    """Get AI-generated source summary and keywords.

    Shows the "Source Guide" - an AI-generated overview of what a source contains,
    including a summary with highlighted keywords and topic tags.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source guide abc123                    # Get guide for source
      source guide abc123 --json             # Output as JSON
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            async def _fetch_guide():
                return await client.sources.get_guide(nb_id_resolved, resolved_id)

            if json_output:
                guide = await _fetch_guide()
            else:
                with console.status("Generating source guide..."):
                    guide = await _fetch_guide()

            if json_output:
                data = {
                    "source_id": resolved_id,
                    "summary": guide.get("summary", ""),
                    "keywords": guide.get("keywords", []),
                }
                json_output_response(data)
                return

            summary = guide.get("summary", "").strip()
            keywords = guide.get("keywords", [])

            if not summary and not keywords:
                console.print("[yellow]No guide available for this source[/yellow]")
                return

            if summary:
                console.print("[bold cyan]Summary:[/bold cyan]")
                console.print(summary)
                console.print()

            if keywords:
                console.print("[bold cyan]Keywords:[/bold cyan]")
                console.print(", ".join(keywords))

    return _run()


@source.command("stale")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_stale(ctx, source_id, notebook_id, client_auth):
    """Check if a URL/Drive source needs refresh.

    Returns exit code 0 if stale (needs refresh), 1 if fresh.
    This enables shell scripting: if notebooklm source stale ID; then refresh; fi

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source stale abc123              # Check if stale
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            resolved_id = await resolve_source_id(client, nb_id_resolved, source_id)
            is_fresh = await client.sources.check_freshness(nb_id_resolved, resolved_id)

            if is_fresh:
                console.print("[green]✓ Source is fresh[/green]")
                raise SystemExit(1)  # Not stale
            else:
                console.print("[yellow]⚠ Source is stale[/yellow]")
                console.print("[dim]Run 'source refresh' to update[/dim]")
                raise SystemExit(0)  # Is stale

    return _run()


@source.command("wait")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--timeout",
    default=120,
    type=int,
    help="Maximum seconds to wait (default: 120)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_wait(ctx, source_id, notebook_id, timeout, json_output, client_auth):
    """Wait for a source to finish processing.

    After adding a source, it needs to be processed before it can be used
    for chat or artifact generation. This command polls until the source
    is ready or fails.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Exit codes:
      0 - Source is ready
      1 - Source not found or processing failed
      2 - Timeout reached

    \b
    Examples:
      source wait abc123                    # Wait for source to be ready
      source wait abc123 --timeout 300      # Wait up to 5 minutes
      source wait abc123 --json             # Output status as JSON

    \b
    Subagent pattern for long-running operations:
      # In main conversation, add source then spawn subagent to wait:
      notebooklm source add https://example.com
      # Subagent runs: notebooklm source wait <source_id>
    """
    from ..types import SourceNotFoundError, SourceProcessingError, SourceTimeoutError

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            if not json_output:
                console.print(f"[dim]Waiting for source {resolved_id}...[/dim]")

            try:
                source = await client.sources.wait_until_ready(
                    nb_id_resolved,
                    resolved_id,
                    timeout=float(timeout),
                )

                if json_output:
                    data = {
                        "source_id": source.id,
                        "title": source.title,
                        "status": "ready",
                        "status_code": source.status,
                    }
                    json_output_response(data)
                else:
                    console.print(f"[green]✓ Source ready:[/green] {source.id}")
                    if source.title:
                        console.print(f"[bold]Title:[/bold] {source.title}")

            except SourceNotFoundError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "not_found",
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source not found:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceProcessingError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "error",
                        "status_code": e.status,
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source processing failed:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceTimeoutError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "timeout",
                        "last_status_code": e.last_status,
                        "timeout_seconds": int(e.timeout),
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[yellow]⚠ Timeout waiting for source:[/yellow] {e.source_id}")
                    console.print(f"[dim]Last status: {e.last_status}[/dim]")
                raise SystemExit(2) from None

    return _run()


@source.command("clean")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be deleted without actually deleting"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def source_clean(ctx, notebook_id, dry_run, yes, client_auth):
    """Automatically remove duplicate, error, and access-blocked sources."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            with console.status("Fetching sources for cleanup..."):
                sources = await client.sources.list(nb_id_resolved)

            candidates = _classify_junk_sources(sources)

            if not candidates:
                console.print("[green]Notebook is already clean. No junk sources found.[/green]")
                return

            _print_clean_candidates(candidates)

            if dry_run:
                console.print(
                    f"[yellow]Dry run: would delete {len(candidates)} source(s).[/yellow]"
                )
                return

            if not yes and not click.confirm(f"Delete {len(candidates)} source(s)?"):
                return

            console.print(f"[dim]Cleaning {len(candidates)} source(s) (in chunks of 10)...[/dim]")

            delete_list = [c[0] for c in candidates]
            chunk_size = 10
            deleted = 0
            failures: list[tuple[str, str]] = []
            for i in range(0, len(delete_list), chunk_size):
                chunk = delete_list[i : i + chunk_size]
                delete_tasks = [client.sources.delete(nb_id_resolved, sid) for sid in chunk]
                results = await asyncio.gather(*delete_tasks, return_exceptions=True)
                for sid, r in zip(chunk, results, strict=True):
                    if isinstance(r, Exception):
                        failures.append((sid, str(r)))
                    else:
                        deleted += 1
                if i + chunk_size < len(delete_list):
                    await asyncio.sleep(0.5)

            if failures:
                console.print(
                    f"[yellow]Cleaned {deleted} source(s). "
                    f"{len(failures)} deletion(s) failed.[/yellow]"
                )
                for sid, err in failures[:5]:
                    console.print(f"  [red]{sid}:[/red] {err}")
                if len(failures) > 5:
                    console.print(f"  [dim]...and {len(failures) - 5} more[/dim]")
            else:
                console.print(f"[green]Successfully cleaned {deleted} source(s).[/green]")

    return _run()
