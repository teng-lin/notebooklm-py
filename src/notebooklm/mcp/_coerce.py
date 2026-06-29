"""Tolerant coercion of list-shaped MCP tool params.

Real MCP clients and some LLM tool-callers send a "list of strings" param in
several shapes: a real list/tuple, a JSON-array string ``'["a","b"]'``, a
comma-separated string ``"a,b"``, or a bare scalar. Tools that type such a param
as ``list[str]`` and trust the schema reject the non-list shapes at the Pydantic
boundary. :func:`coerce_list` normalizes all of them to a ``list[str]`` so a tool
can accept the param typed ``list[str] | str | None`` and pre-parse it.

``None`` is preserved as ``None`` (callers treat ``None`` as "unset" — for
``source_ids`` that means "all sources", which is DISTINCT from an explicit empty
list meaning "zero sources"; see the source-ids contract in ``tools/artifacts.py``
and #1652). An empty/whitespace-only string yields ``[]``.

This module imports NO ``click`` / ``rich`` / ``cli`` — only stdlib ``json``.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["coerce_list"]


def coerce_list(value: Any) -> list[str] | None:
    """Normalize a list-shaped value into a ``list[str]`` (or ``None``).

    Accepts a real ``list``/``tuple``, a JSON-array string, a comma-separated
    string, or a bare scalar:

    * ``None`` -> ``None`` (preserves the "unset"/"all" contract — NOT ``[]``).
    * ``list`` / ``tuple`` -> each element stringified.
    * a JSON-array string (``'["a","b"]'``) -> the parsed list, stringified. If
      the string starts with ``[`` but is not valid JSON or not a JSON list, the
      leading ``[`` (and a trailing ``]`` if present) is stripped — handling the
      bracketed-but-unquoted ``"[a, b]"`` / unbalanced ``"[a,b"`` some clients
      emit — and it falls back to comma-splitting.
    * any other string -> split on commas, stripping whitespace and dropping
      empty segments (so ``""`` / ``"   "`` -> ``[]``, ``"a"`` -> ``["a"]``).
    * a bare non-string scalar (e.g. ``5``) -> ``["5"]`` (defensive: a non-string
      scalar is rejected by Pydantic before reaching a tool typed
      ``list[str] | str | None``, so this branch only matters for direct calls).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            # Not valid JSON (or not a JSON list) — e.g. the bracketed-but-unquoted
            # "[a, b]" some clients/LLMs emit. A leading "[" is never a valid source
            # ref, so strip it (and a matching trailing "]") before the comma-split
            # below — yielding clean items ("a"/"b") instead of "[a"/"b]". This also
            # covers the unbalanced "[a,b" case so a stray "[" can't leak into a
            # resolved id and surface as a confusing "not found".
            text = text.removeprefix("[").removesuffix("]")
        return [part.strip() for part in text.split(",") if part.strip()]
    # bare non-string scalar (int, etc.)
    return [str(value)]
