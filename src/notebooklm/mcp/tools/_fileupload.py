"""File-transfer helpers for the source MCP tools.

Split out of :mod:`.sources` to keep that module under the ADR-0008 size budget:
this is the file-specific slice of ``source_add`` (incl. its ``bytes_base64`` mode) — the
signed-URL broker (:func:`_broker_upload`), the in-channel base64 decode
(:func:`_decode_upload_b64`) + byte-spool (:func:`_add_bytes`), and the shared
plan-build/execute seam (:func:`_add_one`) they and the URL/text/batch paths reuse.

Imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastmcp import Context

from ..._app import source_add as add_core
from ...exceptions import ValidationError
from .._confirm import READ_ONLY
from .._context import get_file_transfer
from .._errors import mcp_errors
from .._filelink import UPLOAD_TTL, FileLinkError, FileTransferConfig

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import Source

#: Cap on ``source_add``'s ``bytes_base64`` payload — measured on the base64 STRING
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
    token = url.rsplit("/", 1)[1]
    # ``upload_url`` just stamped ``exp = now + UPLOAD_TTL``; compute it directly rather than
    # a full ``verify()`` (HMAC + base64 + JSON) purely to read one field back. Drift is ≤1s
    # on a 15-min TTL — immaterial to expires_at / _iso and the short-link store window.
    expires_at = int(time.time()) + UPLOAD_TTL
    # Tap-friendly short link over the SAME token: the human path gets ``/u/<shortid>``
    # (survives mobile-chat corruption of the long token — live-confirmed), the agent path
    # keeps the direct ``/files/ul`` URL (a ``/u/`` id only serves GET→redirect, not the raw
    # POST). await_upload accepts either.
    short_url = f"{cfg.base_url.rstrip('/')}/u/{cfg.short_links.put(token, expires_at)}"
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
        # itself reliably surfaces the link to the user (the mobile case). Uses the SHORT
        # ``/u/<shortid>`` link — a long opaque token gets mangled in a mobile chat.
        "human_upload": {
            "url": short_url,
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


#: Default poll window for :func:`_await_upload`. Kept safely under the ~60s
#: connector-timeout watchdog observed on claude.ai's remote MCP transport (which
#: measures time-to-first-response-byte); on timeout the tool returns ``pending`` so
#: the model re-invokes next turn — the re-invoke loop, not any keepalive, is the
#: load-bearing completion mechanism (ADR-0024 / Phase 1). Raise only after a live
#: timing test.
_AWAIT_TIMEOUT_S = 45.0
_AWAIT_POLL_INTERVAL_S = 2.0
#: Hard ceiling on a single ``await_upload`` poll — kept just under the ~60s connector
#: watchdog so the tool always returns a clean ``pending`` (→ re-invoke) before the transport
#: cuts the call. A caller asking for more is clamped, not honored.
_AWAIT_MAX_TIMEOUT_S = 55.0


def _extract_ul_token(token_or_url: str) -> str:
    """Return the bare ``ul`` token from either a raw token or a full
    ``{base}/files/ul/{token}`` URL. Tokens are ``base64url . base64url`` (no ``/``, ``?``
    or ``#``), so trimming at the first of those is safe."""
    text = token_or_url.strip()
    marker = "/files/ul/"
    if marker in text:
        text = text.split(marker, 1)[1]
    for sep in ("?", "#", "/"):
        text = text.split(sep, 1)[0]
    return text


def _resolve_upload_token(cfg: FileTransferConfig, token_or_url: str) -> str | None:
    """Resolve any of the three link shapes ``await_upload`` accepts to the signed token:
    a tap-friendly ``{base}/u/<shortid>`` (looked up in the in-process short-link store), a
    full ``{base}/files/ul/<token>``, or a bare token. Returns ``None`` only for an unknown
    or expired short id (the caller reports it as expired/invalid, same as a bad token)."""
    text = token_or_url.strip()
    if "/u/" in text:
        shortid = text.split("/u/", 1)[1]
        for sep in ("?", "#", "/"):
            shortid = shortid.split(sep, 1)[0]
        return cfg.short_links.get(shortid)
    return _extract_ul_token(text)


async def _await_upload(
    cfg: FileTransferConfig,
    token_or_url: str,
    *,
    timeout_s: float = _AWAIT_TIMEOUT_S,
    poll_interval_s: float = _AWAIT_POLL_INTERVAL_S,
    progress: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Poll the in-process completion map for the upload behind ``token_or_url``.

    Returns one of:
    - ``{"status": "received", "source_id": ..., "file": {...}}`` — the browser/agent
      upload committed a source (same process wrote it; ADR-0024).
    - ``{"status": "pending", "hint": ...}`` — nothing yet after ``timeout_s``; the
      model should re-invoke with the same link.
    - ``{"status": "expired_or_invalid", "hint": ...}`` — the link failed signature/
      expiry/op checks; mint a fresh one via ``source_add(source_type="file")``.

    ``progress`` (best-effort) is awaited at t=0 and each tick as a keepalive; the design
    does not depend on it.
    """
    invalid = {
        "status": "expired_or_invalid",
        "hint": "this upload link is invalid or expired — call "
        'source_add(source_type="file") to get a fresh one',
    }
    # Bound the poll window: a non-finite timeout would never satisfy the deadline check
    # (an unbounded loop), and one past the ~60s connector watchdog would let the request
    # die instead of returning a clean ``pending`` — breaking the re-invoke loop the design
    # relies on. Clamp to [0, _AWAIT_MAX_TIMEOUT_S]; reject NaN/inf outright.
    if not math.isfinite(timeout_s):
        raise ValidationError("timeout must be a finite number of seconds")
    timeout_s = max(0.0, min(timeout_s, _AWAIT_MAX_TIMEOUT_S))
    token = _resolve_upload_token(cfg, token_or_url)
    if token is None:  # unknown/expired short id
        return invalid
    try:
        payload = cfg.signer.verify(token, op="ul")
    except FileLinkError:
        # The start-token may have expired WHILE a large upload finished — but the
        # ``/files/ul`` POST verified it live and committed a result. Recover that result
        # (MAC + op still enforced via allow_expired) rather than lose a successful add; a
        # truly bad/forged token, or an expired one with nothing committed, stays invalid.
        try:
            expired_payload = cfg.signer.verify(token, op="ul", allow_expired=True)
        except FileLinkError:
            return invalid
        done = cfg.jti_store.completed(str(expired_payload.get("jti") or ""))
        if done is not None:
            return {"status": "received", "source_id": done.get("source_id"), "file": done}
        return invalid
    jti = str(payload.get("jti") or "")
    deadline = time.monotonic() + timeout_s
    if progress is not None:
        await progress()
    while True:
        result = cfg.jti_store.completed(jti)
        if result is not None:
            return {"status": "received", "source_id": result.get("source_id"), "file": result}
        if time.monotonic() >= deadline:
            return {
                "status": "pending",
                "hint": "upload not detected yet — re-invoke await_upload with the same link",
            }
        # Never sleep past the deadline — cap the tick to the time remaining so a small
        # custom timeout returns ``pending`` on time instead of overshooting by an interval.
        await asyncio.sleep(min(poll_interval_s, max(0.0, deadline - time.monotonic())))
        if progress is not None:
            await progress()


def register_file_tools(mcp: Any) -> None:
    """Register the file-transfer MCP tools that live in this sibling module.

    Currently just ``await_upload`` (Phase 1). Called from ``tools.sources.register``
    so the sources domain keeps a single manifest entry point while this module holds
    the file-specific overflow (ADR-0008 size budget)."""

    @mcp.tool(annotations=READ_ONLY)
    async def await_upload(ctx: Context, upload_link: str, timeout: float = 45.0) -> dict[str, Any]:
        """Wait for a file uploaded via a ``source_add(source_type="file")`` link to land.

        Pass the ``human_upload.url`` (or the bare token) that ``source_add`` returned.
        Polls the server in-process until the browser/agent upload commits the source:

        * ``{"status":"received","source_id",...,"file":{...}}`` — the upload landed.
        * ``{"status":"pending",...}`` — nothing yet after ~``timeout`` s; **re-invoke with
          the same link** (the wait resumes; a transport reset does not lose it).
        * ``{"status":"expired_or_invalid",...}`` — the link failed; mint a fresh one via
          ``source_add(source_type="file")``.
        """
        with mcp_errors():
            cfg = get_file_transfer(ctx)
            if cfg is None:
                raise ValidationError(
                    "await_upload needs the remote signed-URL transport; set "
                    "NOTEBOOKLM_MCP_PUBLIC_URL on the server to enable it"
                )
            # ponytail: no ctx.report_progress keepalive yet — the re-invoke loop is the
            # load-bearing completion path; add one only if a live claude.ai timing test
            # shows the ~45s poll needs it (ADR-0024 / Phase 1 plan).
            return await _await_upload(cfg, upload_link, timeout_s=timeout)
