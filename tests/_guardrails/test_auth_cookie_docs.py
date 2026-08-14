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
DEPRECATIONS = REPO_ROOT / "docs" / "deprecations.md"
PYTHON_API = REPO_ROOT / "docs" / "python-api.md"
PREDICATE_NAMES = {
    "_has_rotatable_secondary_binding",
    "_has_valid_secondary_binding",
}
SNIPPET_MARKER = "```python\nMINIMUM_REQUIRED_COOKIES ="
SECONDARY_BINDING_COOKIE_NAMES = {"APISID", "LSID", "OSID", "SAPISID"}
AUTH_COOKIE_DEPRECATION_ROWS = {
    "`AuthTokens.flat_cookies`": (
        "`AuthTokens.jar` for bootstrap-cookie questions; managed `NotebookLMClient` request "
        "APIs for HTTP",
        "v0.8.1",
        "v1.0",
        "Direct property access emits one caller-attributed `DeprecationWarning`. It is a lossy "
        "name-only projection and cannot preserve domain/path siblings. "
        "`NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses the warning.",
    ),
    "`AuthTokens.cookies` / `AuthTokens.cookie_jar`": (
        "Use `AuthTokens.jar` as the v0.x migration shape; adopt the immutable "
        "`initial_cookies: CookieJar` bootstrap field in v1",
        "v0.8.1",
        "v1.0",
        "**Docs-only deprecation:** these remain dataclass fields through v0.x, so runtime "
        "warnings would leak through construction, repr, equality, and `dataclasses.replace()`. "
        "They are public compatibility shadows, not the managed client's live jar.",
    ),
    "`AuthTokens.jar`": (
        "The v1 `AuthTokens.initial_cookies` bootstrap field",
        "v0.8.1",
        "v1.0",
        "Warning-free v0.x migration shape. It is an immutable question/input projection, not a "
        "second live-cookie authority.",
    ),
    "`AuthTokens.cookie_header`": (
        "Managed `NotebookLMClient` request APIs",
        "v0.8.1",
        "v1.0",
        "Docs-only deprecation. Its name-only, domain-blind join is unsafe for request "
        "construction; it remains warning-free through v0.x so it does not indirectly trigger "
        "the `flat_cookies` warning.",
    ),
    "`AuthTokens.cookie_header_for(url)`": (
        "Managed `NotebookLMClient` request APIs",
        "v0.8.1",
        "v1.0",
        "Docs-only deprecation. Domain-aware selection remains compatible for standalone "
        "callers, but first-party request paths already use the kernel-owned jar.",
    ),
}


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


def _scheduled_auth_cookie_rows(document: str) -> dict[str, tuple[str, str, str, str]]:
    """Parse the five auth-cookie rows from the scheduled-removal table."""
    scheduled = document.partition("## Scheduled for removal")[2].partition("\n## ")[0]
    rows: dict[str, tuple[str, str, str, str]] = {}
    for line in scheduled.splitlines():
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        if len(cells) != 5 or cells[0] not in AUTH_COOKIE_DEPRECATION_ROWS:
            continue
        assert cells[0] not in rows, f"duplicate deprecation-table row for {cells[0]}"
        rows[cells[0]] = cells[1:]
    return rows


def _cookiejar_contract_problems(document: str) -> set[str]:
    """Return missing or contradictory CookieJar sequence/authority claims."""
    text = re.sub(r"\s+", " ", document.replace("`", " "))
    problems: set[str] = set()
    if not re.search(r"\bCookieJar\b.{0,120}\bordered\s+sequence\b", text, re.IGNORECASE):
        problems.add("ordered sequence")
    if not re.search(
        r"\bCookieJar\b.{0,300}\b(?:never|not)\s+(?:a\s+)?Mapping\b",
        text,
        re.IGNORECASE,
    ):
        problems.add("not a Mapping")
    if not re.search(
        r"\bCookieJar\b.{0,350}\b(?:never|not)\s+(?:(?:a|the)\s+)?"
        r"(?:managed client(?:'s)?\s+)?live(?:[- ](?:cookie|mutable|transport))*\s+"
        r"(?:jar|authority)\b",
        text,
        re.IGNORECASE,
    ):
        problems.add("not a live jar/authority")
    if re.search(
        r"\bCookieJar\b\s+(?:is|implements|becomes|acts\s+as)\s+"
        r"(?!not\b|never\b)(?:an?\s+)?(?:mutable\s+)?Mapping\b",
        text,
        re.IGNORECASE,
    ):
        problems.add("positive Mapping claim")
    if re.search(
        r"\bCookieJar\b\s+(?:is|becomes|owns|provides)\s+(?!not\b|never\b)"
        r"(?:(?:a|the)\s+)?(?:managed client(?:'s)?\s+)?live"
        r"(?:[- ](?:cookie|mutable|transport))*\s+(?:jar|authority)\b",
        text,
        re.IGNORECASE,
    ):
        problems.add("positive live-authority claim")
    return problems


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


def test_auth_cookie_deprecation_runway_is_complete_and_sequence_shaped() -> None:
    """Every compatibility projection has explicit, non-Mapping migration guidance."""
    deprecations = DEPRECATIONS.read_text(encoding="utf-8")
    assert _scheduled_auth_cookie_rows(deprecations) == AUTH_COOKIE_DEPRECATION_ROWS

    for path in (DEPRECATIONS, PYTHON_API, LIFECYCLE_DOC):
        text = path.read_text(encoding="utf-8")
        assert "initial_cookies" in text
        plain_text = text.replace("`", "")
        assert re.search(
            r"managed(?:\s+NotebookLMClient|-client|\s+client).{0,40}request", plain_text
        )
        assert not _cookiejar_contract_problems(text), path.relative_to(REPO_ROOT)


@pytest.mark.parametrize(
    ("claim", "expected_problem"),
    [
        ("CookieJar implements a Mapping.", "positive Mapping claim"),
        ("CookieJar is a mutable Mapping.", "positive Mapping claim"),
        ("CookieJar is the live transport jar.", "positive live-authority claim"),
        ("CookieJar owns live-cookie authority.", "positive live-authority claim"),
    ],
)
def test_cookiejar_contract_guard_rejects_positive_claims(
    claim: str, expected_problem: str
) -> None:
    negative_contract = (
        "CookieJar is an immutable, ordered sequence, never a Mapping and never the live jar."
    )
    assert expected_problem in _cookiejar_contract_problems(f"{negative_contract} {claim}")


@pytest.mark.parametrize(
    "claim",
    [
        "CookieJar remains an immutable, ordered sequence. It is never a Mapping and never the "
        "managed client's live mutable jar.",
        "CookieJar is an immutable, ordered sequence of rows—not a mapping and not a live "
        "transport jar.",
        "CookieJar is an immutable, ordered sequence, never a Mapping and never the live jar.",
    ],
)
def test_cookiejar_contract_guard_accepts_negative_sequence_claims(claim: str) -> None:
    assert not _cookiejar_contract_problems(claim)
