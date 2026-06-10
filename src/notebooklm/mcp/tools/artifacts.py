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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import Context

from ..._app import artifacts as artifact_core
from ..._app import download as download_core
from ..._app import generate as generate_core
from ..._app.language import is_supported_language
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from ...types import ArtifactType
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook
from ._passthrough import passthrough_notebook_id

if TYPE_CHECKING:
    from ...client import NotebookLMClient

#: The generation kinds an agent may request via ``artifact_generate``. Mirrors
#: the neutral ``generate`` core's :data:`~notebooklm._app.generate.GenerationKind`
#: (minus ``revise-slide``, which mutates an existing slide deck rather than
#: producing a fresh artifact — not a from-scratch generation).
_GENERATE_TYPES = (
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
)

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

#: Accepted values for the agent-facing per-kind options. Validated up front so a
#: bad choice surfaces as a clean ``VALIDATION`` error rather than a raw
#: ``KeyError`` from a generate-core display-name lookup that runs before its own
#: choice validation (the CLI never hits this because Click validates the
#: ``Choice`` first; the neutral core's per-kind validation is incomplete for
#: ``report_format``). The agent may still pass any of these by keyword.
_OPTION_CHOICES: dict[str, tuple[str, ...]] = {
    "report_format": ("briefing-doc", "study-guide", "blog-post", "custom"),
    "audio_format": ("deep-dive", "brief", "critique", "debate"),
    "audio_length": ("short", "default", "long"),
    "quantity": ("fewer", "standard", "more"),
    "difficulty": ("easy", "medium", "hard"),
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
    """Return ``source_ids`` unchanged (MCP supplies full source ids)."""
    return source_ids


async def _passthrough_download_notebook(notebook_id: str) -> str:
    """Async pass-through notebook resolver for the download core."""
    return notebook_id


def _no_partial_artifact(_artifacts: list[Any], artifact_id: str) -> str:
    """Artifact-id resolver for the download core (MCP passes a full id through)."""
    return artifact_id


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
        artifact_type: str,
        source_ids: list[str] | None = None,
        instructions: str = "",
        language: str | None = None,
        report_format: str | None = None,
        audio_format: str | None = None,
        audio_length: str | None = None,
        quantity: str | None = None,
        difficulty: str | None = None,
    ) -> dict[str, Any]:
        """Start generating a studio artifact. Accepts a notebook name or ID.

        Non-blocking: returns immediately with a ``task_id``; poll
        ``artifact_status(notebook, task_id)`` until ``is_complete`` is true.

        ``artifact_type`` selects the artifact kind (each routes to its own
        generator):

        * ``audio``        — podcast-style overview (``audio_format``:
          deep-dive|brief|critique|debate, ``audio_length``: short|default|long).
        * ``video`` / ``cinematic-video`` — video overview.
        * ``slide-deck``   — slide deck.
        * ``quiz`` / ``flashcards`` — study aids (``quantity``:
          fewer|standard|more, ``difficulty``: easy|medium|hard).
        * ``infographic``  — single-image infographic.
        * ``data-table``   — extracted data table.
        * ``mind-map``     — interactive mind map.
        * ``report``       — text report (``report_format``:
          briefing-doc|study-guide|blog-post|custom).

        Only the options listed above are agent-controllable: ``audio``
        (``audio_format``/``audio_length``), ``quiz``/``flashcards``
        (``quantity``/``difficulty``), and ``report`` (``report_format``). The
        other kinds — ``video``, ``cinematic-video``, ``slide-deck``,
        ``infographic``, ``data-table``, and ``mind-map`` — use FIXED internal
        defaults for their per-kind options (video format/style, deck
        format/length, infographic orientation/detail, mind-map kind) and do NOT
        expose them as settable parameters.

        ``source_ids`` (optional) scopes generation to specific sources; omit it
        to use every source. ``instructions`` is free-text guidance for kinds
        that accept it. Each agent-controllable option defaults to the standard
        choice when omitted.
        """
        client = get_client(ctx)
        with mcp_errors():
            if artifact_type not in _GENERATE_TYPES:
                raise ValidationError(
                    f"Unknown artifact type {artifact_type!r}; "
                    f"expected one of {list(_GENERATE_TYPES)}"
                )
            # Validate ``language`` up front: the neutral generate core's default
            # language resolver returns the raw string unchecked (the CLI
            # validates via SUPPORTED_LANGUAGES first), so a bad code would be
            # forwarded raw to the backend. Fail with a clean VALIDATION instead.
            if language is not None and not is_supported_language(language):
                raise ValidationError(f"Unsupported language {language!r}")
            nb_id = await resolve_notebook(client, notebook)
            raw_args: dict[str, Any] = dict(_KIND_DEFAULTS[artifact_type])
            raw_args.update(
                {
                    "notebook_id": nb_id,
                    "description": instructions or "",
                    "source_ids": tuple(source_ids or ()),
                    "language": language,
                    "wait": False,
                    "json_output": True,
                }
            )
            # Apply caller-supplied per-kind overrides over the defaults,
            # validating each against its choice set first.
            for key, value in (
                ("report_format", report_format),
                ("audio_format", audio_format),
                ("audio_length", audio_length),
                ("quantity", quantity),
                ("difficulty", difficulty),
            ):
                if value is not None:
                    choices = _OPTION_CHOICES[key]
                    if value not in choices:
                        raise ValidationError(
                            f"Invalid {key} {value!r}; expected one of {list(choices)}"
                        )
                    raw_args[key] = value

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
        artifact_type: str,
        path: str,
        output_format: str | None = None,
    ) -> dict[str, Any]:
        """Download a generated artifact to a local path. Accepts a notebook name or ID.

        ``artifact_type`` is one of audio|video|slide-deck|infographic|report|
        mind-map|data-table|quiz|flashcards. ``path`` is the output file on the
        server host (the latest artifact of that type is selected).
        ``output_format`` overrides the default file format where supported:
        slide-deck → pdf|pptx; quiz/flashcards → json|markdown|html.
        """
        client = get_client(ctx)
        with mcp_errors():
            spec = _DOWNLOAD_SPECS.get(artifact_type)
            if spec is None:
                raise ValidationError(
                    f"Unknown download type {artifact_type!r}; "
                    f"expected one of {sorted(_DOWNLOAD_SPECS)}"
                )
            nb_id = await resolve_notebook(client, notebook)
            args: dict[str, Any] = {
                "notebook_id": nb_id,
                "output_path": path,
                "latest": True,
            }
            if output_format is not None:
                if not spec.format_choices:
                    # The type has no format axis (audio/video/report/etc.); a
                    # supplied ``output_format`` was previously dropped silently.
                    # Fail with a clean VALIDATION so the caller learns it is
                    # unsupported.
                    raise ValidationError(
                        f"artifact_type {artifact_type!r} does not support an output_format option"
                    )
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
