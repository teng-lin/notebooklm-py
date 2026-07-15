from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..auth_deps import ALGORITHM, SECRET_KEY
from ..database import get_session
from ..models import RequestLog as RequestLogModel

logger = logging.getLogger(__name__)

MAX_BODY_LOG_BYTES = 100 * 1024
CLEANUP_LOG_DAYS = 90


def _truncate(body: bytes) -> str:
    if len(body) > MAX_BODY_LOG_BYTES:
        return body[:MAX_BODY_LOG_BYTES].decode("utf-8", errors="replace") + "... (truncated)"
    return body.decode("utf-8", errors="replace")


def _extract_user_id(request: Request) -> int | None:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :].strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload.get("sub", 0)) or None
    except (JWTError, ValueError, TypeError):
        return None


class RequestLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        start = time.monotonic()
        user_id = _extract_user_id(request)
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent", "")
        method = request.method
        path = request.url.path
        endpoint = path

        try:
            body_bytes = await request.body()
        except Exception:
            body_bytes = b""

        response = await call_next(request)
        latency_ms = int((time.monotonic() - start) * 1000)

        response_body_bytes = b""
        if hasattr(response, "body_iterator"):
            chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            response_body_bytes = b"".join(chunks)
            response = Response(
                content=response_body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        try:
            db = get_session()
            log_entry = RequestLogModel(
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                request_headers=json.dumps(dict(request.headers)),
                request_body=_truncate(body_bytes),
                response_status=response.status_code,
                response_headers=json.dumps(dict(response.headers)),
                response_body=_truncate(response_body_bytes),
                latency_ms=latency_ms,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            db.add(log_entry)
            db.commit()
        except Exception as exc:
            logger.warning("Failed to log request: %s", exc)

        return response


async def cleanup_old_logs() -> int:
    from datetime import datetime, timedelta

    try:
        db = get_session()
        cutoff = datetime.utcnow() - timedelta(days=CLEANUP_LOG_DAYS)
        deleted = db.query(RequestLogModel).filter(RequestLogModel.created_at < cutoff).delete()
        db.commit()
        return deleted
    except Exception as exc:
        logger.warning("Failed to cleanup old logs: %s", exc)
        return 0
