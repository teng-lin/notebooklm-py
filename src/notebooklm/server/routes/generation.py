from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth_deps import get_current_user
from ..database import get_session
from ..generation import engines  # noqa: F401 — registers all generators
from ..generation.registry import GeneratorRegistry
from ..models import GeneratedContent as GeneratedContentModel
from ..models import User

router = APIRouter(prefix="/api/generation", tags=["generation"])


def _run_async(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@router.post("/generate")
def generate_content(
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    content_type = body.get("content_type", "")
    notebook_id = body.get("notebook_id", "")
    prompt = body.get("prompt", "")
    template = body.get("template")
    options = body.get("options", {})

    if not all([content_type, notebook_id, prompt]):
        raise HTTPException(400, "content_type, notebook_id, and prompt are required")

    options.setdefault("title", body.get("title", ""))
    options.setdefault("user_id", user.id)

    generator = GeneratorRegistry.create(content_type)
    result = _run_async(
        generator.generate(
            notebook_id=notebook_id,
            prompt=prompt,
            template=template,
            options=options,
        )
    )

    record = GeneratedContentModel(
        user_id=user.id,
        notebook_id=int(notebook_id) if notebook_id.isdigit() else 0,
        content_type=content_type,
        title=result.title,
        prompt=prompt,
        status=result.status,
        local_file_path=result.local_file_path,
        file_size=result.file_size,
        content=result.content,
    )
    meta = result.metadata
    for k in (
        "ppt_page_count",
        "ppt_template",
        "ppt_json",
        "ppt_preview_images",
        "mindmap_data",
        "mindmap_layout",
        "infographic_template",
        "infographic_blocks",
        "audio_file_path",
        "duration_seconds",
        "audio_speakers",
        "audio_transcript",
        "video_file_path",
        "video_duration_seconds",
        "video_resolution",
        "video_scenes",
        "video_narration",
        "doc_page_count",
        "doc_sections",
        "doc_format",
    ):
        if k in meta:
            setattr(record, k, meta[k])

    db.add(record)
    db.commit()
    db.refresh(record)

    return {
        "id": record.id,
        "content_type": record.content_type,
        "title": record.title,
        "status": record.status,
        "local_file_path": record.local_file_path,
        "file_size": record.file_size,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.get("/list")
def list_generated_contents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    notebook_id: int | None = Query(None),
    content_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    q = db.query(GeneratedContentModel).filter(
        GeneratedContentModel.user_id == user.id,
    )
    if notebook_id is not None:
        q = q.filter(GeneratedContentModel.notebook_id == notebook_id)
    if content_type is not None:
        q = q.filter(GeneratedContentModel.content_type == content_type)
    total = q.count()
    rows = (
        q.order_by(GeneratedContentModel.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": [
            {
                "id": r.id,
                "content_type": r.content_type,
                "title": r.title,
                "status": r.status,
                "file_size": r.file_size,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{content_id}")
def get_generated_content(
    content_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    r = (
        db.query(GeneratedContentModel)
        .filter(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
        .first()
    )
    if not r:
        raise HTTPException(404, "Generated content not found")

    return {
        "id": r.id,
        "content_type": r.content_type,
        "title": r.title,
        "prompt": r.prompt,
        "status": r.status,
        "content": r.content,
        "local_file_path": r.local_file_path,
        "file_size": r.file_size,
        "thumbnail_path": r.thumbnail_path,
        "error_message": r.error_message,
        "metadata": {
            "ppt_page_count": r.ppt_page_count,
            "ppt_template": r.ppt_template,
            "ppt_json": r.ppt_json,
            "ppt_preview_images": r.ppt_preview_images,
            "mindmap_data": r.mindmap_data,
            "mindmap_layout": r.mindmap_layout,
            "infographic_template": r.infographic_template,
            "infographic_blocks": r.infographic_blocks,
            "audio_file_path": r.audio_file_path,
            "duration_seconds": r.duration_seconds,
            "audio_speakers": r.audio_speakers,
            "audio_transcript": r.audio_transcript,
            "video_file_path": r.video_file_path,
            "video_duration_seconds": r.video_duration_seconds,
            "video_resolution": r.video_resolution,
            "video_scenes": r.video_scenes,
            "video_narration": r.video_narration,
            "doc_page_count": r.doc_page_count,
            "doc_sections": r.doc_sections,
            "doc_format": r.doc_format,
        },
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.delete("/{content_id}", status_code=204)
def delete_generated_content(
    content_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    r = (
        db.query(GeneratedContentModel)
        .filter(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
        .first()
    )
    if not r:
        raise HTTPException(404, "Generated content not found")

    if r.local_file_path and os.path.exists(r.local_file_path):
        os.remove(r.local_file_path)

    db.delete(r)
    db.commit()


@router.get("/templates")
def list_templates(
    content_type: str | None = Query(None),
) -> list[dict[str, str]]:
    types: list[str] = [content_type] if content_type else GeneratorRegistry.list_types()
    result: list[dict[str, str]] = []
    for ct in types:
        try:
            gen = GeneratorRegistry.create(ct)
            templates = _run_async(gen.get_supported_templates())
            for t in templates:
                result.append(
                    {
                        "content_type": ct,
                        "name": t.name,
                        "label": t.label,
                        "description": t.description,
                    }
                )
        except ValueError:
            pass
    return result


@router.post("/{content_id}/regenerate")
def regenerate_content(
    content_id: int,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    record = (
        db.query(GeneratedContentModel)
        .filter(
            GeneratedContentModel.id == content_id,
            GeneratedContentModel.user_id == user.id,
        )
        .first()
    )
    if not record:
        raise HTTPException(404, "Generated content not found")

    new_prompt = body.get("prompt")
    if not new_prompt:
        raise HTTPException(400, "prompt is required")

    generator = GeneratorRegistry.create(record.content_type)
    gen_result = _run_async(
        generator.generate(
            notebook_id=str(record.notebook_id),
            prompt=new_prompt,
            template=record.ppt_template or body.get("template"),
            options={"title": record.title, "user_id": user.id},
        )
    )

    record.prompt = new_prompt
    record.status = gen_result.status
    record.content = gen_result.content
    record.local_file_path = gen_result.local_file_path
    record.file_size = gen_result.file_size
    db.commit()

    return {
        "id": record.id,
        "status": record.status,
        "local_file_path": record.local_file_path,
    }
