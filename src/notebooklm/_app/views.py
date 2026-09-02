"""Transport-neutral output-projection views (enum→label, debug-blob strip).

Where a wire result would otherwise leak raw protocol integers or a debug blob,
these helpers project the typed domain object into an agent-readable dict. They
are the single home for projections the MCP tools historically hand-rolled, so
the CLI/MCP/REST adapters all emit the SAME enriched shape (Option B — lift the
projection into ``_app`` rather than copy it per adapter):

* :func:`share_status_view` — ``ShareStatus`` with string-labeled access /
  permission / view_level enums (a raw :func:`to_jsonable` pass would leak
  ``access=1`` etc.).
* :func:`source_view` — a ``Source`` with string ``kind`` / ``status_label`` /
  ``drive_status_label`` labels and the ``is_drive_degraded`` verdict added
  alongside the raw ``status`` / ``_type_code`` / ``drive_status`` integers.
* :func:`notebook_view` — a ``Notebook`` with a string ``role_label`` added
  alongside the raw ``role`` integer.
* :func:`notebook_viewed_keys` — the ``last_viewed_at`` timestamp plus its
  deprecated ``modified_at`` alias key, for the hand-built CLI JSON envelopes
  that do not go through :func:`notebook_view`.
* :func:`ask_result_view` — an ``AskResult`` serialized with the internal
  ``raw_response`` debugging blob stripped (it just burns agent context; the
  field stays on the dataclass, it is only omitted from the wire).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..types import drive_source_status_to_str, share_permission_to_str, source_status_to_str
from .serialize import to_jsonable

if TYPE_CHECKING:
    from ..types import AskResult, Notebook, ShareStatus, Source

__all__ = [
    "ACCESS_LABELS",
    "PERMISSION_LABELS",
    "VIEW_LEVEL_LABELS",
    "ask_result_view",
    "label",
    "notebook_view",
    "notebook_viewed_keys",
    "share_status_view",
    "source_view",
]

#: ``int, Enum`` value → wire label for the three share enums. Unknown values
#: (e.g. ``SharePermission._REMOVE`` or protocol drift) degrade to ``str(value)``
#: via :func:`label` so the projection never KeyErrors.
ACCESS_LABELS = {0: "restricted", 1: "anyone_with_link"}
VIEW_LEVEL_LABELS = {0: "full", 1: "chat"}
PERMISSION_LABELS = {1: "owner", 2: "editor", 3: "viewer"}

# Stable source fields exposed by the MCP and REST adapters. ``Source`` also
# carries Python-only metadata that those established wire contracts do not
# publish. Keep this allowlist explicit so enriching the dataclass cannot widen
# every adapter response accidentally.
_SOURCE_VIEW_FIELDS = (
    "id",
    "title",
    "url",
    "_type_code",
    "created_at",
    "status",
    "drive_document_id",
    "drive_status",
    # Play Books provenance on an Expert-Intelligence source; ``None`` on every
    # other source, so this widens the schema only for that kind (#2292).
    "expert_intelligence",
)


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

    ``max_individuals_share_limit`` and ``is_public_sharing_allowed`` are
    emitted verbatim, including ``None`` (#2130). This projection is an explicit
    allowlist rather than a :func:`to_jsonable` pass, so a field added to the
    dataclass reaches no adapter until it is listed here. Both keys are always
    present — an omitted key and a ``null`` are the same thing to a JSON client,
    but a *stably* present key lets a consumer distinguish "this server does not
    report the field" from "this notebook has no value for it".

    ``is_public_sharing_denied`` carries the *verdict* over that tri-state, for
    the same reason :func:`source_view` ships ``is_drive_degraded``: it is a
    property, so a raw serialization pass would drop it, leaving every
    non-Python consumer to re-derive it — and the idiomatic re-derivation
    (``!allowed`` in JS, ``not allowed`` in Python) is *wrong*, because it also
    fires on the unknown case. Shipping the verdict is what keeps that bug out
    of consumers rather than only out of this repo.
    """
    payload: dict[str, Any] = {
        "notebook_id": status.notebook_id,
        "is_public": status.is_public,
        "access": label(ACCESS_LABELS, status.access),
        "share_url": status.share_url,
        "max_individuals_share_limit": status.max_individuals_share_limit,
        "is_public_sharing_allowed": status.is_public_sharing_allowed,
        "is_public_sharing_denied": status.is_public_sharing_denied,
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
    label stays in lock-step. It is one of ``unknown``/``processing``/``ready``/
    ``error``/``preparing``.

    ``drive_status_label`` does the same for the Drive-side health code
    (:attr:`~notebooklm.Source.drive_status`), and is ``None`` — not
    ``"unknown"`` — when the row carried no Drive status at all, so an agent
    can tell "not a Drive claim" from "a Drive code we could not read" (#2111).

    ``is_drive_degraded`` carries the *verdict* over those codes. It is a
    property, so :func:`to_jsonable` would drop it, leaving every non-Python
    consumer to re-derive the degraded set from the label string — exactly the
    per-adapter duplication this module exists to prevent.

    The base fields are an explicit allowlist. ``Source`` is also the public
    Python model and may gain local metadata without implicitly changing the
    established MCP/REST response schema.
    """
    serialized = to_jsonable(source)
    view = {key: serialized[key] for key in _SOURCE_VIEW_FIELDS if key in serialized}
    view["kind"] = source.kind.value
    view["status_label"] = source_status_to_str(source.status)
    view["drive_status_label"] = (
        drive_source_status_to_str(source.drive_status) if source.drive_status is not None else None
    )
    view["is_drive_degraded"] = source.is_drive_degraded
    return view


def notebook_view(notebook: Notebook) -> dict[str, Any]:
    """Serialize a ``Notebook`` with an agent-readable ``role_label`` added.

    ``role`` is the caller's own permission level on the notebook
    (``SharePermission``), so a raw :func:`to_jsonable` pass would leak
    ``role=3`` and force an agent to guess. ``role_label`` comes from
    :func:`~notebooklm.rpc.types.share_permission_to_str` — the repo's single
    source of truth for permission→string — and is one of ``owner``/``editor``/
    ``viewer``, or ``None`` when the row did not state a role.
    """
    # Stable MCP/REST contract: Notebook's richer Project-only fields
    # (emoji, premium capabilities, create-only chat sessions, chat settings)
    # are Python API data and must be opted into a transport deliberately.
    # Filtering a serialized dict retains datetime/enum normalization without
    # letting every future dataclass addition silently expand these APIs.
    serialized = to_jsonable(notebook)
    view = {
        key: serialized[key]
        for key in (
            "id",
            "title",
            "created_at",
            "sources_count",
            "is_owner",
            "modified_at",
            "role",
            "last_viewed_at",
        )
        if key in serialized
    }
    view["role_label"] = (
        share_permission_to_str(notebook.role) if notebook.role is not None else None
    )
    return view


def notebook_viewed_keys(notebook: Notebook) -> dict[str, str | None]:
    """Return ``{"last_viewed_at": iso, "modified_at": iso}`` for a notebook.

    ``modified_at`` is the deprecated alias of ``last_viewed_at`` (#2126): the
    wire slot is ``lastViewedTime``, not a modification time. Both keys carry
    the same ISO-8601 value so no consumer of the old key breaks during the v1.0
    runway, and both are read from ``last_viewed_at`` so nothing here depends on
    the alias staying in sync.

    :func:`notebook_view` already emits both (they are dataclass fields, so
    :func:`to_jsonable` picks them up). This helper exists for the hand-built
    CLI ``--json`` envelopes, which pick individual keys rather than serializing
    the whole dataclass — it keeps the alias policy in one place instead of
    three copies of the same conditional.
    """
    viewed = notebook.last_viewed_at.isoformat() if notebook.last_viewed_at else None
    return {"last_viewed_at": viewed, "modified_at": viewed}


def ask_result_view(result: AskResult) -> dict[str, Any]:
    """Serialize an ``AskResult``, dropping the two context-burning blobs.

    ``raw_response`` is a debug-only truncated wire dump; ``answer_document``
    (#2120) restates the answer as a block tree these agent-facing surfaces do
    not consume and would roughly double every payload. Both are omitted from
    the wire on every adapter — the fields stay on the dataclass for local use.
    The answer-side citation offsets survive the trim: they ride on each
    ``ChatReference`` as ``answer_anchor_start`` / ``answer_anchor_end``.
    Callers that want a trimmed ``references`` projection apply it on top of
    this base dict.
    """
    payload = to_jsonable(result)
    payload.pop("raw_response", None)
    payload.pop("answer_document", None)
    return payload
