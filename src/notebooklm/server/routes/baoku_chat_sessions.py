from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...client import NotebookLMClient
from .._context import get_client
from ..auth_deps import get_current_user
from ..database import get_session
from ..models import ChatMessage, ChatSession, Notebook, User

router = APIRouter(prefix="/notebooks/{notebook_id}/chat", tags=["chat-sessions"])


class SessionCreate(BaseModel):
    title: str | None = None


class MessageCreate(BaseModel):
    content: str
    source_ids: list[str] | None = None


def _get_notebook_db_id(db: Session, notebook_id: str) -> int | None:
    row = db.query(Notebook).filter(Notebook.remote_id == notebook_id).first()
    return row.id if row else None


@router.get("/sessions")
def list_sessions(
    notebook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        return {"items": [], "total": 0}
    rows = (
        db.query(ChatSession)
        .filter(ChatSession.notebook_id == db_id, ChatSession.user_id == user.id)
        .order_by(ChatSession.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "message_count": r.message_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/sessions", status_code=201)
def create_session(
    notebook_id: str,
    body: SessionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    record = ChatSession(
        user_id=user.id,
        notebook_id=db_id,
        title=body.title,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "message_count": 0,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    notebook_id: str,
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    record = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Session not found")
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.delete(record)
    db.commit()


@router.get("/sessions/{session_id}/messages")
def list_messages(
    notebook_id: str,
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Session not found")
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "citations": json.loads(r.citations) if r.citations else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/sessions/{session_id}/messages", status_code=201)
async def create_message(
    notebook_id: str,
    session_id: int,
    body: MessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    client: NotebookLMClient = Depends(get_client),
) -> dict[str, Any]:
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Session not found")

    user_msg = ChatMessage(
        session_id=session_id,
        user_id=user.id,
        role="user",
        content=body.content,
    )
    db.add(user_msg)
    session.message_count = (session.message_count or 0) + 1

    result = await client.chat.ask(
        notebook_id,
        body.content,
        source_ids=body.source_ids,
    )

    citations_json = json.dumps(
        [{"source_id": r.source_id, "source_name": r.title} for r in (result.references or [])]
    )

    assistant_msg = ChatMessage(
        session_id=session_id,
        user_id=user.id,
        role="assistant",
        content=result.answer,
        citations=citations_json,
    )
    db.add(assistant_msg)
    session.message_count = (session.message_count or 0) + 1
    db.commit()
    db.refresh(assistant_msg)

    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": assistant_msg.content,
        "citations": json.loads(assistant_msg.citations) if assistant_msg.citations else None,
        "created_at": assistant_msg.created_at.isoformat() if assistant_msg.created_at else None,
    }
