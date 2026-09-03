#!/usr/bin/env python3
"""Audit direct-module imports within ``notebooklm._auth`` without importing it."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUTH_DIR = REPO_ROOT / "src" / "notebooklm" / "_auth"
DEFAULT_PACKAGE_PREFIX = "notebooklm._auth"


class _ImportCollector(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        nodes: set[str],
        *,
        package_prefix: str = DEFAULT_PACKAGE_PREFIX,
        include_external: bool = False,
    ) -> None:
        self.source = source
        self.nodes = nodes
        self.package_prefix = package_prefix
        self.include_external = include_external
        self.function_depth = 0
        self.edges: set[tuple[str, str, str]] = set()

    @property
    def scope(self) -> str:
        return "function" if self.function_depth else "module"

    def _add(self, target: str) -> None:
        is_local = target in self.nodes
        if (is_local or self.include_external) and target != self.source:
            self.edges.add((self.source, target, self.scope))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            prefix = f"{self.package_prefix}."
            if item.name.startswith(prefix):
                self._add(item.name[len(prefix) :].split(".", 1)[0])
            elif self.include_external and item.name.startswith("notebooklm."):
                self._add(item.name.removeprefix("notebooklm."))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level == 1:
            if module:
                self._add(module.split(".", 1)[0])
            else:
                for item in node.names:
                    self._add(item.name.split(".", 1)[0])
            return
        if node.level == 2 and self.include_external:
            if module == "_auth":
                for item in node.names:
                    self._add(f"_auth.{item.name.split('.', 1)[0]}")
            elif module:
                self._add(module)
            else:
                for item in node.names:
                    self._add(item.name.split(".", 1)[0])
            return
        if node.level:
            return
        if module == self.package_prefix:
            for item in node.names:
                self._add(item.name.split(".", 1)[0])
        elif module.startswith(f"{self.package_prefix}."):
            self._add(module.removeprefix(f"{self.package_prefix}.").split(".", 1)[0])
        elif self.include_external:
            if module == "notebooklm":
                for item in node.names:
                    self._add(item.name.split(".", 1)[0])
            elif module.startswith("notebooklm."):
                self._add(module.removeprefix("notebooklm."))


def _strong_components(nodes: set[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node]):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return sorted(result)


def build_projection(
    auth_dir: Path = DEFAULT_AUTH_DIR,
    *,
    repo_root: Path = REPO_ROOT,
    package_prefix: str = DEFAULT_PACKAGE_PREFIX,
    include_external: bool = False,
) -> dict[str, object]:
    """Build the deterministic module/edge/SCC projection for a direct package directory."""
    paths = sorted(auth_dir.glob("*.py"))
    nodes = {path.stem for path in paths}
    modules: list[dict[str, object]] = []
    all_edges: set[tuple[str, str, str]] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            relative = path.as_posix()
        modules.append({"name": path.stem, "path": relative, "lines": len(text.splitlines())})
        collector = _ImportCollector(
            path.stem,
            nodes,
            package_prefix=package_prefix,
            include_external=include_external,
        )
        collector.visit(ast.parse(text, filename=str(path)))
        all_edges.update(collector.edges)
    edges = [
        {"source": source, "target": target, "scope": scope}
        for source, target, scope in sorted(all_edges)
    ]
    module_pairs = {
        (s, t) for s, t, scope in all_edges if scope == "module" and s in nodes and t in nodes
    }
    all_pairs = {(s, t) for s, t, _scope in all_edges if s in nodes and t in nodes}
    return {
        "version": 1,
        "modules": modules,
        "edges": edges,
        "summary": {
            "modules": len(modules),
            "total_lines": sum(int(module["lines"]) for module in modules),
            "unique_edges": len(all_edges),
            "module_edges": sum(scope == "module" for _s, _t, scope in all_edges),
            "function_local_edges": sum(scope == "function" for _s, _t, scope in all_edges),
        },
        "sccs": {
            "module_level": _strong_components(nodes, module_pairs),
            "all_scopes": _strong_components(nodes, all_pairs),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    parser.add_argument("--package-prefix", default=DEFAULT_PACKAGE_PREFIX)
    parser.add_argument("--include-external", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.auth_dir.is_dir():
        parser.error(f"not a directory: {args.auth_dir}")
    projection = build_projection(
        args.auth_dir,
        package_prefix=args.package_prefix,
        include_external=args.include_external,
    )
    if args.json:
        json.dump(projection, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        summary = projection["summary"]
        print(
            f"{summary['modules']} modules, {summary['total_lines']} lines, "
            f"{summary['unique_edges']} edges "
            f"({summary['module_edges']} module, "
            f"{summary['function_local_edges']} function-local)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
