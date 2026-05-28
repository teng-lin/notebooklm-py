"""Service helpers for the ``source add`` command.

The URL guard here is CLI input validation. The lower-level Python API
continues to pass caller-supplied URLs through to NotebookLM unchanged.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

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
        self,
        notebook_id: str,
        file_path: str,
        mime_type: str | None = None,
        *,
        title: str | None = None,
    ) -> Source: ...


@dataclass(frozen=True)
class SourceAddPlan:
    """Prepared source-add inputs after stdin/type/path handling."""

    content: str
    detected_type: SourceAddType
    title: str | None
    upload_path: Path | None
    mime_type: str | None = None
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


#: Schemes accepted by ``source add`` when content is URL-shaped. Any other
#: scheme (``file://``, ``ftp://``, ``gopher://``, ...) is rejected outright
#: as an SSRF / local-file-read risk — even with ``--allow-internal``.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain"})
_LOCALHOST_SUFFIXES = (".localhost", ".localhost.localdomain")


def _canonical_host(host: str) -> str:
    """Return the hostname form used for local-host checks."""
    return host.strip().rstrip(".").lower()


def _parse_host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse literal and legacy IPv4 host spellings without resolving DNS.

    The ``inet_aton`` fallback catches legacy IPv4 forms reliably on POSIX;
    Windows may treat non-standard spellings as DNS names instead.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return None

    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _is_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified


def _is_localhost_name(host: str) -> bool:
    return host in _LOCALHOST_NAMES or host.endswith(_LOCALHOST_SUFFIXES)


def _is_url_shaped(content: str) -> bool:
    """Return True if ``content`` looks like a URL (has a scheme delimiter).

    This is a tight heuristic — we only treat content as URL-shaped when
    ``"://"`` is present. ``urllib.parse.urlsplit`` happily produces single-
    letter schemes for Windows-style paths like ``c:\\foo\\bar.pdf``, which
    we still want to flow through to the file/text detection branch.
    """
    return "://" in content


def validate_url(url: str, *, allow_internal: bool) -> None:
    """Validate a URL for SSRF / local-file-read safety.

    Replaces the previous ``startswith(("http://", "https://"))`` prefix
    check with a structural parse + a scheme allowlist + a private/loopback/
    link-local / unspecified IP rejection (with ``localhost`` and localhost
    spellings rejected by literal when the host is a DNS name).

    DNS is **never** resolved at validation time: resolving here would be
    flaky in CI and would leak the caller's interest in the URL to whatever
    resolver is configured. The DNS-name branch only matches localhost
    spellings; legacy numeric IPv4 spellings such as ``127.1`` are parsed
    locally and classified as IP literals.

    Args:
        url: The URL the user wants to add as a source.
        allow_internal: If True, bypass the internal-host rejection (private
            IPs, loopback, link-local, unspecified, and localhost spellings).
            The scheme allowlist still applies — ``file://`` / ``ftp://`` etc.
            are rejected even with ``allow_internal=True``.

    Raises:
        SourceAddValidationError: if the URL is structurally invalid, has a
            disallowed scheme, has no host, or (without ``allow_internal``)
            targets an internal host.
    """
    try:
        parsed = urlsplit(url)
    except ValueError as exc:  # pragma: no cover — urlsplit is permissive
        raise SourceAddValidationError(f"Invalid URL: {url} ({exc})") from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise SourceAddValidationError(
            f"URL scheme {scheme!r} is not allowed; only http and https URLs "
            f"are accepted as sources. Got: {url}"
        )

    # ``hostname`` strips port + IPv6 brackets, lowercases for us, and
    # returns ``None`` for ``http:///path`` style inputs with no host.
    host = parsed.hostname
    if not host:
        raise SourceAddValidationError(f"URL has no host component: {url}")

    canonical_host = _canonical_host(host)
    if not canonical_host:
        raise SourceAddValidationError(f"URL has no host component: {url}")

    if allow_internal:
        return

    ip = _parse_host_ip(canonical_host)
    if ip is None:
        # Host is a DNS name (not an IP literal). DO NOT resolve it —
        # resolving here would be flaky in CI and leaks intent. Reject
        # only localhost spellings; everything else is accepted at this
        # layer and the network stack handles connectivity later.
        if _is_localhost_name(canonical_host):
            raise SourceAddValidationError(
                f"URL targets the local host {host!r}; pass --allow-internal "
                f"to override. Got: {url}"
            ) from None
        return

    if _is_internal_ip(ip):
        raise SourceAddValidationError(
            f"URL targets an internal IP address {host}; pass --allow-internal "
            f"to override. Got: {url}"
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
    validate_path: Callable[[str, bool], Path],
    looks_path_shaped: Callable[[str], bool],
    allow_internal: bool = False,
) -> SourceAddPlan:
    """Detect source-add mode, validate upload paths + URLs, collect warnings.

    URL validation (SSRF guard): any URL-shaped content (``"://"`` present)
    is passed through :func:`validate_url`, which enforces a http/https
    scheme allowlist and rejects private / loopback / link-local IP hosts
    (plus the ``localhost`` literal). The opt-in ``allow_internal=True``
    flag bypasses the host check but still rejects non-http(s) schemes.

    Args:
        allow_internal: Forwarded to :func:`validate_url` to opt into
            internal-host URLs (e.g. ``http://127.0.0.1:8080``).
    """
    detected_type = source_type
    file_title = title
    upload_path: Path | None = None
    warnings: list[str] = []

    if detected_type is None:
        if _is_url_shaped(content):
            # Validate before deciding url vs youtube — a bad scheme or an
            # internal-IP host must raise before we even bind a type, so
            # ``--type youtube`` cannot smuggle ``file:///etc/passwd`` past
            # the gate via auto-detection.
            validate_url(content, allow_internal=allow_internal)
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
    elif detected_type in {"url", "youtube"}:
        # Explicit ``--type url`` / ``--type youtube`` must honor the same
        # gate as auto-detection: pre-fix, ``--type url file:///etc/passwd``
        # would skip the prefix check entirely.
        validate_url(content, allow_internal=allow_internal)

    return SourceAddPlan(
        content=content,
        detected_type=detected_type,
        title=file_title,
        upload_path=upload_path,
        mime_type=mime_type if detected_type == "file" else None,
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

    return await sources.add_file(
        notebook_id,
        str(plan.upload_path),
        plan.mime_type,
        title=plan.title,
    )


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
