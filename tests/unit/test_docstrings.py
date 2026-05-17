"""Guards against `from_storage` docstring drift.

`NotebookLMClient.from_storage` is an async classmethod, so a correct
example must use ``async with await NotebookLMClient.from_storage(...)``.
A bare ``async with NotebookLMClient.from_storage(...)`` raises
``TypeError`` at runtime because the coroutine itself is not an async
context manager. This test parses the module/class docstrings in the
public package and asserts every example snippet stays well-formed.

Module docstrings in ``client.py`` and ``__init__.py`` historically
had this bug; this test exists so the bug cannot re-enter via copy-paste.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.client import NotebookLMClient

# Files whose docstrings should not contain the broken example. We parse
# each file's AST and walk every docstring (module + class + function).
DOCSTRING_TARGETS = [
    "src/notebooklm/__init__.py",
    "src/notebooklm/client.py",
    "src/notebooklm/_notebooks.py",
    "src/notebooklm/_sources.py",
    "src/notebooklm/_artifacts.py",
    "src/notebooklm/_chat.py",
    "src/notebooklm/_research.py",
    "src/notebooklm/_notes.py",
    "src/notebooklm/_settings.py",
    "src/notebooklm/_sharing.py",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iter_docstrings(tree: ast.AST):
    """Yield every docstring found in the AST."""
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield node, doc


@pytest.mark.parametrize("relpath", DOCSTRING_TARGETS)
def test_from_storage_examples_use_await(relpath: str) -> None:
    """No docstring example may show `async with` + `from_storage()` without `await`.

    The whole point of the audit: ``from_storage`` is async, so anything
    that opens it as an async context manager must await it first.
    """
    path = _repo_root() / relpath
    tree = ast.parse(path.read_text())

    offenders: list[tuple[str, int, str]] = []
    for node, doc in _iter_docstrings(tree):
        # ``node.lineno`` for a module is 1; for class/function it's the
        # def line. Either is good enough to point a human at the bug.
        for idx, line in enumerate(doc.splitlines(), start=1):
            if "from_storage(" in line and "async with" in line and "await" not in line:
                offenders.append((getattr(node, "name", "<module>"), idx, line.strip()))

    assert not offenders, (
        f"{relpath}: found `async with ...from_storage(...)` example(s) "
        f"missing `await`: {offenders}"
    )


@pytest.mark.parametrize("relpath", DOCSTRING_TARGETS)
def test_docstring_example_lines_parse(relpath: str) -> None:
    """Every example line that mentions `from_storage()` must be valid Python.

    We don't execute the snippets — we only parse them. This catches
    typos / unterminated strings / etc. so future drift breaks a test
    instead of silently shipping a broken example.
    """
    path = _repo_root() / relpath
    tree = ast.parse(path.read_text())

    for _node, doc in _iter_docstrings(tree):
        for raw in doc.splitlines():
            line = raw.strip()
            if "from_storage(" not in line:
                continue
            # Wrap the line inside an ``async def`` so constructs like
            # ``async with X as y:`` (only legal inside an async function)
            # parse. If the line is itself a compound-statement header
            # (ends with ``:``), give it a ``pass`` body so the wrapper
            # stays syntactically valid.
            if line.rstrip().endswith(":"):
                wrapped = f"async def _():\n    {line}\n        pass\n"
            else:
                wrapped = f"async def _():\n    {line}\n"
            try:
                ast.parse(wrapped)
            except SyntaxError as exc:  # pragma: no cover - failure path
                pytest.fail(f"{relpath}: example line {line!r} failed to parse: {exc}")


async def test_from_storage_smoke_constructs_client(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    """Smoke test: the documented example shape actually returns a client.

    Mirrors what the module/class docstrings advertise:

        async with await NotebookLMClient.from_storage(path=...) as client:
            ...

    We stop short of entering the context manager — we just assert that
    awaiting ``from_storage`` returns a ``NotebookLMClient`` instance.
    That is enough to prove the example shape compiles and runs.
    """
    storage_file = tmp_path / "storage_state.json"
    storage_state = {
        "cookies": [
            {"name": "SID", "value": "smoke_sid", "domain": ".google.com"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "smoke_1psidts",
                "domain": ".google.com",
            },
            {"name": "HSID", "value": "smoke_hsid", "domain": ".google.com"},
        ],
        "origins": [],
    }
    storage_file.write_text(json.dumps(storage_state))

    # ``from_storage`` performs a token fetch against notebooklm.google.com
    # during construction; serve a minimal stub so the call resolves
    # without touching the network.
    html = '"SNlM0e":"smoke_csrf" "FdrFJe":"smoke_session"'
    httpx_mock.add_response(
        url="https://notebooklm.google.com/",
        content=html.encode(),
    )

    client = await NotebookLMClient.from_storage(path=str(storage_file))

    assert isinstance(client, NotebookLMClient)
    # Sanity: the client should not be connected yet — from_storage just
    # constructs it. Entering the async-with would call __aenter__.
    assert client.is_connected is False
