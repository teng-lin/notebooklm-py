"""Pure-value type for a NotebookLM collection (account-level notebook group).

Re-exported from ``notebooklm.types``. A ``Collection`` groups whole notebooks
(account-level, playlist-style) — the wire tuple is ``[name, member_notebook_ids,
collection_id, emoji]``, shaped like a source ``Label`` (``[name, sources, id,
emoji]``) only while **empty**. Once populated the member slot carries *bare*
notebook-id strings (``["nb_id", ...]``), not the label's wrapped singletons
(``[["src_id"], ...]``) — confirmed live on PR #2009 — so decoding is positional
here rather than reusing :class:`LabelRow`, whose strict decoder rejects the
bare form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Collection:
    """A NotebookLM collection (a named, emoji-labeled group of notebooks).

    Account-level (unlike a notebook-scoped :class:`~notebooklm.types.Label`):
    it carries no notebook parent. Membership is many-to-many — a notebook may
    belong to multiple collections, and a collection owns a list of notebook IDs
    (the notebook carries no back-reference). Nesting and sharing are not
    supported by the service.
    """

    id: str
    name: str
    emoji: str | None = None
    # Member notebook UUIDs. Empty for a freshly-created (still empty) collection.
    notebook_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, data: list[Any], *, method_id: str | None = None) -> Collection:
        """Parse one collection 4-tuple ``[name, notebook_ids, collection_id, emoji]``.

        Decoded positionally rather than via :class:`LabelRow` because the
        member slot's shape differs from a label's once populated: bare
        notebook-id strings, not ``[[source_id], ...]`` wrapped singletons.
        Strict per ADR-0019/0011 — checked row descent raises on failure, and
        any type drift (non-string name/id, a member that isn't a string, a
        non-string emoji) raises too; there is no degrade-to-sentinel path.
        """
        from .._web.rows.collections import decode_collection

        return decode_collection(cls, data, method_id=method_id)
