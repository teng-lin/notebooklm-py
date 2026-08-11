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
            # same semantics, so omit empty defaults from the digest.
            if child is None or child == []:
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
