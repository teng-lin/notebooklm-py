"""Guard: the chat domain package encodes no ``batchexecute`` wire vocabulary.

P10 R2.1 moved the streamed-ask encoder out of ``_chat/stream_request.py`` and
into ``_web/codec/chat_stream.py`` (``encode_ask_stream``). The thing that made
that module a boundary violation was not a *decode* — it was the encode side:
``rpc.encoder.nest_source_ids`` for the source-id nesting and
``rpc.types.get_query_url`` for the endpoint, both named directly from a domain
package.

Neither existing gate covers it:

* ``tests/_guardrails/test_wire_contract.py`` scans ``_row_adapters/`` plus three
  named files. It does **not** scan ``_chat/``.
* ``tests/_guardrails/test_no_raw_positional_rpc_indexing.py`` does reach
  ``_chat/``, but the ask grammar was list *construction*
  (``params = [sources_array, question, ...]``), never an index read, so an
  index-shaped scan would not have caught it.

So the ban is written here, as its own AST gate over the whole package: no
module under ``src/notebooklm/_chat/`` may import ``rpc.encoder``,
``nest_source_ids``, or ``get_query_url``, by any spelling. The offender set is
empty after R2.1 and must stay empty — a domain module that needs an encoded
request asks a codec for one.

``rpc.decoder``/``rpc.types`` are deliberately NOT banned wholesale: ``RPCMethod``
constants and ``safe_index`` drift diagnostics still legitimately appear in
decode-adjacent chat code, and separating those is the job of
``test_no_rpc_method_below_port.py``, not of this gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_SRC = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
_CHAT = _SRC / "_chat"

#: Modules no ``_chat/`` module may import, by absolute or relative spelling.
BANNED_MODULES = frozenset({"rpc.encoder", "notebooklm.rpc.encoder"})

#: Names no ``_chat/`` module may import from anywhere.
BANNED_NAMES = frozenset({"nest_source_ids", "get_query_url"})


def _resolve(module: str | None, level: int, package_depth: int) -> str:
    """Absolute dotted name for an import inside ``notebooklm/_chat/``.

    ``package_depth`` is how many package components sit above the module
    (``notebooklm._chat`` == 2), so ``level`` 1 stays in ``_chat`` and 2 reaches
    ``notebooklm``. A deeper level cannot name a first-party module.
    """
    if not level:
        return module or ""
    parts = ("notebooklm", "_chat")[: package_depth - level + 1]
    prefix = ".".join(parts)
    return f"{prefix}.{module}" if module else prefix


def chat_wire_encoding_imports() -> set[str]:
    """Every ``_chat/`` import of a banned encoder module or name."""
    offenders: set[str] = set()
    for path in sorted(_CHAT.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in BANNED_MODULES:
                        offenders.add(f"{relative}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve(node.module, node.level, package_depth=2)
                suffix = resolved.removeprefix("notebooklm.")
                banned_module = suffix in BANNED_MODULES or resolved in BANNED_MODULES
                for alias in node.names:
                    if banned_module or alias.name in BANNED_NAMES:
                        offenders.add(f"{relative}: from {resolved} import {alias.name}")
    return offenders


def test_chat_package_imports_no_request_encoder() -> None:
    """No module under ``_chat/`` names the RPC request encoder."""
    offenders = chat_wire_encoding_imports()
    assert offenders == set(), (
        "chat domain modules must not encode wire requests; ask a "
        "`_web/codec` encoder instead:\n  " + "\n  ".join(sorted(offenders))
    )


def test_gate_scans_a_non_empty_chat_package() -> None:
    """The scan really reaches ``_chat/`` — the ban above is not vacuous."""
    assert sorted(path.name for path in _CHAT.rglob("*.py"))


def test_detector_flags_every_banned_spelling() -> None:
    """The AST detector catches absolute, relative and name-only spellings."""
    source = (
        "import notebooklm.rpc.encoder\n"
        "from ..rpc.encoder import nest_source_ids\n"
        "from ..rpc.types import get_query_url\n"
        "from notebooklm.rpc.encoder import build_body\n"
        "from ..rpc.types import RPCMethod\n"
    )
    tree = ast.parse(source)
    flagged: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            flagged += [a.name for a in node.names if a.name in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve(node.module, node.level, package_depth=2)
            suffix = resolved.removeprefix("notebooklm.")
            for alias in node.names:
                if suffix in BANNED_MODULES or alias.name in BANNED_NAMES:
                    flagged.append(alias.name)
    assert flagged == [
        "notebooklm.rpc.encoder",
        "nest_source_ids",
        "get_query_url",
        "build_body",
    ]
