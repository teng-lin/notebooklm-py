"""Assert cli/services/login/ import graph matches the declared leaf-ward DAG.

Run from repo root; uses AST to read each module's `from .<sibling> import …`
statements (relative imports only — absolute imports of stdlib/third-party
don't participate in the in-package DAG).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_PATH = Path("src/notebooklm/cli/services/login")

# NOTE: `__init__` is intentionally absent from ALLOWED_EDGES — the re-export-only
# guard (test_login_init_is_reexport_only) covers it. The DAG check below skips
# `__init__` from edge iteration to avoid flagging legitimate sibling re-exports
# (`from .cookie_domains import …`) as DAG violations.
# ``ALLOWED_EDGES`` is an UPPER BOUND, not an equality check — the DAG test
# verifies ``actual_edges ⊆ allowed_edges``. Edges marked "allowed but
# currently unused" below are pre-declared room for likely future imports
# (per the phase-3.md DAG diagram); the implementation modules don't take
# them today. If you remove an "unused" entry, the test will still pass —
# but you also remove the documented design intent that says "this edge is
# legitimate when needed". Keep the entry; add a comment when you start
# using it.
# ``io_seam`` (the caller-injected ``LoginIO`` Protocol + resolver, #1393) is a
# leaf: it imports no siblings. Every helper that emits / exits / runs async now
# threads a ``LoginIO`` sink and imports ``io_seam`` (the resolver and/or the
# Protocol under TYPE_CHECKING), so it is an allowed edge from the whole DAG.
ALLOWED_EDGES: dict[str, set[str]] = {
    "exceptions": set(),
    "outcomes": set(),
    "cookie_domains": set(),
    "rookiepy_errors": set(),
    "io_seam": set(),
    "cookie_jar": {
        "outcomes",
        # allowed but currently unused — _enumerate_one_jar formats its own
        # rookiepy error messages and does not call _handle_rookiepy_error.
        "rookiepy_errors",
        # io_seam: _enumerate_one_jar drives the account probe via io.run_async.
        "io_seam",
    },
    "chromium_accounts": {
        "cookie_jar",
        "rookiepy_errors",
        "cookie_domains",
        "outcomes",
        # io_seam: the chromium readers emit verbose progress via io.emit.
        "io_seam",
    },
    "firefox_accounts": {
        # _read_firefox_container_cookies returns a CookieValidationFailure
        # (a BrowserCookieOutcome) on every extractor failure instead of
        # console.print + exit_with_code, so the command layer renders + exits.
        "outcomes",
        "rookiepy_errors",
        "cookie_domains",
        # allowed but currently unused — the firefox helpers hand raw cookies
        # back to the caller (browser_accounts) which then routes through
        # _enumerate_one_jar; this module does not import it directly.
        "cookie_jar",
        # io_seam: the firefox readers emit verbose progress via io.emit.
        "io_seam",
    },
    "browser_accounts": {
        "chromium_accounts",
        "firefox_accounts",
        "cookie_jar",
        "outcomes",
        "rookiepy_errors",
        # The phase-3 DAG diagram routes cookie_domains via chromium/firefox
        # subordinates, but ``_read_browser_cookies``'s "auto" + named-alias
        # branch (the legacy ``rookiepy.load`` path) constructs its own domain
        # list — that call site lives in browser_accounts, not in the
        # browser-family subordinates. Adding the edge here keeps the dispatch
        # logic colocated; the DAG stays acyclic (cookie_domains is a leaf).
        "cookie_domains",
        # io_seam: the dispatcher resolves + threads the LoginIO sink.
        "io_seam",
    },
    "profile_targets": {
        # ADR-0015 Pattern B decoupling — _validate_profile_name raises
        # LoginConfigurationError instead of click.ClickException.
        "exceptions",
    },
    "cookie_writes": {
        # allowed but currently unused — the writer operates on already-loaded
        # cookie data and the selectors don't query the cookie-domain policy.
        "browser_accounts",
        "cookie_domains",
        "outcomes",
        # io_seam: _write_extracted_cookies / _select_account emit warnings and
        # run the verification probe through the injected sink.
        "io_seam",
    },
    "refresh": {
        "browser_accounts",
        "cookie_writes",
        "outcomes",
        "profile_targets",
        # io_seam: refresh drivers own success messaging + exit policy via the
        # injected sink (io.emit / io.fail / io.run_async).
        "io_seam",
    },
}


def _module_edges(path: Path) -> set[str]:
    """Return the set of sibling module names this module imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    edges: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 1:
            # level == 0: absolute import (stdlib/third-party — not in-package DAG)
            # level >= 2: parent-package import (e.g. `from .. import x` reaches
            #   cli/services/) — silently allowed; package boundary is policed
            #   at a higher level and is out of scope for this DAG lint.
            continue
        if node.module is None:
            # `from . import <sibling>[, …]` — sibling names live in node.names.
            for alias in node.names:
                edges.add(alias.name)
        else:
            # `from .<sibling> import …` — node.module is the sibling, possibly
            # with sub-attrs (`from .cookie_domains.x import y`); take the head.
            edges.add(node.module.split(".")[0])
    return edges


def _detect_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Return any simple cycles via DFS."""
    cycles: list[list[str]] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            cycles.append([*path[path.index(node) :], node])
            return
        if node in visited:
            return
        visiting.add(node)
        path.append(node)
        for child in edges.get(node, set()):
            visit(child)
        path.pop()
        visiting.discard(node)
        visited.add(node)

    for node in edges:
        visit(node)
    return cycles


def test_login_package_dag() -> None:
    # ALLOWED_EDGES covers implementation modules only; __init__.py is the
    # re-export surface and is policed by test_login_init_is_reexport_only.
    if not PKG_PATH.is_dir():
        # Package not yet split (e.g. pre-T4 baseline run). Skip gracefully —
        # the DAG policy only applies once the package exists.
        pytest.skip(f"{PKG_PATH} does not exist yet (pre-split state)")

    expected_modules = set(ALLOWED_EDGES)
    actual_modules = {p.stem for p in PKG_PATH.glob("*.py") if p.stem != "__init__"}
    assert actual_modules == expected_modules, (
        f"Module set mismatch.\n  expected: {sorted(expected_modules)}\n"
        f"  actual:   {sorted(actual_modules)}\n"
        f"Update ALLOWED_EDGES (and phase-3.md DAG) if the layout intentionally changed."
    )

    actual_edges: dict[str, set[str]] = {}
    edge_violations: list[str] = []
    for module_name in expected_modules:
        module_path = PKG_PATH / f"{module_name}.py"
        edges = _module_edges(module_path)
        actual_edges[module_name] = edges
        allowed = ALLOWED_EDGES[module_name]
        unexpected = edges - allowed
        if unexpected:
            edge_violations.append(
                f"{module_name}.py imports {sorted(unexpected)} which is not in ALLOWED_EDGES "
                f"({sorted(allowed)})."
            )
    assert not edge_violations, "DAG edge violations:\n  " + "\n  ".join(edge_violations)

    cycles = _detect_cycles(actual_edges)
    assert not cycles, f"Import cycles detected: {cycles}"
