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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..._app import artifacts as artifact_core
from ..._app import download as download_core
from ..._app import generate as generate_core
from ..._app.language import is_supported_language
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from ...exceptions import ValidationError
from ...types import ArtifactType, GenerationState
from .._context import get_client, get_pending
from .._errors import safe_detail
from .._pending import PendingRegistry
from ._passthrough import (
    passthrough_artifact_id,
    passthrough_download_notebook,
    passthrough_notebook_id,
    passthrough_source_ids,
)

__all__ = ["DOWNLOAD_SPECS", "GENERATE_TYPES", "router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/artifacts", tags=["artifacts"])

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

#: Accepted values for the caller-facing per-kind options, validated up front so
#: a bad choice is a clean 400 rather than a raw ``KeyError`` from a generate-core
#: display-name lookup that runs before its own choice validation.
_OPTION_CHOICES: dict[str, tuple[str, ...]] = {
    "report_format": ("briefing-doc", "study-guide", "blog-post", "custom"),
    "audio_format": ("deep-dive", "brief", "critique", "debate"),
    "audio_length": ("short", "default", "long"),
    "quantity": ("fewer", "standard", "more"),
    "difficulty": ("easy", "medium", "hard"),
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
    """Request body for starting a studio-artifact generation."""

    type: str
    source_ids: list[str] | None = None
    instructions: str = ""
    language: str | None = None
    report_format: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None
    quantity: str | None = None
    difficulty: str | None = None


class ArtifactDownload(BaseModel):
    """Request body for downloading a generated artifact."""

    type: str
    output_format: str | None = None


@router.get("")
async def list_artifacts(notebook_id: str, client: ClientDep) -> dict[str, Any]:
    """List a notebook's studio artifacts."""
    artifacts = await client.artifacts.list(notebook_id)
    return {"notebook_id": notebook_id, "artifacts": to_jsonable(artifacts)}


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

    raw_args: dict[str, Any] = dict(_KIND_DEFAULTS[body.type])
    raw_args.update(
        {
            "notebook_id": notebook_id,
            "description": body.instructions or "",
            "source_ids": tuple(body.source_ids or ()),
            "language": body.language,
            "wait": False,
            "json_output": True,
        }
    )
    for key, value in (
        ("report_format", body.report_format),
        ("audio_format", body.audio_format),
        ("audio_length", body.audio_length),
        ("quantity", body.quantity),
        ("difficulty", body.difficulty),
    ):
        if value is not None:
            choices = _OPTION_CHOICES[key]
            if value not in choices:
                raise ValidationError(f"Invalid {key} {value!r}; expected one of {list(choices)}")
            raw_args[key] = value

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
    """Poll a generation task, projecting state through the pending registry."""
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
    temp_path = os.path.join(temp_dir, f"artifact{spec.extension}")
    try:
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
    except BaseException:
        _cleanup(temp_dir)
        raise

    # No completed artifact of this kind exists yet (not ready), or a pre-download
    # error — surface as 409, not 500, and clean up the unused temp dir.
    if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED:
        _cleanup(temp_dir)
        detail = (
            safe_detail(result.error)
            if result.error
            else (f"No completed {body.type} artifact is available yet")
        )
        raise HTTPException(status_code=409, detail=detail)

    # Stream the actual written file. The core may resolve a conflict to a
    # different name, but it must stay inside our private dir — anything else is a
    # bug, not a file we serve.
    served = result.output_path or temp_path
    if Path(temp_dir).resolve() not in Path(served).resolve().parents:
        _cleanup(temp_dir)
        raise ValidationError("Download produced an unexpected output path")
    return FileResponse(
        served,
        filename=os.path.basename(served),
        background=BackgroundTask(_cleanup, temp_dir),
    )


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
