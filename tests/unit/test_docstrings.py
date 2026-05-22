"""Guards against `from_storage` docstring drift.

`NotebookLMClient.from_storage` returns an awaitable async-context-manager
wrapper. The canonical idiom is bare ``async with
NotebookLMClient.from_storage(...) as client:`` — no ``await``. The
legacy ``async with await NotebookLMClient.from_storage(...)`` form
still works (it emits ``DeprecationWarning``; removed in v1.0) and is
the only form we permit in docstrings that explicitly call themselves
out as the migration reference.

For all other docstring example snippets, the parse-validity check below
is enough to keep things honest. The historical "must have `await`"
assertion is gone — that was correct under the old async-coroutine
``from_storage`` and is now exactly what we discourage.

Module docstrings in ``client.py`` and ``__init__.py`` historically
had the inverse bug (bare ``async with`` against a coroutine); this
test exists so future drift in either direction breaks a test instead
of silently shipping a broken example.
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
def test_from_storage_examples_prefer_canonical_idiom(relpath: str) -> None:
    """Docstring examples must not advertise the deprecated `await` form.

    ``async with await NotebookLMClient.from_storage(...) as client:``
    still works in v0.5.0+ but emits ``DeprecationWarning`` (removed in
    v1.0). Docstrings should advertise the new canonical idiom
    ``async with NotebookLMClient.from_storage(...) as client:``.

    Exception: docstrings that explicitly call themselves out as the
    migration reference (the same docstring block contains the word
    ``Legacy`` or ``deprecated``) may show the old form once.
    """
    path = _repo_root() / relpath
    tree = ast.parse(path.read_text())

    offenders: list[tuple[str, int, str]] = []
    for node, doc in _iter_docstrings(tree):
        doc_lower = doc.lower()
        # If the docstring as a whole calls itself out as the deprecation /
        # migration reference, allow the legacy example shape to appear in
        # it. Every other docstring must use the canonical idiom.
        is_deprecation_reference = "legacy" in doc_lower or "deprecated" in doc_lower
        if is_deprecation_reference:
            continue
        for idx, line in enumerate(doc.splitlines(), start=1):
            if "from_storage(" in line and "async with await" in line:
                offenders.append((getattr(node, "name", "<module>"), idx, line.strip()))

    assert not offenders, (
        f"{relpath}: found deprecated `async with await ...from_storage(...)` "
        f"example(s); use the canonical `async with NotebookLMClient.from_storage(...)` "
        f"form instead: {offenders}"
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
            # Skip pure-comment lines (e.g. an inline migration reference
            # like ``# async with await NotebookLMClient.from_storage(...) ...``).
            # The comment text isn't a parseable statement and isn't meant
            # to be runnable code.
            if line.startswith("#"):
                continue
            # Skip lines that are prose with embedded rst-quoted code
            # (e.g. "Prefer ``async with from_storage(...) as client:`` idiom.").
            # These aren't standalone code examples — they reference the
            # idiom in running text.
            if line.startswith("``") or "``" in line.split("from_storage(", 1)[0]:
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

    Mirrors what the module/class docstrings advertise (canonical idiom):

        async with NotebookLMClient.from_storage(path=...) as client:
            ...

    We assert that ``async with`` on the wrapper yields a connected
    ``NotebookLMClient`` instance — proves the example shape compiles
    and runs end-to-end.
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
    # during the wrapper's lazy ``_build``; serve a minimal stub so the
    # call resolves without touching the network.
    html = '"SNlM0e":"smoke_csrf" "FdrFJe":"smoke_session"'
    httpx_mock.add_response(
        url="https://notebooklm.google.com/",
        content=html.encode(),
    )

    async with NotebookLMClient.from_storage(path=str(storage_file)) as client:
        assert isinstance(client, NotebookLMClient)
        assert client.is_connected is True

    # After exiting the context manager, the client is closed.
    assert client.is_connected is False
