"""Service helpers for the ``source add`` command."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ...types import Source
from ...urls import is_youtube_url

if TYPE_CHECKING:
    import click

    from ...client import NotebookLMClient

SourceAddType = Literal["url", "text", "file", "youtube"]


class SourceAddValidationError(ValueError):
    """Raised when source-add inputs fail validation."""


class SourceAddFacade(Protocol):
    """Subset of ``client.sources`` needed by source-add orchestration."""

    async def add_url(self, notebook_id: str, url: str) -> Source: ...

    async def add_text(self, notebook_id: str, title: str, content: str) -> Source: ...

    async def add_file(
        self, notebook_id: str, file_path: str, *, title: str | None = None
    ) -> Source: ...


@dataclass(frozen=True)
class SourceAddPlan:
    """Prepared source-add inputs after stdin/type/path handling."""

    content: str
    detected_type: SourceAddType
    title: str | None
    upload_path: Path | None
    warnings: tuple[str, ...] = ()


_PATH_SHAPED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".txt",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".doc",
        ".docx",
        ".rtf",
        ".odt",
        ".csv",
        ".tsv",
        ".epub",
    }
)


def looks_like_path(content: str) -> bool:
    """Return True if ``content`` is path-shaped (slash OR known extension)."""
    if "/" in content or "\\" in content:
        return True
    suffix = Path(content).suffix.lower()
    return suffix in _PATH_SHAPED_EXTENSIONS


def validate_upload_path(content: str, follow_symlinks: bool) -> Path:
    """Validate a local-file path before uploading it as a source.

    Raises:
        SourceAddValidationError: if the path is a refused symlink or is
            not a regular file.
    """
    raw = Path(content)

    if not follow_symlinks:
        for component in [raw, *raw.parents]:
            if component.is_symlink():
                raise SourceAddValidationError(
                    "Path is a symlink; pass --follow-symlinks to follow it "
                    f"explicitly. Refusing to upload: {raw}"
                )

    file_path = raw.expanduser().resolve()

    if not file_path.is_file():
        raise SourceAddValidationError(f"Not a regular file: {content}")

    return file_path


def build_source_add_plan(
    *,
    content: str,
    source_type: SourceAddType | None,
    title: str | None,
    mime_type: str | None,
    follow_symlinks: bool,
    suppress_file_mime_deprecation: bool,
    validate_path: Callable[[str, bool], Path],
    looks_path_shaped: Callable[[str], bool],
) -> SourceAddPlan:
    """Detect source-add mode, validate upload paths, and collect warnings."""
    detected_type = source_type
    file_title = title
    upload_path: Path | None = None
    warnings: list[str] = []

    if detected_type is None:
        if content.startswith(("http://", "https://")):
            detected_type = "youtube" if is_youtube_url(content) else "url"
        elif Path(content).exists() or Path(content).is_symlink():
            upload_path = validate_path(content, follow_symlinks)
            detected_type = "file"
        else:
            if looks_path_shaped(content):
                warnings.append(
                    f"warning: '{content}' looks like a path but does not "
                    "exist; ingesting as inline text. Pass --type text to "
                    "suppress this warning, or check the path for typos."
                )
            detected_type = "text"
            file_title = title or "Pasted Text"
    elif detected_type == "file":
        upload_path = validate_path(content, follow_symlinks)

    if mime_type is not None and detected_type == "file" and not suppress_file_mime_deprecation:
        warnings.append(
            "--mime-type is unused for file sources; remove the flag "
            "before v0.6.0 (Drive sources retain this option)."
        )

    return SourceAddPlan(
        content=content,
        detected_type=detected_type,
        title=file_title,
        upload_path=upload_path,
        warnings=tuple(warnings),
    )


async def add_source(
    sources: SourceAddFacade,
    *,
    notebook_id: str,
    plan: SourceAddPlan,
) -> Source:
    """Add a source using a prepared source-add plan."""
    if plan.detected_type in {"url", "youtube"}:
        return await sources.add_url(notebook_id, plan.content)

    if plan.detected_type == "text":
        text_title = plan.title or "Untitled"
        return await sources.add_text(notebook_id, text_title, plan.content)

    if plan.upload_path is None:
        raise SourceAddValidationError("upload_path must be set when detected_type == 'file'")

    # Do not forward the deprecated mime_type flag: the CLI emits the user
    # warning, while add_file() would also emit a library-level warning.
    return await sources.add_file(notebook_id, str(plan.upload_path), title=plan.title)


@dataclass(frozen=True)
class SourceAddExecutionPlan:
    """Click-shaped inputs for ``execute_source_add``.

    Distinct from :class:`SourceAddPlan` (which captures the detected source
    type + warnings produced by :func:`build_source_add_plan`). This wraps
    the resolved-notebook id + the prepared add-plan so the executor has a
    single argument matching the other ``cli/services/source_*`` pairs.
    """

    notebook_id: str
    plan: SourceAddPlan
    json_output: bool


async def execute_source_add(
    client: NotebookLMClient,
    plan: SourceAddExecutionPlan,
    *,
    ctx: click.Context | None = None,
) -> None:
    """Run the ``source add`` workflow with spinner + JSON / text rendering.

    P1.T2 bug 5: ``rich.console.Console.status`` is a SYNCHRONOUS context
    manager. The pre-fix shape ``with console.status(...): return _run()``
    exited the spinner as soon as ``_run()`` returned the coroutine — BEFORE
    ``with_client`` awaited it — so the spinner was effectively invisible
    during the actual upload. The ``with`` block here lives inside the
    awaited coroutine so the spinner spans the real I/O. JSON mode still
    suppresses the spinner so stdout stays pure JSON.
    """
    # Local imports keep this service module free of CLI-only top-level deps
    # so importing it does not pull in click for non-CLI consumers.
    from ..rendering import cli_print, console, json_output_response

    spinner = (
        contextlib.nullcontext()
        if plan.json_output
        else console.status(f"Adding {plan.plan.detected_type} source...")
    )
    with spinner:
        src = await add_source(
            client.sources,
            notebook_id=plan.notebook_id,
            plan=plan.plan,
        )

    if plan.json_output:
        json_output_response(
            {
                "source": {
                    "id": src.id,
                    "title": src.title,
                    "type": str(src.kind),
                    "url": src.url,
                }
            }
        )
        return

    cli_print(f"[green]Added source:[/green] {src.id}", ctx=ctx)
