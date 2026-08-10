"""Keep the documented secondary-binding rule aligned with runtime policy.

The authoritative auth lifecycle guide once retained the superseded
``APISID`` + ``SAPISID`` predicate after runtime added the bare ``LSID``
conjunct (#1977/#2074). These gates compare both executable documentation
predicates with runtime and keep user-facing summaries of the strict rule from
silently dropping bare ``LSID`` again.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import re
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from notebooklm._auth.cookie_policy import (
    MINIMUM_REQUIRED_COOKIES,
    _has_rotatable_secondary_binding,
    _has_valid_secondary_binding,
)

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_DOC = REPO_ROOT / "docs" / "auth-cookie-lifecycle.md"
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"
CONFIGURATION = REPO_ROOT / "docs" / "configuration.md"
PREDICATE_NAMES = {
    "_has_rotatable_secondary_binding",
    "_has_valid_secondary_binding",
}
SNIPPET_MARKER = "```python\nMINIMUM_REQUIRED_COOKIES ="
SECONDARY_BINDING_COOKIE_NAMES = {"APISID", "LSID", "OSID", "SAPISID"}


def _documented_policy() -> tuple[
    dict[str, Callable[[set[str]], bool]],
    dict[str, ast.FunctionDef],
    set[str],
]:
    text = LIFECYCLE_DOC.read_text(encoding="utf-8")
    marker_at = text.find(SNIPPET_MARKER)
    assert marker_at >= 0, (
        f"{LIFECYCLE_DOC.relative_to(REPO_ROOT)} must retain the executable cookie-policy "
        "snippet so its rule can be checked against runtime"
    )
    code_at = marker_at + len("```python\n")
    fence_at = text.find("\n```", code_at)
    assert fence_at >= 0, "cookie-policy documentation snippet has no closing fence"

    tree = ast.parse(text[code_at:fence_at], filename=str(LIFECYCLE_DOC))
    predicate_nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in PREDICATE_NAMES
    }
    assert set(predicate_nodes) == PREDICATE_NAMES, (
        "cookie-policy documentation snippet must define both secondary-binding "
        "predicates exactly once"
    )

    namespace: dict[str, object] = {}
    exec(compile(tree, str(LIFECYCLE_DOC), "exec"), namespace)
    predicates = {name: namespace[name] for name in PREDICATE_NAMES}
    minimum_required = namespace["MINIMUM_REQUIRED_COOKIES"]
    assert all(callable(predicate) for predicate in predicates.values())
    assert isinstance(minimum_required, set)
    assert all(isinstance(name, str) for name in minimum_required)
    return predicates, predicate_nodes, set(minimum_required)


def _cookie_names(predicate_node: ast.FunctionDef) -> set[str]:
    body = predicate_node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return {
        node.value
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_lifecycle_predicate_snippet_matches_runtime_for_every_cookie_subset() -> None:
    """The guide's executable predicates must remain truth-table-equivalent to runtime."""
    documented, documented_nodes, documented_minimum = _documented_policy()
    assert documented_minimum == MINIMUM_REQUIRED_COOKIES

    runtime_predicates = {
        "_has_rotatable_secondary_binding": _has_rotatable_secondary_binding,
        "_has_valid_secondary_binding": _has_valid_secondary_binding,
    }
    documented_cookie_names: set[str] = set()
    for name, runtime in runtime_predicates.items():
        runtime_tree = ast.parse(textwrap.dedent(inspect.getsource(runtime)))
        runtime_node = runtime_tree.body[0]
        assert isinstance(runtime_node, ast.FunctionDef)

        cookie_names = sorted(_cookie_names(documented_nodes[name]) | _cookie_names(runtime_node))
        documented_cookie_names.update(cookie_names)
        for size in range(len(cookie_names) + 1):
            for present in itertools.combinations(cookie_names, size):
                cookie_set = set(present)
                assert documented[name](cookie_set) is runtime(cookie_set), (
                    f"documented {name} disagrees with runtime for cookies {sorted(cookie_set)}"
                )
    assert documented_cookie_names == SECONDARY_BINDING_COOKIE_NAMES


def test_user_facing_secondary_binding_summaries_match_strict_rule() -> None:
    """User-facing summaries must retain both branches and the bare-LSID conjunct."""
    paragraphs = re.split(r"\n\s*\n", CLI_REFERENCE.read_text(encoding="utf-8"))
    binding_summaries = [
        paragraph
        for paragraph in paragraphs
        if "Imported cookies are filtered" in paragraph or "secondary-binding check" in paragraph
    ]
    assert len(binding_summaries) == 2, "CLI reference must retain both guarded summaries"
    strict_cli_rule = "`OSID`, or `APISID`+`SAPISID` together with bare `LSID`"
    assert all(strict_cli_rule in paragraph for paragraph in binding_summaries), (
        "every CLI summary must retain OSID OR APISID+SAPISID+bare-LSID semantics"
    )

    configuration = CONFIGURATION.read_text(encoding="utf-8")
    strict_configuration_rule = (
        "either `OSID` is present, or `APISID` and `SAPISID` are present "
        "**together with bare `LSID`**"
    )
    assert strict_configuration_rule in configuration, (
        "configuration summary must retain OSID OR APISID+SAPISID+bare-LSID semantics"
    )
