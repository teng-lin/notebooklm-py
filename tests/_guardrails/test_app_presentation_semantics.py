"""Guard semantic ``_app`` contracts against CLI presentation regressions."""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

from notebooklm._app.collections import CollectionResolutionError
from notebooklm._app.download import DownloadPlanValidationError, DownloadResult
from notebooklm._app.labels import LabelResolutionError
from notebooklm._app.research import ResearchValidationError
from notebooklm._app.source_add import SourceAddValidationError
from notebooklm._app.source_mutations import SourceMutationError

_APP_ROOT = Path(__file__).parents[2] / "src" / "notebooklm" / "_app"
_OWNED_MODULES = (
    "collections.py",
    "download.py",
    "labels.py",
    "research.py",
    "source_add.py",
    "source_mutations.py",
    "source_research.py",
)
_CLI_VOCABULARY = re.compile(r"notebooklm\s+|(?<![\w-])--[a-z][a-z-]*")


def _runtime_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("module_name", _OWNED_MODULES)
def test_neutral_runtime_strings_contain_no_cli_commands_or_flags(module_name: str) -> None:
    violations = [
        (line, value)
        for line, value in _runtime_strings(_APP_ROOT / module_name)
        if _CLI_VOCABULARY.search(value)
    ]
    assert violations == []


def test_download_result_contains_semantic_failure_not_presentation_fields() -> None:
    names = {field.name for field in fields(DownloadResult)}
    assert "failure" in names
    assert names.isdisjoint({"error", "error_code", "message", "hint", "suggestion", "exit_code"})


@pytest.mark.parametrize(
    "error",
    [
        DownloadPlanValidationError("conflicting_overwrite_policy"),
        SourceAddValidationError(
            "internal_ip_disallowed", url="http://127.0.0.1", host="127.0.0.1"
        ),
        SourceMutationError("id_not_found", token="missing"),
        CollectionResolutionError("not_found", token="missing"),
        LabelResolutionError("not_found", notebook_id="nb", token="missing"),
        ResearchValidationError("cited_requires_import"),
    ],
)
def test_neutral_errors_expose_semantics_not_cli_projection(error: Exception) -> None:
    assert _CLI_VOCABULARY.search(str(error)) is None
    assert not hasattr(error, "code")
    assert not hasattr(error, "message")
    assert not hasattr(error, "extra")
