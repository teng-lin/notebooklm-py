from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth_deps import get_current_user
from ..database import get_session
from ..models import Note as NoteModel
from ..models import Notebook, User

router = APIRouter(prefix="/notebooks/{notebook_id}/notes", tags=["notes"])


class NoteCreate(BaseModel):
    title: str | None = None
    content: str = ""


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


def _get_notebook_db_id(db: Session, notebook_id: str) -> int | None:
    row = db.query(Notebook).filter(Notebook.remote_id == notebook_id).first()
    return row.id if row else None


@router.get("")
def list_notes(
    notebook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    rows = (
        db.query(NoteModel)
        .filter(NoteModel.notebook_id == db_id, NoteModel.user_id == user.id)
        .order_by(NoteModel.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("", status_code=201)
def create_note(
    notebook_id: str,
    body: NoteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    record = NoteModel(
        user_id=user.id,
        notebook_id=db_id,
        title=body.title,
        content=body.content,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.put("/{note_id}")
def update_note(
    notebook_id: str,
    note_id: int,
    body: NoteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    record = (
        db.query(NoteModel)
        .filter(NoteModel.id == note_id, NoteModel.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Note not found")
    if body.title is not None:
        record.title = body.title
    if body.content is not None:
        record.content = body.content
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.delete("/{note_id}", status_code=204)
def delete_note(
    notebook_id: str,
    note_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    record = (
        db.query(NoteModel)
        .filter(NoteModel.id == note_id, NoteModel.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Note not found")
    db.delete(record)
    db.commit()
