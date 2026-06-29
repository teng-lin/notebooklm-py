"""Artifact (Studio) MCP tools.

Thin adapters over the transport-neutral artifact cores:

* ``artifact_list`` reads ``client.artifacts.list`` directly (like ``source_list``).
* ``artifact_generate`` is a hybrid over the neutral ``generate`` core: it builds a
  :class:`~notebooklm._app.generate.GenerationPlan` via ``build_generation_plan``
  (which enum-maps + validates the per-kind options) and drives
  ``execute_generation`` with **pass-through** notebook/source resolvers (MCP has
  already resolved the notebook id and supplies full source ids). Each ``type``
  routes to the matching ``client.artifacts.generate_*`` method.
* ``artifact_status`` is the **stateless** poll path (``_app.artifacts.poll_artifact``
  → ``client.artifacts.poll_status``) so an agent can poll a ``task_id`` across
  separate tool calls without holding server state.
* ``artifact_download`` is a hybrid over the neutral ``download`` core: each
  ``type`` selects a :class:`~notebooklm._app.download.DownloadTypeSpec` row and
  ``build_download_plan`` + ``execute_download`` run with pass-through resolvers.

This module imports NO ``click`` / ``rich`` / ``cli`` — the ``DownloadTypeSpec``
registry rows are rebuilt here from the neutral ``_app.download`` types rather
than imported from ``cli/_download_specs.py``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import ResourceLink
from pydantic import AnyUrl

from ..._app import artifacts as artifact_core
from ..._app import download as download_core
from ..._app import generate as generate_core
from ..._app.language import is_supported_language
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from ...types import ArtifactType
from .._coerce import coerce_list
from .._confirm import READ_ONLY
from .._context import get_client, get_file_transfer
from .._errors import mcp_errors
from .._filelink import DOWNLOAD_TTL, FileTransferConfig
from .._resolve import resolve_notebook, resolve_source
from ._passthrough import passthrough_notebook_id

if TYPE_CHECKING:
    from ...client import NotebookLMClient

#: Per-kind default option values mirroring the CLI ``generate`` Click ``Choice``
#: defaults, so a bare ``artifact_generate(notebook, type=…)`` succeeds without
#: the agent restating every enum. The agent can override any of these by passing
#: the matching keyword; ``build_generation_plan`` enum-maps + validates them.
_KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "audio": {"audio_format": "deep-dive", "audio_length": "default"},
    "video": {"video_format": "explainer", "style": "auto"},
    "cinematic-video": {},
    "slide-deck": {"deck_format": "detailed", "deck_length": "default"},
    "quiz": {"quantity": "standard", "difficulty": "medium"},
    "flashcards": {"quantity": "standard", "difficulty": "medium"},
    "infographic": {"orientation": "landscape", "detail": "standard", "style": "auto"},
    "data-table": {},
    "mind-map": {"map_kind": "interactive"},
    "report": {"report_format": "briefing-doc"},
}

#: Per-kind agent-settable options → their accepted choices. ``None`` choices mean
#: free text (only ``style_prompt``). This single table drives all three things the
#: agent-facing override path needs:
#:
#: * **Choice validation** up front, so a bad value surfaces as a clean ``VALIDATION``
#:   error rather than a raw ``KeyError`` from a generate-core display-name lookup that
#:   runs before its own choice validation (the CLI never hits this — Click validates
#:   the ``Choice`` first).
#: * **The ``style`` collision** — ``video`` and ``infographic`` both take a ``style``
#:   kwarg but with DIFFERENT choice sets (overlapping only on ``auto``/``anime``/
#:   ``kawaii``); keying choices by ``artifact_type`` keeps them apart.
#: * **Wrong-kind rejection** — an option valid for some other kind (e.g. ``orientation``
#:   passed to ``quiz``) is rejected here, because the neutral core silently *ignores*
#:   irrelevant extras (``build_generation_plan`` "picks the relevant subset"), which
#:   would otherwise be a confusing silent no-op for an agent.
#:
#: The literal choice tuples are DUPLICATED from the neutral core's private ``_*_MAP``
#: maps (MCP must not import them — the CLI/MCP boundary rule); a guardrail test pins
#: these tuples equal to the core maps so they can't silently drift. ``map_kind`` has no
#: core map (the core reads it raw and any non-``interactive`` value routes note-backed),
#: so it is validated here ONLY.
_KIND_OPTIONS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "audio": {
        "audio_format": ("deep-dive", "brief", "critique", "debate"),
        "audio_length": ("short", "default", "long"),
    },
    "video": {
        "video_format": ("explainer", "brief", "cinematic"),
        "style": (
            "auto",
            "custom",
            "classic",
            "whiteboard",
            "kawaii",
            "anime",
            "watercolor",
            "retro-print",
            "heritage",
            "paper-craft",
        ),
        "style_prompt": None,
    },
    "cinematic-video": {},
    "slide-deck": {
        "deck_format": ("detailed", "presenter"),
        "deck_length": ("default", "short"),
    },
    "quiz": {
        "quantity": ("fewer", "standard", "more"),
        "difficulty": ("easy", "medium", "hard"),
    },
    "flashcards": {
        "quantity": ("fewer", "standard", "more"),
        "difficulty": ("easy", "medium", "hard"),
    },
    "infographic": {
        "orientation": ("landscape", "portrait", "square"),
        "detail": ("concise", "standard", "detailed"),
        "style": (
            "auto",
            "sketch-note",
            "professional",
            "bento-grid",
            "editorial",
            "instructional",
            "bricks",
            "clay",
            "anime",
            "kawaii",
            "scientific",
        ),
    },
    "data-table": {},
    "mind-map": {"map_kind": ("interactive", "note-backed")},
    "report": {"report_format": ("briefing-doc", "study-guide", "blog-post", "custom")},
}

#: Download type registry, rebuilt from the neutral ``_app.download`` types so this
#: module never imports the Click-coupled ``cli/_download_specs.py``. Each row
#: mirrors the corresponding CLI ``DownloadTypeSpec`` (name / kind / extension /
#: download method / optional ``--format`` wiring).
_DOWNLOAD_SPECS: dict[str, download_core.DownloadTypeSpec] = {
    "audio": download_core.DownloadTypeSpec(
        name="audio",
        kind=ArtifactType.AUDIO,
        extension=".mp3",
        default_dir="./audio",
        download_attr="download_audio",
        help_summary="",
        help_examples="",
    ),
    "video": download_core.DownloadTypeSpec(
        name="video",
        kind=ArtifactType.VIDEO,
        extension=".mp4",
        default_dir="./video",
        download_attr="download_video",
        help_summary="",
        help_examples="",
    ),
    "slide-deck": download_core.DownloadTypeSpec(
        name="slide-deck",
        kind=ArtifactType.SLIDE_DECK,
        extension=".pdf",
        default_dir="./slide-decks",
        download_attr="download_slide_deck",
        format_choices=("pdf", "pptx"),
        format_default="pdf",
        format_extension_map={"pdf": ".pdf", "pptx": ".pptx"},
        format_kwarg="output_format",
        forward_format_only_if_set=True,
        help_summary="",
        help_examples="",
    ),
    "infographic": download_core.DownloadTypeSpec(
        name="infographic",
        kind=ArtifactType.INFOGRAPHIC,
        extension=".png",
        default_dir="./infographic",
        download_attr="download_infographic",
        help_summary="",
        help_examples="",
    ),
    "report": download_core.DownloadTypeSpec(
        name="report",
        kind=ArtifactType.REPORT,
        extension=".md",
        default_dir="./reports",
        download_attr="download_report",
        help_summary="",
        help_examples="",
    ),
    "mind-map": download_core.DownloadTypeSpec(
        name="mind-map",
        kind=ArtifactType.MIND_MAP,
        extension=".json",
        default_dir="./mind-maps",
        download_attr="download_mind_map",
        help_summary="",
        help_examples="",
    ),
    "data-table": download_core.DownloadTypeSpec(
        name="data-table",
        kind=ArtifactType.DATA_TABLE,
        extension=".csv",
        default_dir="./data-tables",
        download_attr="download_data_table",
        help_summary="",
        help_examples="",
    ),
    "quiz": download_core.DownloadTypeSpec(
        name="quiz",
        kind=ArtifactType.QUIZ,
        extension=".json",
        default_dir="./quizzes",
        download_attr="download_quiz",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_extension_map=dict(download_core.FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        help_summary="",
        help_examples="",
    ),
    "flashcards": download_core.DownloadTypeSpec(
        name="flashcards",
        kind=ArtifactType.FLASHCARDS,
        extension=".json",
        default_dir="./flashcards",
        download_attr="download_flashcards",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_extension_map=dict(download_core.FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        help_summary="",
        help_examples="",
    ),
}


async def _passthrough_sources(
    _client: NotebookLMClient,
    _notebook_id: str,
    source_ids: Any,
    *,
    json_output: bool = False,
) -> Any:
    """Return the supplied (already-full) source ids, or ``None`` when none were
    given so the backend uses *every* source.

    MCP supplies full source ids, so no partial-id resolution is needed. But an
    EMPTY collection must map to ``None`` (not ``[]``): the generate core treats
    ``None`` as "all sources" (mirroring the CLI's ``resolve_source_ids``, which
    returns ``None`` for no input), whereas an empty list means "zero sources" —
    which the backend refuses for source-needing kinds (quiz/audio/flashcards),
    returning a null id surfaced as ``… generation is unavailable``. The tool
    passes ``tuple(source_ids or ())``, so omitting ``source_ids`` arrives here
    as ``()`` and must become ``None``."""
    return source_ids or None


async def _passthrough_download_notebook(notebook_id: str) -> str:
    """Async pass-through notebook resolver for the download core."""
    return notebook_id


def _no_partial_artifact(_artifacts: list[Any], artifact_id: str) -> str:
    """Artifact-id resolver for the download core (MCP passes a full id through)."""
    return artifact_id


def _is_http_transport() -> bool:
    """Whether the current tool call arrived over the http transport.

    A remote (http) call has an active Starlette request; stdio does not
    (:func:`get_http_request` raises ``RuntimeError``). Lets a remote download
    *without* file transfer configured report a clean "not configured" error
    instead of the stdio "requires path" error.
    """
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


def _broker_download(
    cfg: FileTransferConfig,
    notebook_id: str,
    artifact_type: str,
    output_format: str | None,
) -> ToolResult:
    """Mint a signed download URL + a clickable ``resource_link`` for a remote
    ``artifact_download``.

    Returns a :class:`ToolResult` carrying BOTH a ``resource_link`` content item
    (claude.ai renders it clickable) and the structured ``download_ready`` payload.
    The signer injects expiry; ``expires_at`` mirrors the download TTL.
    """
    payload: dict[str, Any] = {
        "nb": notebook_id,
        "atype": artifact_type,
    }  # op stamped by download_url
    if output_format is not None:
        payload["fmt"] = output_format
    url = cfg.download_url(payload)
    structured = {
        "status": "download_ready",
        "notebook_id": notebook_id,
        "artifact_type": artifact_type,
        "url": url,
        "expires_at": int(time.time()) + DOWNLOAD_TTL,
    }
    link = ResourceLink(
        type="resource_link",
        name=f"{artifact_type} download",
        # ResourceLink.uri is an AnyUrl — construct it explicitly rather than
        # passing the raw str (keeps mypy happy across pydantic-stub versions:
        # a bare str needed a [arg-type] ignore that CI's stubs flagged unused).
        uri=AnyUrl(url),
        description=f"Download the latest {artifact_type} artifact (link expires).",
    )
    return ToolResult(content=[link], structured_content=structured)


def register(mcp: Any) -> None:
    """Register the artifact tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def artifact_list(ctx: Context, notebook: str) -> dict[str, Any]:
        """List a notebook's studio artifacts. Accepts a notebook name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            artifacts = await client.artifacts.list(nb_id)
            return {"notebook_id": nb_id, "artifacts": to_jsonable(artifacts)}

    @mcp.tool
    async def artifact_generate(
        ctx: Context,
        notebook: str,
        artifact_type: Literal[
            "audio",
            "video",
            "cinematic-video",
            "slide-deck",
            "quiz",
            "flashcards",
            "infographic",
            "data-table",
            "mind-map",
            "report",
        ],
        source_ids: list[str] | str | None = None,
        instructions: str = "",
        language: str | None = None,
        report_format: str | None = None,
        audio_format: str | None = None,
        audio_length: str | None = None,
        quantity: str | None = None,
        difficulty: str | None = None,
        video_format: str | None = None,
        style: str | None = None,
        style_prompt: str | None = None,
        deck_format: str | None = None,
        deck_length: str | None = None,
        orientation: str | None = None,
        detail: str | None = None,
        map_kind: str | None = None,
    ) -> dict[str, Any]:
        """Start generating a studio artifact. Accepts a notebook name or ID.

        Non-blocking: returns immediately with a ``task_id``; poll
        ``artifact_status(notebook, task_id)`` until ``is_complete`` is true.

        ``artifact_type`` selects the artifact kind (each routes to its own
        generator):

        * ``audio``        — podcast-style overview (``audio_format``:
          deep-dive|brief|critique|debate, ``audio_length``: short|default|long).
        * ``video``        — video overview (``video_format``:
          explainer|brief|cinematic, ``style``: auto|custom|classic|whiteboard|
          kawaii|anime|watercolor|retro-print|heritage|paper-craft, ``style_prompt``:
          free-text custom-style prompt — requires ``style=custom``).
        * ``cinematic-video`` — AI-generated documentary video (no per-kind options).
        * ``slide-deck``   — slide deck (``deck_format``: detailed|presenter,
          ``deck_length``: default|short).
        * ``quiz`` / ``flashcards`` — study aids (``quantity``:
          fewer|standard|more, ``difficulty``: easy|medium|hard).
        * ``infographic``  — single-image infographic (``orientation``:
          landscape|portrait|square, ``detail``: concise|standard|detailed,
          ``style``: auto|sketch-note|professional|bento-grid|editorial|
          instructional|bricks|clay|anime|kawaii|scientific).
        * ``data-table``   — extracted data table (no per-kind options).
        * ``mind-map``     — mind map (``map_kind``: interactive|note-backed).
        * ``report``       — text report (``report_format``:
          briefing-doc|study-guide|blog-post|custom).

        Each per-kind option is valid ONLY for the kind(s) listed above; passing one
        to a different ``artifact_type`` (e.g. ``orientation`` to ``quiz``) is a
        validation error rather than a silent no-op. Options default to the standard
        choice when omitted. Note ``style`` is shared by ``video`` and ``infographic``
        but accepts each kind's own set of values.

        ``source_ids`` (optional) scopes generation to specific sources; omit it
        to use every source. It accepts a real list, a JSON-array string, or a
        comma-separated string (the comma form cannot carry a source title that
        itself contains a comma — use a JSON array or a real list for those).
        ``instructions`` is free-text guidance for kinds that accept it
        (including ``mind-map``).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Tolerate ``source_ids`` sent as a JSON-array string / comma string /
            # scalar (some MCP clients + LLM tool-callers do); normalize to a
            # ``list[str]`` up front. ``None`` stays ``None`` (=> all sources, the
            # #1652 contract); ``""``/``[]`` collapse to all sources downstream.
            source_ids = coerce_list(source_ids)
            # ``artifact_type`` is a Literal — FastMCP/Pydantic rejects an unknown
            # kind at the schema boundary, so no runtime membership check is needed.
            # Validate ``language`` up front: the neutral generate core's default
            # language resolver returns the raw string unchecked (the CLI
            # validates via SUPPORTED_LANGUAGES first), so a bad code would be
            # forwarded raw to the backend. Fail with a clean VALIDATION instead.
            if language is not None and not is_supported_language(language):
                raise ValidationError(f"Unsupported language {language!r}")

            # Validate caller-supplied per-kind overrides FIRST — before resolving the
            # notebook — so a wrong-kind or invalid option fails fast without a wasted
            # notebook-resolution round-trip. Each option is validated against the choice
            # set for THIS ``artifact_type`` (see ``_KIND_OPTIONS``): an option not accepted
            # by this kind is rejected (the core would otherwise silently ignore it), and a
            # bad value surfaces a clean VALIDATION error. ``style_prompt`` (choices
            # ``None``) is free text — the core enforces the ``style=custom`` ⇔
            # ``style_prompt`` combination rules.
            allowed = _KIND_OPTIONS[artifact_type]
            overrides: dict[str, Any] = {}
            for key, value in (
                ("report_format", report_format),
                ("audio_format", audio_format),
                ("audio_length", audio_length),
                ("quantity", quantity),
                ("difficulty", difficulty),
                ("video_format", video_format),
                ("style", style),
                ("style_prompt", style_prompt),
                ("deck_format", deck_format),
                ("deck_length", deck_length),
                ("orientation", orientation),
                ("detail", detail),
                ("map_kind", map_kind),
            ):
                if value is None:
                    continue
                if key not in allowed:
                    accepts = (
                        f"this kind accepts {sorted(allowed)}"
                        if allowed
                        else "this kind accepts no per-kind options"
                    )
                    raise ValidationError(
                        f"option {key!r} is not valid for artifact_type {artifact_type!r}; "
                        f"{accepts}"
                    )
                choices = allowed[key]
                if choices is not None and value not in choices:
                    raise ValidationError(
                        f"Invalid {key} {value!r}; expected one of {list(choices)}"
                    )
                overrides[key] = value

            nb_id = await resolve_notebook(client, notebook)
            # Resolve each source ref the same way every other source-accepting tool
            # does (full-UUID fast-path, 12-char prefix, exact title) instead of
            # forwarding raw — so an agent that passed a prefix/title (the style that
            # works elsewhere) or an empty string gets it validated/resolved, not
            # forwarded to the backend. Omitted/empty stays None (= all sources, #1652).
            resolved_source_ids = (
                list(
                    await asyncio.gather(
                        *(resolve_source(client, nb_id, ref) for ref in source_ids)
                    )
                )
                if source_ids
                else None
            )
            raw_args: dict[str, Any] = dict(_KIND_DEFAULTS[artifact_type])
            raw_args.update(
                {
                    "notebook_id": nb_id,
                    "description": instructions or "",
                    # ``mind-map`` reads ``raw_args["instructions"]`` (every other kind
                    # reads ``description``); set it so mind-map instructions actually
                    # reach the client — the extra key is ignored by the other builders.
                    "instructions": instructions or None,
                    "source_ids": tuple(resolved_source_ids or ()),
                    "language": language,
                    "wait": False,
                    "json_output": True,
                }
            )
            raw_args.update(overrides)

            plan = generate_core.build_generation_plan(artifact_type, raw_args)
            result = await generate_core.execute_generation(
                plan,
                client,
                notebook_resolver=passthrough_notebook_id,
                source_resolver=_passthrough_sources,
            )
            return _generation_payload(nb_id, result)

    @mcp.tool(annotations=READ_ONLY)
    async def artifact_status(ctx: Context, notebook: str, task_id: str) -> dict[str, Any]:
        """Poll a generation task's status. Accepts a notebook name or ID.

        Stateless: pass the ``task_id`` returned by ``artifact_generate``. Returns
        ``status`` / ``url`` / ``error`` / ``is_complete``; call repeatedly until
        ``is_complete`` is true.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            status = await artifact_core.poll_artifact(client, nb_id, task_id)
            view = artifact_core.status_view(status)
            return {"notebook_id": nb_id, **to_jsonable(view)}

    @mcp.tool
    async def artifact_download(
        ctx: Context,
        notebook: str,
        artifact_type: Literal[
            "audio",
            "video",
            "slide-deck",
            "infographic",
            "report",
            "mind-map",
            "data-table",
            "quiz",
            "flashcards",
        ],
        path: str | None = None,
        output_format: str | None = None,
    ) -> Any:
        """Download a generated artifact. Accepts a notebook name or ID.

        ``artifact_type`` is one of audio|video|slide-deck|infographic|report|
        mind-map|data-table|quiz|flashcards (the latest artifact of that type is
        selected). ``output_format`` overrides the default file format where
        supported: slide-deck → pdf|pptx; quiz/flashcards → json|markdown|html.

        Over **stdio** the artifact is written to ``path`` (the output file on the
        server host; required). Over the **remote (http) connector** the server's
        filesystem is unreachable, so the tool instead returns a clickable
        ``resource_link`` plus ``{"status": "download_ready", "url": …}`` — a
        short-lived signed URL; ``path`` is ignored.
        """
        client = get_client(ctx)
        with mcp_errors():
            spec = _DOWNLOAD_SPECS.get(artifact_type)
            if spec is None:
                raise ValidationError(
                    f"Unknown download type {artifact_type!r}; "
                    f"expected one of {sorted(_DOWNLOAD_SPECS)}"
                )
            # Validate output_format against the spec up front (shared by BOTH the
            # local-download and signed-URL paths) so stdio and the remote connector
            # fail identically — a bad value must not mint a token whose link 500s
            # only when the browser opens it.
            if output_format is not None:
                if not spec.format_choices:
                    raise ValidationError(
                        f"artifact_type {artifact_type!r} does not support an output_format option"
                    )
                if output_format not in spec.format_choices:
                    raise ValidationError(
                        f"output_format {output_format!r} is not valid for artifact_type "
                        f"{artifact_type!r}; expected one of {sorted(spec.format_choices)}"
                    )
            nb_id = await resolve_notebook(client, notebook)

            cfg = get_file_transfer(ctx)
            if cfg is not None:
                # Remote connector: broker a signed download URL (the server path is
                # unreachable). `path` is accepted but ignored.
                return _broker_download(cfg, nb_id, artifact_type, output_format)
            # No file-transfer config. On the remote (http) connector the server
            # filesystem is unreachable REGARDLESS of `path`, so fail clearly here —
            # mirroring source_add type=file — BEFORE any server-side download (else a
            # supplied `path` would silently write the artifact onto the server).
            if _is_http_transport():
                raise ValidationError(
                    "remote file transfer is not configured; set "
                    "NOTEBOOKLM_MCP_PUBLIC_URL on the server to enable it"
                )
            if path is None:
                raise ValidationError("artifact_download requires 'path' on the stdio transport")

            args: dict[str, Any] = {
                "notebook_id": nb_id,
                "output_path": path,
                "latest": True,
            }
            if output_format is not None:
                args[spec.format_param_name] = output_format
            plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
            result = await download_core.execute_download(
                plan,
                client,
                notebook_resolver=_passthrough_download_notebook,
                artifact_resolver=_no_partial_artifact,
            )
            return to_jsonable(result)


def _generation_payload(
    notebook_id: str, result: generate_core.GenerationExecutionResult
) -> dict[str, Any]:
    """Project a :class:`GenerationExecutionResult` onto the wire shape.

    Surfaces the ``task_id`` an agent polls with ``artifact_status`` plus the
    generation outcome (status / url / error) or, for mind maps, the rendered
    map. Mind-map generation renders synchronously (no ``task_id`` to poll).
    """
    payload: dict[str, Any] = {
        "notebook_id": notebook_id,
        "kind": result.kind,
    }
    if result.mind_map is not None:
        payload["mind_map"] = to_jsonable(result.mind_map)
        return payload
    outcome = result.generation
    if outcome is not None:
        payload.update(
            {
                "task_id": outcome.task_id,
                "status": outcome.status,
                "url": outcome.url,
                "error": outcome.error,
            }
        )
    return payload
