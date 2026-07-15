from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth_deps import SECRET_KEY, get_current_user
from ..database import get_session
from ..external_kb.registry import ConnectorRegistry
from ..models import (
    ExternalImport,
    ExternalKBCollection,
    ExternalKBConnection,
    ExternalKBDocument,
    User,
)

router = APIRouter(prefix="/api/external-kb", tags=["external-kb"])


def _get_fernet_key() -> bytes:
    raw = SECRET_KEY
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _encrypt_credentials(raw: dict) -> str:
    return Fernet(_get_fernet_key()).encrypt(json.dumps(raw).encode()).decode()


def _decrypt_credentials(encrypted: str) -> dict:
    return json.loads(Fernet(_get_fernet_key()).decrypt(encrypted.encode()).decode())


@router.post("/connections")
def create_connection(
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    encrypted = _encrypt_credentials(body.get("auth_credentials", {}))
    conn = ExternalKBConnection(
        user_id=user.id,
        name=body["name"],
        provider_type=body["provider_type"],
        api_base_url=body["api_base_url"],
        auth_type=body.get("auth_type", "api_key"),
        auth_credentials=encrypted,
        extra_config=json.dumps(body.get("extra_config", {})),
        is_active=True,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return {
        "id": conn.id,
        "name": conn.name,
        "provider_type": conn.provider_type,
        "api_base_url": conn.api_base_url,
    }


@router.get("/connections")
def list_connections(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        db.query(ExternalKBConnection)
        .filter(
            ExternalKBConnection.user_id == user.id,
            ExternalKBConnection.is_active,
        )
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "provider_type": r.provider_type,
            "api_base_url": r.api_base_url,
            "auth_type": r.auth_type,
            "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.put("/connections/{connection_id}")
def update_connection(
    connection_id: int,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    conn = (
        db.query(ExternalKBConnection)
        .filter(
            ExternalKBConnection.id == connection_id,
            ExternalKBConnection.user_id == user.id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(404, "Connection not found")
    if "name" in body:
        conn.name = body["name"]
    if "api_base_url" in body:
        conn.api_base_url = body["api_base_url"]
    if "auth_type" in body:
        conn.auth_type = body["auth_type"]
    if "auth_credentials" in body:
        conn.auth_credentials = _encrypt_credentials(body["auth_credentials"])
    if "extra_config" in body:
        conn.extra_config = json.dumps(body["extra_config"])
    db.commit()
    return {"id": conn.id, "status": "updated"}


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    conn = (
        db.query(ExternalKBConnection)
        .filter(
            ExternalKBConnection.id == connection_id,
            ExternalKBConnection.user_id == user.id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(404, "Connection not found")
    conn.is_active = False
    db.commit()


@router.post("/connections/{connection_id}/test")
def test_connection(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    conn = (
        db.query(ExternalKBConnection)
        .filter(
            ExternalKBConnection.id == connection_id,
            ExternalKBConnection.user_id == user.id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(404, "Connection not found")
    config = {
        "api_base_url": conn.api_base_url,
        "auth_type": conn.auth_type,
        "auth_credentials": _decrypt_credentials(conn.auth_credentials),
    }
    try:
        import asyncio

        instance = ConnectorRegistry.create(conn.provider_type, config)
        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(instance.test_connection())
        finally:
            loop.close()
        return {"success": ok}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@router.get("/connections/{connection_id}/collections")
def list_collections(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        db.query(ExternalKBCollection)
        .filter(ExternalKBCollection.connection_id == connection_id)
        .all()
    )
    return [
        {
            "id": r.id,
            "remote_id": r.remote_id,
            "name": r.name,
            "description": r.description,
            "document_count": r.document_count,
        }
        for r in rows
    ]


@router.get("/connections/{connection_id}/collections/{collection_id}/documents")
def list_documents(
    connection_id: int,
    collection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    rows = (
        db.query(ExternalKBDocument)
        .filter(
            ExternalKBDocument.collection_id == collection_id,
            ExternalKBDocument.connection_id == connection_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "remote_id": r.remote_id,
                "title": r.title,
                "summary": r.summary,
                "file_type": r.file_type,
                "file_size": r.file_size,
            }
            for r in rows
        ],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


@router.get("/connections/{connection_id}/collections/{collection_id}/search")
def search_documents(
    connection_id: int,
    collection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    q: str = Query("", min_length=1),
) -> list[dict[str, Any]]:
    rows = (
        db.query(ExternalKBDocument)
        .filter(
            ExternalKBDocument.collection_id == collection_id,
            ExternalKBDocument.connection_id == connection_id,
            ExternalKBDocument.title.ilike(f"%{q}%"),
        )
        .all()
    )
    return [
        {
            "id": r.id,
            "remote_id": r.remote_id,
            "title": r.title,
            "summary": r.summary,
        }
        for r in rows
    ]


@router.post("/import")
def import_document(
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    connection_id = body.get("connection_id")
    document_id = body.get("document_id")
    target_notebook_id = body.get("target_notebook_id")
    if not all([connection_id, document_id, target_notebook_id]):
        raise HTTPException(400, "connection_id, document_id, and target_notebook_id are required")

    conn = (
        db.query(ExternalKBConnection)
        .filter(
            ExternalKBConnection.id == connection_id,
            ExternalKBConnection.user_id == user.id,
        )
        .first()
    )
    if not conn:
        raise HTTPException(404, "Connection not found")

    config = {
        "api_base_url": conn.api_base_url,
        "auth_type": conn.auth_type,
        "auth_credentials": _decrypt_credentials(conn.auth_credentials),
    }
    import asyncio

    instance = ConnectorRegistry.create(conn.provider_type, config)
    instance._current_user_id = user.id
    instance._connection_id = conn.id

    loop = asyncio.new_event_loop()
    try:
        import_result = loop.run_until_complete(
            instance.import_document(document_id, target_notebook_id)
        )
    finally:
        loop.close()

    return {
        "success": import_result.success,
        "local_source_id": import_result.local_source_id,
        "local_source_path": import_result.local_source_path,
        "error_message": import_result.error_message,
    }


@router.get("/imports")
def list_imports(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        db.query(ExternalImport)
        .filter(ExternalImport.user_id == user.id)
        .order_by(ExternalImport.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "connection_id": r.connection_id,
            "target_notebook_id": r.target_notebook_id,
            "status": r.status,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
