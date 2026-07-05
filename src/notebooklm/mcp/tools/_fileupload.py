"""File-transfer helpers for the source MCP tools.

Split out of :mod:`.sources` to keep that module under the ADR-0008 size budget:
this is the file-specific slice of ``source_add`` / ``source_upload_bytes`` — the
signed-URL broker (:func:`_broker_upload`), the in-channel base64 decode
(:func:`_decode_upload_b64`) + byte-spool (:func:`_add_bytes`), and the shared
plan-build/execute seam (:func:`_add_one`) they and the URL/text/batch paths reuse.

Imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..._app import source_add as add_core
from ...exceptions import ValidationError
from .._filelink import UPLOAD_TTL, FileTransferConfig

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import Source

#: Cap on ``source_upload_bytes``' base64 payload — measured on the base64 STRING
#: (what rides in the MCP message), NOT the decoded size. 10,000 chars ≈ 7.3 KiB of
#: real file (base64 inflates ~4/3). Chosen so the whole ``tools/call`` request
#: (~1.36× the file, envelope included) stays well under the ~13–16 KiB argument
#: ceiling MCP clients enforce before transit (claude-code#55923); a bigger file
#: must take the signed-URL flow (``source_add(source_type="file")`` →
#: ``upload_required``), which caps at 200 MiB.
_MAX_UPLOAD_B64_CHARS = 10_000


def _upload_too_large(n_chars: int) -> ValidationError:
    """Build the over-cap rejection, naming the signed-URL fallback to take instead."""
    return ValidationError(
        f"bytes_base64 is {n_chars} chars; the in-channel cap is {_MAX_UPLOAD_B64_CHARS} "
        "(~7 KB of file). For a larger file call source_add(source_type='file') to get "
        "an upload_required signed URL."
    )


def _decode_upload_b64(bytes_base64: str) -> bytes:
    """Validate + decode a base64 upload payload from the MCP channel.

    Fail-fast, whitespace-tolerant, then strict — in that order:

    * a fast length pre-check rejects a grossly oversized payload BEFORE the O(n)
      whitespace strip allocates. The ~10% headroom tolerates line-wrapping
      whitespace (76-col base64 adds ~1.3% newlines) without over-allocating;
    * whitespace is stripped so wrapped / MIME base64 decodes, then the CLEANED
      length is checked against the cap — so wrapping can neither smuggle bytes
      past the cap NOR get a valid near-cap payload rejected for its newlines;
    * ``validate=True`` rejects non-alphabet garbage; an empty decode is rejected.

    The cap is on the base64 STRING (what rides in the MCP message), not the decoded
    byte count — see :data:`_MAX_UPLOAD_B64_CHARS`. Raises :class:`ValidationError`.
    """
    if len(bytes_base64) > _MAX_UPLOAD_B64_CHARS + _MAX_UPLOAD_B64_CHARS // 10:
        raise _upload_too_large(len(bytes_base64))
    cleaned = "".join(bytes_base64.split())
    if len(cleaned) > _MAX_UPLOAD_B64_CHARS:
        raise _upload_too_large(len(cleaned))
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("bytes_base64 is not valid base64") from exc
    if not raw:
        raise ValidationError("bytes_base64 decoded to no bytes (empty file)")
    return raw


def _broker_upload(
    cfg: FileTransferConfig,
    notebook_id: str,
    *,
    title: str | None,
    mime_type: str | None,
    path: str | None,
) -> dict[str, Any]:
    """Mint a signed upload URL for a remote ``source_add type=file``.

    The agent-supplied ``title`` / ``mime_type`` ride in the signed token (so they
    survive the browser round-trip and cannot be tampered with). When ``title`` is
    unset, the supplied ``path``'s basename seeds the default. The signer injects
    expiry; ``expires_at`` mirrors the upload TTL for the caller.

    Returns the ``upload_required`` payload (#1801): two first-class actor paths —
    ``human_upload`` (browser/mobile) and ``agent_upload`` (raw-body POST) — plus an
    ``agent_instructions`` try-then-fallback rule, a ``mime_locked`` flag (true only
    when a mime was signed, so the request ``Content-Type`` is ignored), and
    ``expires_at_iso`` / ``expires_in_seconds`` beside the unix ``expires_at``. The
    top-level ``url`` is retained but deprecated in favor of ``human_upload.url``.
    """
    default_title = title
    if not default_title and path:
        # The agent's path may be Windows-style (``C:\\Users\\me\\report.pdf``) even
        # though this server runs on Linux, where ``os.path.basename`` won't split on
        # ``\\`` — normalize first so the default title is the real leaf.
        default_title = os.path.basename(path.replace("\\", "/")) or None
    payload: dict[str, Any] = {"nb": notebook_id}  # op stamped by upload_url
    if default_title:
        payload["title"] = default_title
    if mime_type:
        payload["mime"] = mime_type
    url = cfg.upload_url(payload)
    # Read the deadline back from the signed token so expires_at / _iso match the
    # token's ``exp`` exactly, rather than recomputing now() a hair later (drift).
    expires_at = cfg.signer.verify(url.rsplit("/", 1)[1], op="ul")["exp"]
    expires_iso = (
        datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )
    approx_minutes = UPLOAD_TTL // 60
    # The signed token's mime wins server-side; a request Content-Type is honored
    # ONLY when no mime was signed (see _fileroutes upload_route). So expose
    # Content-Type as an agent knob only in the unlocked case, and flag the locked
    # case with ``mime_locked`` instead of the old confusing header prose. Mirror the
    # exact truthiness that gates signing ``mime`` above (``if mime_type``) so the
    # flag can't claim locked while the token carried no mime (e.g. mime_type == "").
    mime_locked = bool(mime_type)
    agent_headers: dict[str, str] = {"Accept": "application/json"}
    # When unlocked, the request Content-Type is the ONLY mime signal (no
    # extension sniffing server-side), so the example must set it too — an agent
    # is as likely to copy the curl example as to read ``headers``.
    example_ct = "" if mime_locked else '-H "Content-Type: application/pdf" '
    if not mime_locked:
        agent_headers["Content-Type"] = "<mime-type of the file, e.g. application/pdf>"
    return {
        "status": "upload_required",
        "notebook_id": notebook_id,
        # DEPRECATED (kept for backward compat): use human_upload.url instead.
        "url": url,
        "expires_at": expires_at,
        "expires_at_iso": expires_iso,
        # Nominal TTL at mint time — expires_at / expires_at_iso are the authoritative deadline.
        "expires_in_seconds": UPLOAD_TTL,
        "mime_locked": mime_locked,
        # Human/browser path, first-class so an agent that cannot upload the bytes
        # itself reliably surfaces the link to the user (the mobile case).
        "human_upload": {
            "url": url,
            "instructions": (
                "Open this link in a browser on the device that has the file, then "
                "pick the file to upload. Works on mobile (photo library / Files). "
                f"Link expires in ~{approx_minutes} min."
            ),
        },
        # An agent holding the bytes skips the browser: POST them as the raw body here.
        "agent_upload": {
            "method": "POST",
            "url": f"{url}?filename=<basename>",
            "headers": agent_headers,
            "body": "the raw file bytes (not multipart/form-data)",
            "returns": '{"status": "added", "source_id": ...}',
            "example": (
                f'curl -X POST -H "Accept: application/json" {example_ct}'
                f'--data-binary @report.pdf "{url}?filename=report.pdf"'
            ),
        },
        # One authoritative rule instead of asking the agent to predict its own
        # environment: attempt the machine path, fall back to the human path.
        "agent_instructions": (
            "If you hold the file bytes, try agent_upload first (POST the raw bytes). "
            "If that fails with a network/egress error, surface human_upload.url to "
            "the user and ask them to open it in a browser and upload the file."
        ),
    }


async def _add_bytes(
    client: NotebookLMClient,
    notebook_id: str,
    raw: bytes,
    *,
    filename: str | None,
    title: str | None,
    mime_type: str | None,
) -> Source:
    """Spool decoded in-channel bytes to a private temp file, then add it as a file source.

    The neutral add path (:func:`_add_one` → ``build_source_add_plan`` +
    ``execute_source_add``) only accepts a filesystem path, so the bytes are written
    to a ``0600`` file under a ``0700`` ``mkdtemp`` dir — the same spool-then-add
    shape the ``/files/ul`` upload route uses, minus the signed-token / single-use /
    concurrency machinery that guards that PUBLIC internet-facing route (this path is
    already authenticated by the MCP session, so none of it applies). ``filename`` is
    sanitized to a safe basename (traversal / control-char / empty defenses shared
    with the upload route via ``safe_upload_name``). The temp tree is always removed —
    on success, a rejected add, or an error.
    """
    safe = add_core.safe_upload_name(filename)
    temp_dir = tempfile.mkdtemp(prefix="nblm-mcp-ulb-")
    try:
        temp_path = os.path.join(temp_dir, safe)
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
        return await _add_one(
            client,
            notebook_id,
            os.path.realpath(temp_path),
            source_type="file",
            title=title,
            mime_type=mime_type,
            allow_internal=False,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _add_one(
    client: NotebookLMClient,
    notebook_id: str,
    content: str,
    *,
    source_type: add_core.SourceAddType,
    title: str | None,
    mime_type: str | None,
    allow_internal: bool,
) -> Source:
    """Build the source-add plan + execute it, returning the created ``Source``.

    The single seam shared by single-mode and batch-mode ``source_add`` (and the
    point #1679 layers add-time failure-signaling onto). Callers do their own
    presence / host validation BEFORE reaching here — single mode via
    ``_select_content`` (which keeps the YouTube-host guard), batch mode via
    the explicit ``source_type="url"`` that forces :func:`add_core.validate_url`.
    """
    plan = add_core.build_source_add_plan(
        content=content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        follow_symlinks=False,
        validate_path=add_core.validate_upload_path,
        looks_path_shaped=add_core.looks_like_path,
        allow_internal=allow_internal,
    )
    result = await add_core.execute_source_add(
        client,
        add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan),
    )
    return result.source
