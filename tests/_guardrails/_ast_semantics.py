"""Cross-version semantic AST hashing for executable boundary guards."""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Any


def _canonical_ast(value: Any) -> object:
    if isinstance(value, ast.AST):
        fields: list[object] = []
        for name, child in ast.iter_fields(value):
            # Python minors add fields such as ``type_params`` and
            # ``posonlyargs``. An absent field and its empty default have the
            # same semantics, so omit empty defaults from the digest. 3.15
            # adds ``is_lazy`` (PEP 810) to ``Import``/``ImportFrom``; it is 0
            # for every ordinary import.
            if child is None or child == [] or (name == "is_lazy" and not child):
                continue
            fields.append([name, _canonical_ast(child)])
        return [type(value).__name__, fields]
    if isinstance(value, list | tuple):
        return [_canonical_ast(item) for item in value]
    return [type(value).__name__, repr(value)]


def semantic_hash(node: ast.AST) -> str:
    """Hash an AST without interpreter-version-only empty fields."""
    payload = json.dumps(_canonical_ast(node), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()
