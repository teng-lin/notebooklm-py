"""Transport-neutral ``source add`` business logic.

This is the Click-free core behind ``source add`` (imported directly by the
``cli/source_cmd.py`` / ``cli/_source_render.py`` command layer): it owns the
input detection + validation (URL SSRF guard, upload-path checks, source-type
detection) and the add workflow, returning a typed :class:`SourceAddResult`.
Every transport adapter (the Click CLI today, the FastMCP server / future
HTTP later) drives this core and renders the typed result into its own
envelope vocabulary.

The URL guard here is **CLI input validation**: the lower-level Python API
continues to pass caller-supplied URLs through to NotebookLM unchanged.

:class:`SourceAddResult` is typed-fields-only (§11): it builds no ``--json``
dict. The CLI adapter builds the ``{"source": {...}}`` envelope from the typed
result, reusing the neutral :func:`notebooklm._app.serialize.source_summary`
helper for the inner ``{"id", "title", "type", "url"}`` shape.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from ..exceptions import ValidationError
from ..options import USE_DEFAULT
from ..types import _PATH_SHAPED_FILE_EXTENSIONS, Source
from ..urls import is_youtube_url

if TYPE_CHECKING:
    from ..client import NotebookLMClient

SourceAddType = Literal["url", "text", "file", "youtube"]


@dataclass(frozen=True)
class SourceAddWarning:
    """Semantic warning emitted while classifying a source-add input."""

    code: Literal["PATH_NOT_FOUND"]
    content: str


#: Maximum length of a file's basename on the common filesystems (ext4, APFS,
#: NTFS) — measured in **bytes**, not characters. A multibyte name (emoji, CJK)
#: can blow past this while looking short by ``len()``, so truncation is
#: byte-aware.
_MAX_BASENAME_BYTES = 255

#: Control chars stripped from an upload name: C0 (``\x00-\x1f``), DEL (``\x7f``),
#: and the C1 range (``\x80-\x9f``). None are legitimate in a filename, and a NUL
#: would make ``os.open`` raise ``ValueError``.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Return ``text`` clipped to at most ``max_bytes`` UTF-8 bytes.

    Never splits a multibyte character — a trailing partial sequence left by the
    byte clip is dropped (``decode(..., "ignore")``).
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def safe_upload_name(filename: str | None) -> str:
    """Return a safe basename for a spooled upload file, preserving the extension.

    Shared by every transport that spools a caller-named upload to a private
    ``mkdtemp`` dir (the REST ``add_file`` route and the MCP ``/files/ul`` route).
    NotebookLM 400s on an extensionless upload and the source-id extraction keys
    off the real basename+extension, so the caller's name must survive — but
    safely:

    * control chars are stripped — C0 (``\\x00-\\x1f``), DEL (``\\x7f``), and the
      C1 range (``\\x80-\\x9f``); a NUL would make ``os.open`` raise
      ``ValueError`` and none are legitimate in a filename;
    * ``\\`` is normalized so a Windows-style ``C:\\dir\\x.pdf`` yields its real
      leaf, then :func:`os.path.basename` strips directory components (the
      path-traversal guard);
    * the directory-cursor names ``.`` / ``..`` fall back to a safe extensioned
      default (they would target an existing dir and fail ``O_EXCL``);
    * an over-long name is truncated on its **stem** (never the extension), by
      UTF-8 **byte** length so a multibyte name (emoji, CJK) stays under the
      255-byte filesystem basename limit rather than only under 255 *characters*.

    An empty / cursor / extensionless-default input falls back to ``"upload.bin"``
    (never extensionless).
    """
    cleaned = _CONTROL_CHARS.sub("", filename or "").replace("\\", "/")
    base = os.path.basename(cleaned)
    if not base or base in (".", ".."):
        return "upload.bin"
    if len(base.encode("utf-8")) <= _MAX_BASENAME_BYTES:
        return base
    # Preserve the extension: clip it first (in the pathological all-suffix case),
    # then give the stem whatever byte budget remains.
    suffix = _truncate_utf8(Path(base).suffix, _MAX_BASENAME_BYTES)
    stem_budget = _MAX_BASENAME_BYTES - len(suffix.encode("utf-8"))
    return _truncate_utf8(Path(base).stem, stem_budget) + suffix


SourceAddValidationReason = Literal[
    "invalid_url",
    "unsupported_url_scheme",
    "url_missing_host",
    "local_host_disallowed",
    "internal_ip_disallowed",
    "symlink_disallowed",
    "not_regular_file",
    "missing_upload_path",
    "upload_root_not_configured",
    "path_outside_allowed_root",
    "credential_path_disallowed",
]


class SourceAddValidationError(ValidationError):
    """Typed source-add validation failure with adapter-neutral parameters.

    Subclasses :class:`~notebooklm.exceptions.ValidationError` (was ``ValueError``)
    so ``_app.errors.classify`` covers it uniformly across adapters — it
    classifies as :attr:`~notebooklm._app.errors.ErrorCategory.VALIDATION`. The
    Adapters map :attr:`reason` and the accompanying parameters to their own
    vocabulary. The exception text intentionally contains no CLI flags.
    """

    def __init__(
        self,
        reason: SourceAddValidationReason,
        *,
        url: str | None = None,
        scheme: str | None = None,
        host: str | None = None,
        path: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        self.url = url
        self.scheme = scheme
        self.host = host
        self.path = path
        self.detail = detail
        subject = url or path or "source input"
        super().__init__(f"{reason.replace('_', ' ')}: {subject}")


class SourceAddFacade(Protocol):
    """Subset of ``client.sources`` needed by source-add orchestration."""

    async def add_url(self, notebook_id: str, url: str, *, title: str | None = None) -> Source: ...

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
    warnings: tuple[SourceAddWarning, ...] = ()


#: Extensions that make an argument *look* path-shaped. Not declared here: this is
#: the derived set owned by ``notebooklm._types.sources`` alongside the source
#: type-code map, so a newly supported file type gains its spelling with its decode
#: entry instead of drifting behind it (#2202). Routed through the public ``types``
#: facade because the ``_app`` boundary lint forbids importing the private
#: ``_types`` sibling directly.
_PATH_SHAPED_EXTENSIONS = _PATH_SHAPED_FILE_EXTENSIONS


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
        raise SourceAddValidationError("invalid_url", url=url, detail=str(exc)) from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise SourceAddValidationError("unsupported_url_scheme", url=url, scheme=scheme)

    # ``hostname`` strips port + IPv6 brackets, lowercases for us, and
    # returns ``None`` for ``http:///path`` style inputs with no host.
    host = parsed.hostname
    if not host:
        raise SourceAddValidationError("url_missing_host", url=url)

    canonical_host = _canonical_host(host)
    if not canonical_host:
        raise SourceAddValidationError("url_missing_host", url=url)

    if allow_internal:
        return

    ip = _parse_host_ip(canonical_host)
    if ip is None:
        # Host is a DNS name (not an IP literal). DO NOT resolve it —
        # resolving here would be flaky in CI and leaks intent. Reject
        # only localhost spellings; everything else is accepted at this
        # layer and the network stack handles connectivity later.
        if _is_localhost_name(canonical_host):
            raise SourceAddValidationError("local_host_disallowed", url=url, host=host) from None
        return

    if _is_internal_ip(ip):
        raise SourceAddValidationError("internal_ip_disallowed", url=url, host=host)


def looks_like_path(content: str) -> bool:
    """Return True if ``content`` is path-shaped (slash OR known extension).

    Note what this does NOT decide: :func:`build_source_add_plan` tests
    ``Path(content).exists()`` first, so an argument naming a real file is
    uploaded whatever its extension. This predicate runs only on the
    does-not-exist branch, where it chooses between warning the user that their
    file-shaped argument is about to be ingested as inline text and doing that
    silently.
    """
    if "/" in content or "\\" in content:
        return True
    suffix = Path(content).suffix.lower()
    return suffix in _PATH_SHAPED_EXTENSIONS


#: Basenames that are account-equivalent NotebookLM credentials. Matched
#: case-insensitively so a dummy ``storage_state.json`` anywhere the process
#: can read is refused even inside an allowed upload root.
_CREDENTIAL_FILENAMES = frozenset({"storage_state.json", "master_token.json"})
#: Playwright Chromium profile directory names. The conventional profile layout
#: uses ``browser_profile/``; an explicit storage path may instead use a
#: ``*.browser_profile`` sibling. Either is account-equivalent.
_PLAYWRIGHT_DIR_NAME = "browser_profile"
_PLAYWRIGHT_DIR_SUFFIX = ".browser_profile"


def parse_upload_allowed_roots(raw: str | None) -> tuple[Path, ...]:
    """Resolve operator-configured upload directories, excluding broad roots.

    Root configuration is trusted input; target paths are checked separately
    before probing their metadata. Filesystem identity catches differently
    cased home aliases on case-insensitive POSIX filesystems.
    """
    if raw is None or not raw.strip():
        return ()
    forbidden = _forbidden_upload_roots()
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        if not part.strip():
            continue
        try:
            resolved = Path(part.strip()).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.parent == resolved:
            continue
        if any(_same_upload_root(resolved, candidate) for candidate in (*forbidden, *roots)):
            continue
        roots.append(resolved)
    return tuple(roots)


def _forbidden_upload_roots() -> tuple[Path, ...]:
    """Find credential homes even when a service UID has no OS home entry."""
    candidates: list[Path] = []
    try:
        home = Path.home()
        candidates.extend((home, home / ".notebooklm"))
    except (OSError, RuntimeError):
        pass
    try:
        from ..paths import get_home_dir

        candidates.append(get_home_dir())
    except (OSError, RuntimeError):
        pass
    return tuple(candidates)


def _same_upload_root(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return left.samefile(right)
    except (OSError, ValueError):
        return False


def _is_credential_upload_path(path: Path) -> bool:
    """Reject credential basenames, including Win32 stream/dot/space aliases."""
    parts = tuple(part.split(":", 1)[0].rstrip(" .").casefold() for part in path.parts)
    if parts and parts[-1] in _CREDENTIAL_FILENAMES:
        return True
    return any(
        part == _PLAYWRIGHT_DIR_NAME or part.endswith(_PLAYWRIGHT_DIR_SUFFIX) for part in parts
    )


def _is_under_allowed_root(path: Path, root: Path) -> bool:
    """Return True if ``path`` is ``root`` or a descendant, including case-fold."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    path_s = os.path.normcase(str(path))
    root_s = os.path.normcase(str(root))
    if path_s == root_s:
        return True
    return path_s.startswith(root_s.rstrip(os.sep) + os.sep)


def validate_upload_path(
    content: str,
    follow_symlinks: bool,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> Path:
    """Validate a local-file path before uploading it as a source.

    Always refuses known credential filenames (``storage_state.json``,
    ``master_token.json``) and Playwright profile directories. When
    ``allowed_roots`` is passed (including an empty sequence), the resolved
    path must also sit inside one of those roots; an empty sequence is
    default-deny. ``allowed_roots=None`` skips the root check (CLI and
    trusted temp spools).

    Raises:
        SourceAddValidationError: if the path is a refused symlink, is not a
            regular file, is a credential path, or falls outside ``allowed_roots``.
    """
    # Refuse unconfigured access before expanding or probing any target path.
    resolved_roots: list[Path] = []
    if allowed_roots is not None:
        for root in allowed_roots:
            try:
                resolved_roots.append(Path(root).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        if not resolved_roots:
            raise SourceAddValidationError("upload_root_not_configured")

    # abspath normalizes dot segments without following links or stat-ing the
    # target. Out-of-root targets always fail with the same boundary error.
    raw = Path(os.path.abspath(os.path.expanduser(content)))
    if allowed_roots is not None and not any(
        _is_under_allowed_root(raw, root) for root in resolved_roots
    ):
        raise SourceAddValidationError("path_outside_allowed_root")
    if _is_credential_upload_path(raw):
        raise SourceAddValidationError("credential_path_disallowed", path=str(raw))
    if not follow_symlinks:
        for component in [raw, *raw.parents]:
            if component.is_symlink():
                raise SourceAddValidationError("symlink_disallowed", path=str(raw))

    file_path = raw.resolve()
    if allowed_roots is not None and not any(
        _is_under_allowed_root(file_path, root) for root in resolved_roots
    ):
        raise SourceAddValidationError("path_outside_allowed_root")
    # On Windows resolve() expands short (8.3) spellings to the long filename.
    if _is_credential_upload_path(file_path):
        raise SourceAddValidationError("credential_path_disallowed", path=str(file_path))
    if not file_path.is_file():
        raise SourceAddValidationError("not_regular_file", path=content)

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
    warnings: list[SourceAddWarning] = []

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
                warnings.append(SourceAddWarning("PATH_NOT_FOUND", content))
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
        # YouTube / web-page imports re-derive the display title server-side, so
        # ``add_url`` honors an explicit ``title`` via a best-effort post-add
        # rename (#1960). Only forward a title that was actually given — a
        # title-less add keeps the historical call shape (no ``title`` kwarg).
        if plan.title:
            return await sources.add_url(notebook_id, plan.content, title=plan.title)
        return await sources.add_url(notebook_id, plan.content)

    if plan.detected_type == "text":
        text_title = plan.title or "Untitled"
        return await sources.add_text(notebook_id, text_title, plan.content)

    if plan.upload_path is None:
        raise SourceAddValidationError("missing_upload_path")

    return await sources.add_file(
        notebook_id,
        str(plan.upload_path),
        plan.mime_type,
        title=plan.title,
    )


@dataclass(frozen=True)
class SourceAddExecutionPlan:
    """Prepared inputs for ``execute_source_add``.

    Distinct from :class:`SourceAddPlan` (which captures the detected source
    type + warnings produced by :func:`build_source_add_plan`). This wraps
    the resolved-notebook id + the prepared add-plan so the executor has a
    single argument matching the other ``cli/services/source_*`` pairs.
    """

    notebook_id: str
    plan: SourceAddPlan


@dataclass(frozen=True)
class SourceAddResult:
    """Result of adding a source.

    Typed-fields-only (§11): the ``source add`` ``--json`` envelope (which wraps
    the neutral source summary under a ``"source"`` key) is built by the CLI
    adapter from :attr:`source`, not on this dataclass. Adapters that want the
    neutral summary import :func:`notebooklm._app.serialize.source_summary`.
    """

    source: Source


async def execute_source_add(
    client: NotebookLMClient,
    plan: SourceAddExecutionPlan,
) -> SourceAddResult:
    """Run the ``source add`` workflow and return the added source.

    Presentation concerns such as spinners, JSON envelopes, and success
    messages belong to the command layer. The command wraps this awaitable
    with the desired status context so the spinner still spans the real I/O.
    """
    async with client.operation(timeout=USE_DEFAULT):
        src = await add_source(
            client.sources,
            notebook_id=plan.notebook_id,
            plan=plan.plan,
        )
        return SourceAddResult(source=src)


__all__ = [
    "SourceAddExecutionPlan",
    "SourceAddFacade",
    "SourceAddPlan",
    "SourceAddResult",
    "SourceAddType",
    "SourceAddValidationReason",
    "SourceAddWarning",
    "SourceAddValidationError",
    "add_source",
    "build_source_add_plan",
    "execute_source_add",
    "looks_like_path",
    "parse_upload_allowed_roots",
    "safe_upload_name",
    "validate_upload_path",
    "validate_url",
]
