"""Request builder for the Web streamed-chat endpoint."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from ..._auth.account import format_authuser_value
from ..._env import get_default_bl, get_default_language
from ...rpc.types import get_query_url
from ..wire.encoder import nest_source_ids


class AuthSnapshotLike(Protocol):
    """Structural auth snapshot accepted by streamed-chat request builders."""

    @property
    def csrf_token(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def authuser(self) -> int: ...

    @property
    def account_email(self) -> str | None: ...


def build_streaming_chat_request(
    *,
    snapshot: AuthSnapshotLike,
    notebook_id: str,
    question: str,
    source_ids: list[str],
    conversation_history: list | None,
    conversation_id: str | None,
    reqid: int,
) -> tuple[str, str, dict[str, str]]:
    """Assemble ``(url, body, extra_headers)`` for one streamed-chat attempt.

    ``conversation_id=None`` tells the server to use the user's current
    conversation on this notebook, creating one if none exists. The
    server-recorded id is recovered separately by the semantic facade.
    """
    sources_array = nest_source_ids(source_ids, 2)

    params: list[Any] = [
        sources_array,
        question,
        conversation_history,
        [2, None, [1], [1]],
        conversation_id,
        None,
        None,
        notebook_id,
        1,
    ]

    params_json = json.dumps(params, separators=(",", ":"))
    f_req_json = json.dumps([None, params_json], separators=(",", ":"))
    encoded_req = quote(f_req_json, safe="")

    body_parts = [f"f.req={encoded_req}"]
    if snapshot.csrf_token:
        body_parts.append(f"at={quote(snapshot.csrf_token, safe='')}")
    body = "&".join(body_parts) + "&"

    url_params: dict[str, str] = {
        "bl": get_default_bl(),
        "hl": get_default_language(),
        "_reqid": str(reqid),
        "rt": "c",
    }
    if snapshot.session_id:
        url_params["f.sid"] = snapshot.session_id
    if snapshot.account_email or snapshot.authuser:
        url_params["authuser"] = format_authuser_value(
            snapshot.authuser,
            snapshot.account_email,
        )

    url = f"{get_query_url()}?{urlencode(url_params)}"
    return url, body, {}


__all__ = ["AuthSnapshotLike", "build_streaming_chat_request"]
