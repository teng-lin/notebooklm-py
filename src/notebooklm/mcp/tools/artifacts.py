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

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import ResourceLink
from pydantic import AnyUrl

from ..._app import artifacts as artifact_core
from ..._app import download as download_core
from ..._app import generate as generate_core
from ..._app.language import is_supported_language
from ..._app.resolve import resolve_ref
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from ...types import ArtifactType
from .._coerce import coerce_list
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client, get_file_transfer
from .._errors import mcp_errors
from .._filelink import DOWNLOAD_TTL, FileTransferConfig
from .._paginate import DEFAULT_LIMIT, paginate
from .._resolve import resolve_artifact, resolve_notebook, resolve_sources
from ._passthrough import passthrough_notebook_id
from ._preview import title_for_id

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

#: The downloadable artifact-type keys (the ``artifact_type`` param's enum).
DownloadType = Literal[
    "audio",
    "video",
    "slide-deck",
    "infographic",
    "report",
    "mind-map",
    "data-table",
    "quiz",
    "flashcards",
]

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

#: Reverse of ``_DOWNLOAD_SPECS`` — an artifact's ``ArtifactType`` (``.kind``) → the
#: download-type key. Lets ``artifact_download`` derive ``artifact_type`` from an
#: ``artifact`` name-or-id ref (so the caller need not repeat the type).
_KIND_TO_DOWNLOAD_KEY: dict[Any, DownloadType] = {
    spec.kind: cast(DownloadType, key) for key, spec in _DOWNLOAD_SPECS.items()
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


def _resolve_artifact_id(artifacts: list[Any], artifact_id: str) -> str:
    """Resolve a full / partial / UUID artifact id against the type-filtered list.

    Wraps the transport-neutral :func:`resolve_ref` (full-UUID fast-path, exact
    match, unique prefix; ambiguous / no-match prefixes raise ``ValidationError`` /
    ``AmbiguousIdError``). The fast-path returns a canonical UUID **verbatim**
    without scanning ``artifacts``, so we match it case-insensitively against the
    pre-fetched list and return the list's own id. This:

    * fixes uppercase full UUIDs — ``select_artifact`` compares ids
      case-sensitively, so returning the token's casing would spuriously miss; and
    * makes a not-found full UUID raise the SAME hard error as a not-found /
      ambiguous prefix (→ ``ToolError`` on stdio, 400 on the remote route) instead
      of falling through to the download core's soft ``ERROR`` outcome — matching
      how ``_resolve.py`` resolves notebooks / sources (every miss is ``NOT_FOUND``).
    """
    resolved = resolve_ref(
        artifact_id,
        artifacts,
        id_of=lambda a: a["id"],
        title_of=lambda a: a.get("title"),
    ).id
    # The full-UUID fast-path returns the caller's casing verbatim; for a prefix
    # match ``resolved`` is already the list's canonical id. A single
    # case-insensitive scan normalizes both and confirms membership.
    resolved_lower = resolved.lower()
    for artifact in artifacts:
        if str(artifact["id"]).lower() == resolved_lower:
            return str(artifact["id"])
    # Mirror ``select_artifact``'s "Artifact <id> not found" wording so the message
    # is uniform whether the miss is caught here or by the core.
    raise ValidationError(f"Artifact {artifact_id} not found")


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
    artifact_id: str | None = None,
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
    if artifact_id is not None:
        payload["aid"] = artifact_id
    if output_format is not None:
        payload["fmt"] = output_format
    url = cfg.download_url(payload)
    structured: dict[str, Any] = {
        "status": "download_ready",
        "notebook_id": notebook_id,
        "artifact_type": artifact_type,
        "url": url,
        "expires_at": int(time.time()) + DOWNLOAD_TTL,
    }
    if artifact_id is not None:
        # Echo the targeted id the link was brokered for, so the agent's response
        # records what it asked for (the token carries it, but the structured
        # payload should be self-describing).
        structured["artifact_id"] = artifact_id
        desc = f"Download {artifact_type} artifact {artifact_id} (link expires)."
    else:
        desc = f"Download the latest {artifact_type} artifact (link expires)."
    link = ResourceLink(
        type="resource_link",
        name=f"{artifact_type} download",
        # ResourceLink.uri is an AnyUrl — construct it explicitly rather than
        # passing the raw str (keeps mypy happy across pydantic-stub versions:
        # a bare str needed a [arg-type] ignore that CI's stubs flagged unused).
        uri=AnyUrl(url),
        description=desc,
    )
    return ToolResult(content=[link], structured_content=structured)


def register(mcp: Any) -> None:
    """Register the artifact tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def artifact_list(
        ctx: Context, notebook: str, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """List a notebook's studio artifacts. Accepts a notebook name or ID.

        Returns a bounded page: ``limit`` (default 50) artifacts from ``offset``,
        plus ``total`` / ``offset`` / ``has_more``. Page with ``offset += limit``.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            artifacts = await client.artifacts.list(nb_id)
            page, meta = paginate(to_jsonable(artifacts), limit, offset)
            return {"notebook_id": nb_id, "artifacts": page, **meta}

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
        # Finite-choice per-kind options are typed as ``Literal`` so FastMCP/Pydantic
        # emits a JSON-schema ``enum`` (agents discover valid values from the schema,
        # not by trial-and-error) and rejects out-of-enum values at the boundary. The
        # members are DUPLICATED from the neutral core's private ``_*_MAP`` maps via
        # ``_KIND_OPTIONS`` (the CLI/MCP boundary forbids importing them); a guardrail
        # test pins each ``enum`` equal to ``_KIND_OPTIONS`` (itself pinned to the core
        # maps) so they can't drift. ``style_prompt``/``language`` stay free text.
        report_format: Literal["briefing-doc", "study-guide", "blog-post", "custom"] | None = None,
        audio_format: Literal["deep-dive", "brief", "critique", "debate"] | None = None,
        audio_length: Literal["short", "default", "long"] | None = None,
        quantity: Literal["fewer", "standard", "more"] | None = None,
        difficulty: Literal["easy", "medium", "hard"] | None = None,
        video_format: Literal["explainer", "brief", "cinematic"] | None = None,
        # ``style`` is shared by ``video`` and ``infographic`` with DIFFERENT value
        # sets (overlap only auto/anime/kawaii). One param carries one Literal, so this
        # is the UNION of both kinds' values; the runtime ``_KIND_OPTIONS`` loop narrows
        # it per-kind (a video-only value on infographic, or vice versa, is rejected
        # there with a clean VALIDATION error).
        style: Literal[
            # full video set (auto/kawaii/anime also valid for infographic)
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
            # infographic styles not already listed above
            "sketch-note",
            "professional",
            "bento-grid",
            "editorial",
            "instructional",
            "bricks",
            "clay",
            "scientific",
        ]
        | None = None,
        style_prompt: str | None = None,
        deck_format: Literal["detailed", "presenter"] | None = None,
        deck_length: Literal["default", "short"] | None = None,
        orientation: Literal["landscape", "portrait", "square"] | None = None,
        detail: Literal["concise", "standard", "detailed"] | None = None,
        map_kind: Literal["interactive", "note-backed"] | None = None,
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
                await resolve_sources(client, nb_id, source_ids) if source_ids else None
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

    @mcp.tool(annotations=READ_ONLY)
    async def artifact_get_prompt(ctx: Context, notebook: str, artifact: str) -> dict[str, Any]:
        """Fetch the free-text prompt an artifact was generated from.

        Accepts a notebook/artifact name or ID. Returns the stored ``prompt``
        string, or ``null`` when the artifact records no prompt (e.g. a
        note-backed mind map) — ``prompt=None`` is a valid result, not an error.
        An unknown artifact id raises NOT_FOUND.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            artifact_id = await resolve_artifact(client, nb_id, artifact)
            prompt = await artifact_core.get_artifact_prompt(client, nb_id, artifact_id)
            return {"notebook_id": nb_id, "artifact_id": artifact_id, "prompt": prompt}

    @mcp.tool
    async def artifact_download(
        ctx: Context,
        notebook: str,
        artifact: str | None = None,
        artifact_type: DownloadType | None = None,
        path: str | None = None,
        output_format: Literal["pdf", "pptx", "json", "markdown", "html"] | None = None,
        artifact_id: str | None = None,
    ) -> Any:
        """Download a generated artifact. Accepts a notebook name or ID.

        Target the artifact in ONE of two ways (exactly one):
        * ``artifact`` — a name-or-id ref (title / id / unique-id-prefix), the same
          form the other ``artifact_*`` tools take. The tool resolves it to the
          artifact's type + id for you.
        * ``artifact_type`` — one of audio|video|slide-deck|infographic|report|
          mind-map|data-table|quiz|flashcards, optionally with ``artifact_id``
          (full or unique-prefix) for a specific one; omit ``artifact_id`` to get
          the latest artifact of that type.

        ``output_format`` overrides the default file format where supported:
        slide-deck → pdf|pptx; quiz/flashcards → json|markdown|html.

        Over **stdio** the artifact is written to ``path`` (the output file on the
        server host; required). Over the **remote (http) connector** the server's
        filesystem is unreachable, so the tool instead returns a clickable
        ``resource_link`` plus ``{"status": "download_ready", "url": …}`` — a
        short-lived signed URL; ``path`` is ignored. On the remote connector the
        broker cannot list artifacts, so an ``artifact_id`` is validated lazily when
        the link is opened (an unknown/ambiguous id then yields a 400), unlike
        ``output_format``, which is validated up front at the tool call.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            # Two addressing modes (exactly one): an `artifact` name-or-id ref
            # (resolved to its type + id, matching the sibling artifact_* tools) OR
            # an explicit `artifact_type` (+ optional `artifact_id`; else latest of
            # that type). The `artifact` ref path lists to derive the type — the
            # remote broker still gets a concrete type before minting the link.
            if artifact is not None:
                if artifact_type is not None or artifact_id is not None:
                    raise ValidationError(
                        "Provide either `artifact` (name/id) or `artifact_type`"
                        " (+ optional `artifact_id`), not both."
                    )
                resolved_id = await resolve_artifact(client, nb_id, artifact)
                items = await client.artifacts.list(nb_id)
                # ``resolve_artifact`` fast-paths a full UUID verbatim (no list), so
                # ``resolved_id`` may differ in case from the listed id — match
                # case-insensitively (mirrors the resolver's own casefold).
                match = next((a for a in items if a.id.lower() == resolved_id.lower()), None)
                if match is None:
                    raise ValidationError(
                        f"Could not determine the type of artifact {artifact!r} "
                        "(not found in the notebook's artifact list); pass `artifact_type`."
                    )
                artifact_type = _KIND_TO_DOWNLOAD_KEY.get(match.kind)
                if artifact_type is None:
                    raise ValidationError(
                        f"Artifact {artifact!r} has a non-downloadable type {match.kind!r}."
                    )
                artifact_id = resolved_id
            elif artifact_type is None:
                raise ValidationError("Provide `artifact` (name/id) or `artifact_type`.")
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

            cfg = get_file_transfer(ctx)
            if cfg is not None:
                # Remote connector: broker a signed download URL (the server path is
                # unreachable). `path` is accepted but ignored.
                return _broker_download(cfg, nb_id, artifact_type, output_format, artifact_id)
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
                "latest": artifact_id is None,
            }
            if artifact_id is not None:
                args["artifact_id"] = artifact_id
            if output_format is not None:
                args[spec.format_param_name] = output_format
            plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
            result = await download_core.execute_download(
                plan,
                client,
                notebook_resolver=_passthrough_download_notebook,
                artifact_resolver=_resolve_artifact_id,
            )
            return to_jsonable(result)

    @mcp.tool
    async def artifact_rename(
        ctx: Context, notebook: str, artifact: str, new_title: str
    ) -> dict[str, Any]:
        """Rename a studio artifact (title only). Accepts a notebook/artifact name or ID.

        Works for every artifact type — audio, video, slide-deck, quiz,
        flashcards, infographic, data-table, report, and BOTH mind-map kinds.
        Note-backed mind maps are renamed through the note system; interactive
        maps and regular artifacts through the artifact rename RPC. The kind
        routing is handled by the shared ``_app`` core, so callers need not know
        which backing an artifact has.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            art_id = await resolve_artifact(client, nb_id, artifact)
            result = await artifact_core.rename_artifact(client, nb_id, art_id, new_title)
            return {
                "status": "renamed",
                "notebook_id": nb_id,
                "artifact_id": result.artifact_id,
                "new_title": result.new_title,
                "is_mind_map": result.is_mind_map,
            }

    @mcp.tool
    async def artifact_retry(ctx: Context, notebook: str, artifact: str) -> dict[str, Any]:
        """Retry a failed Studio artifact in place (the UI "Retry" action).

        Accepts a notebook/artifact name or ID. Non-blocking: on acceptance it
        returns the kicked-off ``task_id`` (equal to the artifact id) and the new
        ``status``; poll ``artifact_status(notebook, task_id)`` until complete. A
        synchronous refusal (rate limit / quota / not-retryable) surfaces as an error.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            art_id = await resolve_artifact(client, nb_id, artifact)
            result = await artifact_core.retry_artifact(client, nb_id, art_id)
            return {
                "notebook_id": nb_id,
                "artifact_id": art_id,
                "task_id": result.task_id,
                "status": result.status.value,
            }

    @mcp.tool(annotations=DESTRUCTIVE)
    async def artifact_delete(
        ctx: Context, notebook: str, artifact: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Delete a studio artifact (irreversible). Accepts a notebook/artifact name or ID.

        Covers every artifact type, including both mind-map kinds: note-backed
        maps are *cleared* via the note system (not hard-removed — Google may
        garbage collect them later), interactive maps and regular artifacts are
        removed via the artifact delete RPC. The kind routing is handled by the
        shared ``_app`` core.

        Two-step confirmation: with ``confirm=False`` (default) it returns a
        ``needs_confirmation`` preview of the resolved artifact without deleting;
        call again with ``confirm=True`` to perform the delete. Deleting an
        already-absent full id is idempotent (no error).
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            art_id = await resolve_artifact(client, nb_id, artifact)
            if not confirm:
                # Best-effort title for the preview; a full-UUID that is absent
                # from the list resolves to None, which is fine for the preview.
                title = title_for_id(await client.artifacts.list(nb_id), art_id)
                return needs_confirmation(
                    {
                        "action": "delete_artifact",
                        "notebook_id": nb_id,
                        "artifact_id": art_id,
                        "title": title,
                    }
                )
            was_note_backed = await artifact_core.delete_artifact(client, nb_id, art_id)
            return {
                "status": "deleted",
                "notebook_id": nb_id,
                "artifact_id": art_id,
                "was_note_backed": was_note_backed,
            }


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
