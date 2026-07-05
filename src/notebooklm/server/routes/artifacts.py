"""Artifact (Studio) routes — generate / poll / download / list.

Adapters over the transport-neutral generate / download / artifacts cores and
the public ``client.artifacts`` namespace. The generation-kind defaults / option
choices and the download specs are rebuilt here from the neutral ``_app``
registries (``_app.generate_plans.GenerationKind`` + ``build_generation_plan``;
``_app.download.DownloadTypeSpec``) — never imported from the CLI's
``cli/_download_specs.py`` (which this layer must not touch) nor from the MCP
adapter's own re-derivation.

Generation is non-blocking: ``POST .../artifacts`` runs ``execute_generation``
with ``wait=False``, records the returned ``task_id`` in the pending registry,
and returns ``202``. The poll (``GET .../artifacts/{task_id}``) projects the raw
``GenerationState`` through the registry to resolve the same ``NOT_FOUND``
ambiguity as the source poll:

* a registry-known task at ``PENDING`` / ``IN_PROGRESS`` / ``NOT_FOUND`` → ``200``
  (still polling — ``NOT_FOUND`` is the one-shot post-generate lag);
* ``COMPLETED`` → ``200`` ready (dropped from the registry);
* ``REMOVED`` → ``410`` (sustained terminal absence; dropped);
* ``FAILED`` → ``409`` with the error (dropped);
* an unknown task id → ``404``.

Download streams the bytes from a **server-generated** ``mkstemp`` path (never a
caller-supplied one — ``build_download_plan`` does not validate path shapes),
then cleans it up.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from ..._app import artifacts as artifact_core
from ..._app import download as download_core
from ..._app import generate as generate_core
from ..._app.language import is_supported_language
from ..._app.resolve import FULL_ID_PATTERN
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from ...exceptions import ValidationError
from ...types import ArtifactType, GenerationState
from .._context import get_client, get_pending
from .._errors import safe_detail
from .._pagination import MAX_LIMIT, paginate_envelope
from .._pending import PendingRegistry
from ._passthrough import (
    passthrough_artifact_id,
    passthrough_download_notebook,
    passthrough_notebook_id,
    passthrough_source_ids,
)

__all__ = ["DOWNLOAD_SPECS", "GENERATE_TYPES", "router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/artifacts", tags=["artifacts"])


def _canonical_artifact_id(artifact_id: str) -> str:
    """Lowercase a full-UUID artifact id before the kind-aware core call.

    The rename/delete cores detect a note-backed mind map with a CASE-SENSITIVE
    scan of ``mind_maps.list`` / ``mind_maps.list_note_backed`` (whose ids are
    canonically lowercase), so an UPPERCASE full UUID would miss the mind-map
    route and be mislabeled — a note-backed map would report ``renamed`` /
    ``deleted`` without actually being cleared. Backend ids are canonically
    lowercase, so lowering a full UUID is safe for the plain artifact path too;
    a non-UUID (partial) ref is left untouched (its own resolver owns casing).
    Mirrors the MCP ``studio_rename`` / ``studio_delete`` full-UUID carve-out.
    """
    return artifact_id.lower() if FULL_ID_PATTERN.fullmatch(artifact_id) else artifact_id


ClientDep = Annotated[NotebookLMClient, Depends(get_client)]
PendingDep = Annotated[PendingRegistry, Depends(get_pending)]

#: Generation kinds the server exposes. Mirrors the neutral ``GenerationKind``
#: minus ``revise-slide`` (which mutates an existing deck rather than producing a
#: fresh artifact).
GENERATE_TYPES: tuple[str, ...] = (
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

#: Per-kind default option values (mirroring the CLI ``generate`` Choice
#: defaults) so a bare generate request succeeds without restating every enum.
#: ``build_generation_plan`` enum-maps + validates these.
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

#: Per-kind agent-settable options → their accepted choices (``None`` = free text,
#: only ``style_prompt``). Mirrors the MCP ``studio_generate`` ``_KIND_OPTIONS``
#: table so the REST generate route enforces the SAME three things:
#:
#: * **Choice validation** up front — a bad value is a clean 400, not a raw
#:   ``KeyError`` from a generate-core display-name lookup that runs before its own
#:   choice validation.
#: * **The ``style`` collision** — ``video`` and ``infographic`` both take a
#:   ``style`` kwarg with DIFFERENT value sets; keying by ``type`` keeps them apart.
#: * **Wrong-kind rejection** — an option irrelevant to the chosen type (e.g.
#:   ``orientation`` on ``quiz``) is rejected rather than silently ignored by the
#:   neutral core (``build_generation_plan`` "picks the relevant subset").
#:
#: The literal tuples are DUPLICATED from the neutral core's private ``_*_MAP``
#: maps (the server layer must not import the core privates — same rule the MCP
#: table follows); ``tests/server/test_artifacts.py`` pins them equal to the core
#: maps so they cannot silently drift. ``map_kind`` has no core map (the core reads
#: it raw), so it is validated here ONLY.
_KIND_OPTIONS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "audio": {
        "audio_format": ("deep-dive", "brief", "critique", "debate"),
        "audio_length": ("short", "default", "long"),
    },
    "video": {
        "video_format": ("explainer", "brief", "cinematic", "short"),
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


def _download_specs() -> dict[str, download_core.DownloadTypeSpec]:
    """Build the download-type registry from the neutral ``_app.download`` types.

    Each row mirrors the corresponding CLI ``DownloadTypeSpec`` (artifact kind /
    extension / download method / optional format axis). Rebuilt here so this
    layer never imports the Click-coupled ``cli/_download_specs.py``.
    """
    spec = download_core.DownloadTypeSpec
    fmt = dict(download_core.FORMAT_EXTENSIONS)
    return {
        "audio": spec(
            name="audio",
            kind=ArtifactType.AUDIO,
            extension=".mp3",
            default_dir="./audio",
            download_attr="download_audio",
            help_summary="",
            help_examples="",
        ),
        "video": spec(
            name="video",
            kind=ArtifactType.VIDEO,
            extension=".mp4",
            default_dir="./video",
            download_attr="download_video",
            help_summary="",
            help_examples="",
        ),
        "slide-deck": spec(
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
        "infographic": spec(
            name="infographic",
            kind=ArtifactType.INFOGRAPHIC,
            extension=".png",
            default_dir="./infographic",
            download_attr="download_infographic",
            help_summary="",
            help_examples="",
        ),
        "report": spec(
            name="report",
            kind=ArtifactType.REPORT,
            extension=".md",
            default_dir="./reports",
            download_attr="download_report",
            help_summary="",
            help_examples="",
        ),
        "mind-map": spec(
            name="mind-map",
            kind=ArtifactType.MIND_MAP,
            extension=".json",
            default_dir="./mind-maps",
            download_attr="download_mind_map",
            help_summary="",
            help_examples="",
        ),
        "data-table": spec(
            name="data-table",
            kind=ArtifactType.DATA_TABLE,
            extension=".csv",
            default_dir="./data-tables",
            download_attr="download_data_table",
            help_summary="",
            help_examples="",
        ),
        "quiz": spec(
            name="quiz",
            kind=ArtifactType.QUIZ,
            extension=".json",
            default_dir="./quizzes",
            download_attr="download_quiz",
            format_choices=("json", "markdown", "html"),
            format_default="json",
            format_extension_map=fmt,
            format_kwarg="output_format",
            help_summary="",
            help_examples="",
        ),
        "flashcards": spec(
            name="flashcards",
            kind=ArtifactType.FLASHCARDS,
            extension=".json",
            default_dir="./flashcards",
            download_attr="download_flashcards",
            format_choices=("json", "markdown", "html"),
            format_default="json",
            format_extension_map=fmt,
            format_kwarg="output_format",
            help_summary="",
            help_examples="",
        ),
    }


#: Download-type registry (built once at import).
DOWNLOAD_SPECS: dict[str, download_core.DownloadTypeSpec] = _download_specs()


class ArtifactGenerate(BaseModel):
    """Request body for starting a studio-artifact generation.

    Carries the full per-kind option surface (mirroring the MCP
    ``studio_generate`` tool). Each option is valid ONLY for the kind(s) that
    accept it — passing one to a different ``type`` (e.g. ``orientation`` to
    ``quiz``) is a 400, not a silent no-op. ``style`` is shared by ``video`` and
    ``infographic`` with each kind's own value set.
    """

    type: str
    source_ids: list[str] | None = None
    instructions: str = ""
    language: str | None = None
    report_format: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None
    quantity: str | None = None
    difficulty: str | None = None
    video_format: str | None = None
    style: str | None = None
    style_prompt: str | None = None
    deck_format: str | None = None
    deck_length: str | None = None
    orientation: str | None = None
    detail: str | None = None
    map_kind: str | None = None


class ArtifactRename(BaseModel):
    """Request body for renaming a studio artifact (title only)."""

    title: str


class ArtifactDownload(BaseModel):
    """Request body for downloading a generated artifact."""

    type: str
    output_format: str | None = None


@router.get("")
async def list_artifacts(
    notebook_id: str,
    client: ClientDep,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List a notebook's studio artifacts.

    Defaults to the full collection under ``artifacts`` (unchanged). Supply
    ``?limit=`` to slice and add a ``meta`` block; ``?offset=`` pages forward.
    """
    artifacts = await client.artifacts.list(notebook_id)
    return paginate_envelope(
        to_jsonable(artifacts), key="artifacts", limit=limit, offset=offset, notebook_id=notebook_id
    )


@router.post("", status_code=202)
async def generate(
    notebook_id: str, body: ArtifactGenerate, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Start generating a studio artifact (non-blocking → ``task_id``)."""
    if body.type not in GENERATE_TYPES:
        raise ValidationError(
            f"Unknown artifact type {body.type!r}; expected one of {list(GENERATE_TYPES)}"
        )
    if body.language is not None and not is_supported_language(body.language):
        raise ValidationError(f"Unsupported language {body.language!r}")

    # Validate caller-supplied per-kind overrides against the choice set for THIS
    # ``type`` (mirroring the MCP ``studio_generate`` loop): an option not accepted
    # by this kind is rejected — the neutral core would otherwise silently ignore
    # it — and a bad value is a clean 400. ``style_prompt`` (choices ``None``) is
    # free text; the core enforces the ``style=custom`` ⇔ ``style_prompt`` rule.
    allowed = _KIND_OPTIONS[body.type]
    overrides: dict[str, Any] = {}
    for key, value in (
        ("report_format", body.report_format),
        ("audio_format", body.audio_format),
        ("audio_length", body.audio_length),
        ("quantity", body.quantity),
        ("difficulty", body.difficulty),
        ("video_format", body.video_format),
        ("style", body.style),
        ("style_prompt", body.style_prompt),
        ("deck_format", body.deck_format),
        ("deck_length", body.deck_length),
        ("orientation", body.orientation),
        ("detail", body.detail),
        ("map_kind", body.map_kind),
    ):
        if value is None:
            continue
        if key not in allowed:
            accepts = (
                f"this kind accepts {sorted(allowed)}"
                if allowed
                else "this kind accepts no per-kind options"
            )
            raise ValidationError(f"option {key!r} is not valid for type {body.type!r}; {accepts}")
        choices = allowed[key]
        if choices is not None and value not in choices:
            raise ValidationError(f"Invalid {key} {value!r}; expected one of {list(choices)}")
        overrides[key] = value

    # Treat empty / whitespace-only instructions as absent so the default request
    # shape stays byte-identical (no blank prompt slot reaches the server).
    instructions = body.instructions if (body.instructions and body.instructions.strip()) else None
    raw_args: dict[str, Any] = dict(_KIND_DEFAULTS[body.type])
    raw_args.update(
        {
            "notebook_id": notebook_id,
            "description": instructions or "",
            # ``mind-map`` reads ``raw_args["instructions"]`` (every other kind reads
            # ``description``); forward BOTH so mind-map instructions actually reach
            # the client — the extra key is ignored by the other builders.
            "instructions": instructions,
            "source_ids": tuple(body.source_ids or ()),
            "language": body.language,
            "wait": False,
            "json_output": True,
        }
    )
    raw_args.update(overrides)

    plan = generate_core.build_generation_plan(body.type, raw_args)
    result = await generate_core.execute_generation(
        plan,
        client,
        notebook_resolver=passthrough_notebook_id,
        source_resolver=passthrough_source_ids,
    )
    return _generation_payload(notebook_id, result, pending)


@router.get("/{task_id}")
async def poll(
    notebook_id: str, task_id: str, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Poll a generation task, projecting state through the pending registry.

    An id the in-process registry has never seen (e.g. a task from a prior server
    process, or one whose post-generate NOT_FOUND lag has elapsed and been dropped)
    is a deliberate 404: this is a personal-automation surface with no persistent
    job store (reviewer consensus — see ``_pending.py``), so an unknown id is
    "not found" rather than silently 200. Re-poll a live task with its ``task_id``.
    """
    status = await artifact_core.poll_artifact(client, notebook_id, task_id)
    view = artifact_core.status_view(status)
    state = status.status
    projected = {"notebook_id": notebook_id, **to_jsonable(view)}

    if state in (GenerationState.PENDING, GenerationState.IN_PROGRESS):
        return projected
    if state == GenerationState.NOT_FOUND:
        if pending.knows(notebook_id, task_id):
            return projected
        raise HTTPException(status_code=404, detail="Artifact task not found")
    # Terminal states: drop from the registry, then project.
    pending.drop(notebook_id, task_id)
    if state == GenerationState.REMOVED:
        raise HTTPException(status_code=410, detail="Artifact was removed")
    if state == GenerationState.FAILED:
        raise HTTPException(
            status_code=409, detail=safe_detail(view.error) if view.error else "Generation failed"
        )
    # COMPLETED — and, defensively, any unmodeled state — surfaces the projected
    # view rather than a 500.
    return projected


@router.get("/{artifact_id}/prompt")
async def get_prompt(notebook_id: str, artifact_id: str, client: ClientDep) -> dict[str, Any]:
    """Fetch the free-text prompt an artifact was generated from.

    Returns ``{notebook_id, artifact_id, prompt}``. A ``null`` ``prompt`` (the
    artifact records none — e.g. a note-backed mind map) is a valid 200 result,
    NOT a 404; an unknown artifact id raises ``ArtifactNotFoundError`` → 404.
    """
    prompt = await artifact_core.get_artifact_prompt(client, notebook_id, artifact_id)
    return {"notebook_id": notebook_id, "artifact_id": artifact_id, "prompt": prompt}


@router.patch("/{artifact_id}")
async def rename(
    notebook_id: str, artifact_id: str, body: ArtifactRename, client: ClientDep
) -> dict[str, Any]:
    """Rename an artifact (title only), dispatching mind maps kind-aware."""
    result = await artifact_core.rename_artifact(
        client, notebook_id, _canonical_artifact_id(artifact_id), body.title
    )
    return {
        "status": "renamed",
        "notebook_id": notebook_id,
        "artifact_id": result.artifact_id,
        "new_title": result.new_title,
        "is_mind_map": result.is_mind_map,
    }


@router.post("/{artifact_id}/retry")
async def retry(
    notebook_id: str, artifact_id: str, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Retry a failed artifact in place (the UI "Retry" action).

    Non-blocking: on acceptance returns the kicked-off ``task_id`` (equal to the
    artifact id) and the new ``status``; poll ``GET .../artifacts/{task_id}``
    until complete. A synchronous refusal (rate limit / quota / not-retryable)
    surfaces as the classified error.

    The ``task_id`` is recorded in the pending registry (as ``generate`` does) so a
    poll that briefly races ahead of the artifact listing resolves to ``200``
    pending instead of a spurious ``404``.
    """
    status = await artifact_core.retry_artifact(client, notebook_id, artifact_id)
    pending.record(notebook_id, status.task_id)
    return {
        "notebook_id": notebook_id,
        "artifact_id": artifact_id,
        "task_id": status.task_id,
        # ``GenerationStatus.status`` is raw-string-permissive (documented in
        # ``_types/artifacts.py``: an instance built with a plain ``str`` keeps
        # working), so ``.value`` is NOT guaranteed — emit it enum-or-str-safely.
        "status": to_jsonable(status.status),
    }


@router.delete("/{artifact_id}", status_code=204)
async def delete(notebook_id: str, artifact_id: str, client: ClientDep) -> Response:
    """Delete an artifact (irreversible).

    The bare DELETE verb is the destructive gate (consistent with the notebook /
    source / note DELETE routes — no confirm param). A note-backed mind map is
    cleared via the note system inside the shared core. Returns 204.
    """
    await artifact_core.delete_artifact(client, notebook_id, _canonical_artifact_id(artifact_id))
    return Response(status_code=204)


class _CleanupFileResponse(FileResponse):
    """A ``FileResponse`` that removes its private temp dir once the body has
    finished streaming — or the client disconnected / the stream aborted.

    Cleaning in a ``finally`` around the ASGI ``__call__`` (not via a
    ``BackgroundTask``, which Starlette drops when the client disconnects
    mid-stream) guarantees the spooled artifact's temp dir is always removed, so a
    disconnect can never leak it. Error / early-return paths never construct this
    response — the route's own ``finally`` cleans those.
    """

    def __init__(self, *args: Any, temp_dir: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._temp_dir = temp_dir

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _cleanup(self._temp_dir)


@router.post("/download")
async def download(notebook_id: str, body: ArtifactDownload, client: ClientDep) -> FileResponse:
    """Download a completed artifact, streaming from a server-generated temp path."""
    spec = DOWNLOAD_SPECS.get(body.type)
    if spec is None:
        raise ValidationError(
            f"Unknown download type {body.type!r}; expected one of {sorted(DOWNLOAD_SPECS)}"
        )
    # Download into a private 0700 directory we own. mkstemp would pre-create the
    # file, which the download core treats as a conflict and may auto-rename
    # (download.py); an isolated empty dir avoids that, and we assert the served
    # path stays inside it so a surprising resolved path can never be streamed.
    temp_dir = tempfile.mkdtemp(prefix="nblm-download-")
    # On SUCCESS, ownership of the temp dir passes to the returned
    # ``_CleanupFileResponse`` (cleans after streaming, disconnect-safe); every
    # other exit — validation, a not-ready 409, an unexpected raise — cleans in the
    # ``finally`` below.
    success = False
    try:
        temp_path = os.path.join(temp_dir, f"artifact{spec.extension}")
        args: dict[str, Any] = {
            "notebook_id": notebook_id,
            "output_path": temp_path,
            "latest": True,
        }
        if body.output_format is not None:
            if not spec.format_choices:
                raise ValidationError(
                    f"type {body.type!r} does not support an output_format option"
                )
            args[spec.format_param_name] = body.output_format
        plan = download_core.build_download_plan(spec, args, cwd=Path.cwd())
        result = await download_core.execute_download(
            plan,
            client,
            notebook_resolver=passthrough_download_notebook,
            artifact_resolver=passthrough_artifact_id,
        )

        # No completed artifact of this kind exists yet (not ready), or a
        # pre-download error — surface as 409, not 500.
        if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
            detail = (
                safe_detail(result.error)
                if result.error
                else (f"No completed {body.type} artifact is available yet")
            )
            raise HTTPException(status_code=409, detail=detail)

        # Stream the actual written file. The core may resolve a conflict to a
        # different name, but it must stay inside our private dir — anything else is
        # a bug, not a file we serve.
        served = result.output_path or temp_path
        if Path(temp_dir).resolve() not in Path(served).resolve().parents:
            raise ValidationError("Download produced an unexpected output path")
        response = _CleanupFileResponse(
            served, filename=os.path.basename(served), temp_dir=temp_dir
        )
        success = True
        return response
    finally:
        if not success:
            _cleanup(temp_dir)


def _cleanup(path: str) -> None:
    """Remove a temp file or directory tree, ignoring an already-removed path."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except FileNotFoundError:  # pragma: no cover - already gone
        pass


def _generation_payload(
    notebook_id: str,
    result: generate_core.GenerationExecutionResult,
    pending: PendingRegistry,
) -> dict[str, Any]:
    """Project a generation result and record its ``task_id`` in the registry."""
    payload: dict[str, Any] = {"notebook_id": notebook_id, "kind": result.kind}
    if result.mind_map is not None:
        # Mind-map generation renders synchronously (no task_id to poll).
        payload["mind_map"] = to_jsonable(result.mind_map)
        return payload
    outcome = result.generation
    if outcome is not None:
        if outcome.task_id:
            pending.record(notebook_id, outcome.task_id)
        payload.update(
            {
                "task_id": outcome.task_id,
                "status": outcome.status,
                "url": outcome.url,
                "error": outcome.error,
            }
        )
    return payload
