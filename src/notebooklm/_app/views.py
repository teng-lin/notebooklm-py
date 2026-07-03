"""Transport-neutral output-projection views (enum→label, debug-blob strip).

Where a wire result would otherwise leak raw protocol integers or a debug blob,
these helpers project the typed domain object into an agent-readable dict. They
are the single home for projections the MCP tools historically hand-rolled, so
the CLI/MCP/REST adapters all emit the SAME enriched shape (Option B — lift the
projection into ``_app`` rather than copy it per adapter):

* :func:`share_status_view` — ``ShareStatus`` with string-labeled access /
  permission / view_level enums (a raw :func:`to_jsonable` pass would leak
  ``access=1`` etc.).
* :func:`source_view` — a ``Source`` with string ``kind`` / ``status_label``
  labels added alongside the raw ``status`` / ``_type_code`` integers.
* :func:`ask_result_view` — an ``AskResult`` serialized with the internal
  ``raw_response`` debugging blob stripped (it just burns agent context; the
  field stays on the dataclass, it is only omitted from the wire).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..types import source_status_to_str
from .serialize import to_jsonable

if TYPE_CHECKING:
    from ..types import AskResult, ShareStatus, Source

__all__ = [
    "ACCESS_LABELS",
    "PERMISSION_LABELS",
    "VIEW_LEVEL_LABELS",
    "ask_result_view",
    "label",
    "share_status_view",
    "source_view",
]

#: ``int, Enum`` value → wire label for the three share enums. Unknown values
#: (e.g. ``SharePermission._REMOVE`` or protocol drift) degrade to ``str(value)``
#: via :func:`label` so the projection never KeyErrors.
ACCESS_LABELS = {0: "restricted", 1: "anyone_with_link"}
VIEW_LEVEL_LABELS = {0: "full", 1: "chat"}
PERMISSION_LABELS = {1: "owner", 2: "editor", 3: "viewer"}


def label(mapping: dict[int, str], value: int) -> str:
    """Map an ``int, Enum`` member (or raw int) to its label; unknown → ``str``."""
    key = int(value)
    return mapping.get(key, str(key))


def share_status_view(status: ShareStatus, *, include_view_level: bool = False) -> dict[str, Any]:
    """Project ``ShareStatus`` to a wire dict with string-labeled enums.

    ``view_level`` is included ONLY when ``include_view_level`` is set. The
    ``GET_SHARE_STATUS`` read does not report it (``ShareStatus.from_api_response``
    hardcodes ``FULL_NOTEBOOK``), so shipping it from a read would be
    confidently-wrong data; the only trustworthy source is ``set_view_level``'s
    own return, so the caller opts in on that path alone.
    """
    payload: dict[str, Any] = {
        "notebook_id": status.notebook_id,
        "is_public": status.is_public,
        "access": label(ACCESS_LABELS, status.access),
        "share_url": status.share_url,
        "shared_users": [
            {
                "email": user.email,
                "permission": label(PERMISSION_LABELS, user.permission),
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
            }
            for user in status.shared_users
        ],
    }
    if include_view_level:
        payload["view_level"] = label(VIEW_LEVEL_LABELS, status.view_level)
    return payload


def source_view(source: Source) -> dict[str, Any]:
    """Serialize a ``Source`` with agent-readable string labels added.

    ``to_jsonable`` emits only dataclass fields, so the integer ``status`` and
    ``_type_code`` arrive as bare numbers and the ``kind`` *property* is dropped —
    forcing an agent to guess what ``3``/``5``/``2`` mean. Add ``kind`` (e.g.
    ``"pdf"``/``"web_page"``) and ``status_label`` (e.g. ``"ready"``/``"error"``)
    string labels alongside the raw codes.

    ``status_label`` comes from :func:`~notebooklm.rpc.types.source_status_to_str`
    — the repo's single source of truth for status→string — so every adapter's
    label stays in lock-step. It is one of ``ready``/``processing``/``error``/
    ``preparing`` (``unknown`` for an unrecognized code).
    """
    view = to_jsonable(source)
    view["kind"] = source.kind.value
    view["status_label"] = source_status_to_str(source.status)
    return view


def ask_result_view(result: AskResult) -> dict[str, Any]:
    """Serialize an ``AskResult``, dropping the internal ``raw_response`` blob.

    ``raw_response`` is a debug-only truncated wire dump that just burns agent
    context; it is omitted from the wire on every adapter (the field stays on the
    dataclass for local debugging). Callers that want a trimmed ``references``
    projection apply it on top of this base dict.
    """
    payload = to_jsonable(result)
    payload.pop("raw_response", None)
    return payload
