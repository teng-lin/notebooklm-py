#!/usr/bin/env python3
"""Measure the web backend inheritance chain for the P9 entry record.

``docs/plan/2026-08-13-semantic-backend-refactor.md`` carries a "P9 entry
record": a table of structural measurements over ``WebRpcBackend`` and its
ancestor classes in ``src/notebooklm/_web/``, the operation registry, the
call-policy ledger, ``_idempotency.py``, and the tests/catalog strings that
reach into the chain. The plan's entry criteria require those numbers to be
re-measured with a committed script before P9.0 opens; this is that script.

Every row is computed mechanically from ``inspect``/``ast`` over the live
package plus regex scans of ``tests/``, ``scripts/`` and the frozen catalog
baseline. Rows the plan measured by hand (for example the "about a third drive
multi-native operations" annotation) are not reproduced.

Usage:
    uv run python scripts/measure_web_backend_chain.py          # Markdown table
    uv run python scripts/measure_web_backend_chain.py --json   # machine-readable

The Markdown table follows the row order of the plan's entry record so the two
can be compared side by side. Exit code is always 0; the script measures, it
does not gate.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
TESTS_ROOT = REPO_ROOT / "tests"
CATALOG_JSON = TESTS_ROOT / "fixtures" / "baselines" / "operation_catalog.json"
CATALOG_UNIT_TEST = TESTS_ROOT / "unit" / "test_operation_catalog.py"
AUTHORITIES_SCRIPT = REPO_ROOT / "scripts" / "_operation_catalog_authorities.py"
GUARDRAILS_DIR = TESTS_ROOT / "_guardrails"
WEB_BACKEND_TEST = TESTS_ROOT / "unit" / "test_web_backend.py"
POLICY_PATH = SRC_ROOT / "_web" / "policy.py"
IDEMPOTENCY_PATH = SRC_ROOT / "_idempotency.py"
CAPABILITY_PORT = SRC_ROOT / "_backend.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The plan's pinned recorded-kwargs assertion pattern, verbatim.
RECORDED_KWARGS_RE = re.compile(
    r"\.kwargs\[\"(source_path|allow_null|operation_variant|outcome_unknown_on_expiry"
    r"|raise_on_null_status|disable_internal_retries|attempt_timeout|read_timeout"
    r"|_retry_deadline)\"\]"
)
RPC_CALL_KEYWORDS = (
    "source_path",
    "allow_null",
    "operation_variant",
    "outcome_unknown_on_expiry",
    "raise_on_null_status",
    "disable_internal_retries",
    "attempt_timeout",
    "_is_retry",
)
STRICT_WEB_REF_RE = re.compile(r"_web/[A-Za-z0-9_/]+\.py:[A-Z][A-Za-z0-9_]*\.[A-Za-z0-9_]+")
ANY_WEB_PATH_RE = re.compile(r"_web/[A-Za-z0-9_/]+\.py")
RPC_METHOD_RE = re.compile(r"\bRPCMethod\b")
RPC_METHOD_MEMBER_RE = re.compile(r"\bRPCMethod\.")
TRANSLATE_ERROR_RE = re.compile(r"\b_translate_error\(")
DIRECT_CONSTRUCTION_RE = re.compile(r"\bWebRpcBackend\(")
BUILD_FIXTURE_RE = re.compile(r"\bbuild_web_backend\b")


# --- chain introspection ------------------------------------------------------


def _chain_classes() -> list[type]:
    from notebooklm._web.backend import WebRpcBackend

    return [cls for cls in WebRpcBackend.__mro__ if cls is not object]


def _class_def(cls: type) -> tuple[Path, ast.ClassDef, ast.Module]:
    path = Path(inspect.getsourcefile(cls) or "")
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            return path, node, module
    raise RuntimeError(f"{cls.__qualname__} has no top-level ClassDef in {path}")


def _self_calls(node: ast.AST) -> Iterator[ast.Call]:
    """Yield every ``self.<attr>(...)`` call under ``node``."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if isinstance(owner, ast.Name) and owner.id == "self":
            yield call


def _self_attr_names(node: ast.AST) -> set[str]:
    """Every ``self.<name>`` attribute read or written under ``node``."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            if sub.value.id == "self":
                names.add(sub.attr)
    return names


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _method_names(cls: type) -> set[str]:
    """Non-dunder callable entries of ``vars(cls)``: plain functions and staticmethods.

    This is the plan's ``vars()`` notion. ``property`` and ``classmethod`` objects are
    descriptors, not callables, so they are excluded here and reported separately.
    """
    return {name for name, value in vars(cls).items() if not _is_dunder(name) and callable(value)}


def _descriptor_names(cls: type) -> set[str]:
    """Every method-like entry of ``vars(cls)`` including properties, classmethods, dunders."""
    return {
        name
        for name, value in vars(cls).items()
        if inspect.isfunction(value) or isinstance(value, property | classmethod | staticmethod)
    }


def _function_defs(class_node: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _raises_not_implemented(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for stmt in fn.body:
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc
            target = exc.func if isinstance(exc, ast.Call) else exc
            if isinstance(target, ast.Name) and target.id == "NotImplementedError":
                return True
    return False


def _introducing_class(chain: list[type], attr: str) -> type | None:
    """The base-most chain class whose ``vars()`` defines ``attr``.

    A call is cross-class when the name it reaches for is introduced by another
    class. Overriding an ancestor's ``NotImplementedError`` stub does not make
    the head the owner: the contract still lives where the name was introduced,
    so the head's own ``self._rpc_call(...)`` sites count as cross-class too.
    """
    for cls in reversed(chain):
        if attr in vars(cls):
            return cls
    return None


def _assigned_self_attrs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    names: list[str] = []
    for sub in ast.walk(fn):
        targets: list[ast.expr] = []
        if isinstance(sub, ast.Assign):
            targets = list(sub.targets)
        elif isinstance(sub, ast.AnnAssign | ast.AugAssign):
            targets = [sub.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr not in names
            ):
                names.append(target.attr)
    return names


def _instance_callables() -> tuple[int, str]:
    """Count non-dunder callables on a constructed instance, or fall back to the class."""
    from notebooklm._web.backend import WebRpcBackend

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from tests._fixtures.web_backend import build_web_backend
    except Exception:  # pragma: no cover - fixture import is best effort
        target: object = WebRpcBackend
        how = "class (fixture not importable)"
    else:
        target = build_web_backend(object())
        how = "instance via tests/_fixtures/web_backend.py::build_web_backend"
    count = sum(
        1
        for name in dir(target)
        if not _is_dunder(name) and callable(inspect.getattr_static(target, name))
    )
    return count, how


def _reachable_rpc_sites(
    head: type,
    method_name: str,
    seen: tuple[str, ...] = (),
) -> set[tuple[str, int, str | None]]:
    """Syntactically reachable ``self._rpc_call`` sites, as the deadline-seeding test walks them.

    Returns ``(qualname, lineno, native)`` where ``native`` is the ``RPCMethod``
    member named by the first positional argument, when it is one.
    """
    if method_name in seen:
        return set()
    method = getattr(head, method_name, None)
    if method is None:
        return set()
    source = inspect.getsource(method)
    node = ast.parse(_dedent(source)).body[0]
    sites: set[tuple[str, int, str | None]] = set()
    callees: set[str] = set()
    for call in _self_calls(node):
        assert isinstance(call.func, ast.Attribute)
        if call.func.attr == "_rpc_call":
            native = None
            if call.args:
                first = call.args[0]
                if (
                    isinstance(first, ast.Attribute)
                    and isinstance(first.value, ast.Name)
                    and first.value.id == "RPCMethod"
                ):
                    native = first.attr
            sites.add((method.__qualname__, call.lineno, native))
        elif call.func.attr.startswith("_"):
            callees.add(call.func.attr)
    for callee in callees:
        sites.update(_reachable_rpc_sites(head, callee, (*seen, method_name)))
    return sites


def _dedent(source: str) -> str:
    import textwrap

    return textwrap.dedent(source)


# --- file scans ----------------------------------------------------------------


def _py_files(root: Path) -> Iterable[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _count_files_matching(files: Iterable[Path], pattern: re.Pattern[str]) -> list[str]:
    return [_rel(p) for p in files if pattern.search(p.read_text(encoding="utf-8"))]


def _count_matches(path: Path, pattern: re.Pattern[str]) -> int:
    return len(pattern.findall(path.read_text(encoding="utf-8")))


def _count_matching_lines(path: Path, pattern: re.Pattern[str]) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if pattern.search(line))


def _per_file_floors_on_web() -> int | None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python < 3.11
        return None
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    floors = data.get("tool", {}).get("notebooklm", {}).get("per_file_coverage_floors", {})
    return sum(1 for key in floors if "_web/" in key)


# --- measurement ---------------------------------------------------------------


def measure() -> dict[str, Any]:
    """Compute every mechanically measurable entry-record row."""
    from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES
    from notebooklm._web.policy import WEB_CALL_POLICY_BINDINGS
    from notebooklm._web.registry import WEB_OPERATION_REGISTRY

    chain = _chain_classes()
    head = chain[0]
    defs = {cls: _class_def(cls) for cls in chain}

    # Lines.
    class_body_lines = sum(node.end_lineno - node.lineno + 1 for _p, node, _m in defs.values())
    chain_files = sorted({path for path, _n, _m in defs.values()})
    file_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in chain_files)

    # Methods.
    methods_by_vars = sum(len(_method_names(cls)) for cls in chain)
    descriptors_by_vars = sum(len(_descriptor_names(cls)) for cls in chain)
    instance_callables, instance_how = _instance_callables()

    # State attributes.
    head_fns = _function_defs(defs[head][1])
    head_state = _assigned_self_attrs(head_fns["__init__"]) if "__init__" in head_fns else []
    ancestor_state: dict[str, list[str]] = {}
    for cls in chain[1:]:
        assigned: list[str] = []
        for fn in _function_defs(defs[cls][1]).values():
            assigned.extend(a for a in _assigned_self_attrs(fn) if a not in assigned)
        if assigned:
            ancestor_state[cls.__name__] = assigned

    # super() calls.
    super_calls = sum(
        1
        for _p, node, _m in defs.values()
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "super"
    )

    # Abstract seams.
    seams: list[str] = []
    for cls in chain[1:]:
        for name, fn in _function_defs(defs[cls][1]).items():
            if _raises_not_implemented(fn) and name in head_fns:
                if not _raises_not_implemented(head_fns[name]) and name not in seams:
                    seams.append(name)

    # Cross-class calls and _rpc_call sites.
    cross_calls = 0
    cross_rpc_calls = 0
    rpc_call_sites = 0
    keyword_usage: Counter[str] = Counter()
    for cls in chain:
        for call in _self_calls(defs[cls][1]):
            assert isinstance(call.func, ast.Attribute)
            attr = call.func.attr
            if attr == "_rpc_call":
                rpc_call_sites += 1
                keyword_usage.update(kw.arg for kw in call.keywords if kw.arg is not None)
            owner = _introducing_class(chain, attr)
            if owner is not None and owner is not cls:
                cross_calls += 1
                if attr == "_rpc_call":
                    cross_rpc_calls += 1

    # Links with zero dependency on the immediate base.
    links: list[dict[str, Any]] = []
    for child, base in zip(chain, chain[1:], strict=False):
        base_names = {name for name in vars(base) if not _is_dunder(name)}
        used = sorted(_self_attr_names(defs[child][1]) & base_names)
        links.append({"child": child.__name__, "base": base.__name__, "uses": used})
    zero_dependency_links = sum(1 for link in links if not link["uses"])

    # P9.4c deletes handler-name dispatch. Keep the entry-record dimensions in
    # the exit report as zero-valued historical measurements.
    supported: dict[Any, str] = {}
    row_backed = sorted(
        op.value
        for op, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported and binding.row is not None
    )
    reachable = {op: _reachable_rpc_sites(head, name) for op, name in supported.items()}
    composite_ops = sorted(op.value for op, sites in reachable.items() if len(sites) > 1)
    leaf_count = len(supported) - len(composite_ops)
    ledger_multi_deadline = sum(1 for op in SEMANTIC_DEADLINE_AUTHORITIES if op in supported)

    # Policy ledger.
    single = [op for op, b in WEB_CALL_POLICY_BINDINGS.items() if len(b.native_bindings) == 1]
    multi = [op for op, b in WEB_CALL_POLICY_BINDINGS.items() if len(b.native_bindings) >= 2]
    single_natives = {
        n.method for op in single for n in WEB_CALL_POLICY_BINDINGS[op].native_bindings
    }
    multi_natives = {n.method for op in multi for n in WEB_CALL_POLICY_BINDINGS[op].native_bindings}
    only_multi_by_ledger = sorted(m.name for m in multi_natives - single_natives)

    leaf_natives_by_code: set[str] = set()
    multi_natives_by_code: set[str] = set()
    for sites in reachable.values():
        natives = {native for _q, _l, native in sites if native is not None}
        if len(sites) > 1:
            multi_natives_by_code |= natives
        else:
            leaf_natives_by_code |= natives
    only_multi_by_code = sorted(multi_natives_by_code - leaf_natives_by_code)

    # capabilities.supports() consumers outside the port.
    supports_consumers = [
        _rel(p)
        for p in _py_files(SRC_ROOT)
        if p != CAPABILITY_PORT and re.search(r"\.supports\(", p.read_text(encoding="utf-8"))
    ]

    # policy.py / _idempotency.py.
    policy_lines = len(POLICY_PATH.read_text(encoding="utf-8").splitlines())
    policy_rpc_refs = _count_matches(POLICY_PATH, RPC_METHOD_RE)
    policy_rpc_member_refs = _count_matches(POLICY_PATH, RPC_METHOD_MEMBER_RE)
    idempotency_rpc_refs = _count_matches(IDEMPOTENCY_PATH, RPC_METHOD_RE)
    idempotency_rpc_member_refs = _count_matches(IDEMPOTENCY_PATH, RPC_METHOD_MEMBER_RE)

    # Tests reaching into chain internals.
    test_files = list(_py_files(TESTS_ROOT))
    translate_sites_by_file = {
        _rel(p): n for p in test_files if (n := _count_matches(p, TRANSLATE_ERROR_RE))
    }
    recorded_by_file = {
        _rel(p): n for p in test_files if (n := _count_matching_lines(p, RECORDED_KWARGS_RE))
    }
    direct_construction_files = _count_files_matching(test_files, DIRECT_CONSTRUCTION_RE)
    # Includes tests/_fixtures/web_backend.py, the fixture's own definition.
    build_fixture_files = _count_files_matching(test_files, BUILD_FIXTURE_RE)

    # Catalog strings.
    guardrail_files = list(_py_files(GUARDRAILS_DIR))
    catalog = {
        "strict_json": _count_matches(CATALOG_JSON, STRICT_WEB_REF_RE),
        "strict_unit_test": _count_matches(CATALOG_UNIT_TEST, STRICT_WEB_REF_RE),
        "any_json": _count_matches(CATALOG_JSON, ANY_WEB_PATH_RE),
        "any_authorities_script": _count_matches(AUTHORITIES_SCRIPT, ANY_WEB_PATH_RE),
        "any_guardrails": sum(_count_matches(p, ANY_WEB_PATH_RE) for p in guardrail_files),
    }

    return {
        "mro_depth": len(chain),
        "chain": [cls.__name__ for cls in chain],
        "chain_files": [_rel(p) for p in chain_files],
        "class_body_lines": class_body_lines,
        "file_lines": file_lines,
        "methods_by_vars": methods_by_vars,
        "descriptors_by_vars": descriptors_by_vars,
        "instance_callables": instance_callables,
        "instance_callables_source": instance_how,
        "head_state_attributes": head_state,
        "ancestor_state_attributes": ancestor_state,
        "super_calls": super_calls,
        "abstract_seams": seams,
        "cross_class_calls": cross_calls,
        "cross_class_rpc_calls": cross_rpc_calls,
        "rpc_call_sites": rpc_call_sites,
        "rpc_call_keyword_usage": {kw: keyword_usage.get(kw, 0) for kw in RPC_CALL_KEYWORDS},
        "rpc_call_other_keywords": {
            kw: n for kw, n in sorted(keyword_usage.items()) if kw not in RPC_CALL_KEYWORDS
        },
        "links": links,
        "zero_dependency_links": zero_dependency_links,
        "registry_handler_names": len(supported),
        "registry_binding_rows": row_backed,
        "leaf_handlers_by_code": leaf_count,
        "composite_handlers_by_code": composite_ops,
        "deadline_ledger_entries": ledger_multi_deadline,
        "ledger_single_native": len(single),
        "ledger_multi_native": len(multi),
        "natives_only_multi_by_ledger": only_multi_by_ledger,
        "natives_only_multi_by_code": only_multi_by_code,
        "supports_consumers_outside_port": supports_consumers,
        "policy_lines": policy_lines,
        "policy_rpc_method_refs": policy_rpc_refs,
        "policy_rpc_method_member_refs": policy_rpc_member_refs,
        "idempotency_rpc_method_refs": idempotency_rpc_refs,
        "idempotency_rpc_method_member_refs": idempotency_rpc_member_refs,
        "translate_error_sites": sum(translate_sites_by_file.values()),
        "translate_error_files": translate_sites_by_file,
        "recorded_kwargs_lines": sum(recorded_by_file.values()),
        "recorded_kwargs_files": recorded_by_file,
        "recorded_kwargs_in_web_backend_test": recorded_by_file.get(_rel(WEB_BACKEND_TEST), 0),
        "direct_construction_test_files": direct_construction_files,
        "build_web_backend_test_files": build_fixture_files,
        "catalog": catalog,
        "per_file_coverage_floors_on_web": _per_file_floors_on_web(),
    }


# --- rendering -----------------------------------------------------------------


def _fmt(n: int) -> str:
    return f"{n:,}"


def format_markdown(m: dict[str, Any]) -> str:
    kw = m["rpc_call_keyword_usage"]
    cat = m["catalog"]
    ancestors = m["ancestor_state_attributes"]
    state_note = (
        "all in the head's `__init__`" if not ancestors else f"ancestors also assign: {ancestors}"
    )
    floors = m["per_file_coverage_floors_on_web"]
    rows = [
        ("`WebRpcBackend.__mro__` depth (excl. `object`)", str(m["mro_depth"])),
        (
            "Class-body lines across the chain / file lines",
            f"{_fmt(m['class_body_lines'])} / {_fmt(m['file_lines'])}",
        ),
        (
            "Methods (`vars()` per class, summed) / non-dunder callables on the instance (`dir()`)",
            f"{m['methods_by_vars']} / {m['instance_callables']} "
            f"({m['descriptors_by_vars']} `vars()` entries counting properties, classmethods "
            f"and `__init__`; {m['instance_callables_source']})",
        ),
        ("State attributes, " + state_note, str(len(m["head_state_attributes"]))),
        ("`super()` calls in the chain", str(m["super_calls"])),
        (
            "Abstract seams (`NotImplementedError` in ancestor, body in head)",
            str(len(m["abstract_seams"])),
        ),
        (
            "Cross-class calls / of which `_rpc_call`; total `self._rpc_call(` sites",
            f"{m['cross_class_calls']} / {m['cross_class_rpc_calls']}; {m['rpc_call_sites']}",
        ),
        (
            "Links with zero dependency on immediate base",
            f"{m['zero_dependency_links']} of {len(m['links'])}",
        ),
        (
            "Registry handler names / leaf names no existing check resolves",
            f"{m['registry_handler_names']} / {m['leaf_handlers_by_code']} "
            f"({len(m['composite_handlers_by_code'])} composite by handler-code walk; "
            f"{m['deadline_ledger_entries']} deadline-ledger entries; "
            f"{len(m['registry_binding_rows'])} binding rows)",
        ),
        (
            "Operations by policy ledger: single-native / multi-native",
            f"{m['ledger_single_native']} / {m['ledger_multi_native']}",
        ),
        (
            "Natives appearing only in multi-native bindings: by ledger / by handler code",
            f"{len(m['natives_only_multi_by_ledger'])} / {len(m['natives_only_multi_by_code'])}",
        ),
        (
            "`capabilities.supports()` consumers outside the port",
            str(len(m["supports_consumers_outside_port"])),
        ),
        (
            f"`_rpc_call` keyword usage at the {m['rpc_call_sites']} call sites: "
            + " · ".join(f"`{k}`" for k in RPC_CALL_KEYWORDS),
            " · ".join(str(kw[k]) for k in RPC_CALL_KEYWORDS),
        ),
        (
            "`policy.py` lines / `RPCMethod` references (`RPCMethod.` member refs; any token)",
            f"{m['policy_lines']} / {m['policy_rpc_method_member_refs']}; "
            f"{m['policy_rpc_method_refs']}",
        ),
        (
            "`_idempotency.py` `RPCMethod` references (any token; `RPCMethod.` member refs)",
            f"{m['idempotency_rpc_method_refs']}; {m['idempotency_rpc_method_member_refs']}",
        ),
        (
            "Tests reaching into chain internals",
            f"{m['translate_error_sites']} unbound `_translate_error` sites "
            f"({len(m['translate_error_files'])} files)",
        ),
        (
            "Recorded-kwargs assertion lines (pinned pattern)",
            f"{m['recorded_kwargs_lines']} in {len(m['recorded_kwargs_files'])} files "
            f"({m['recorded_kwargs_in_web_backend_test']} in `test_web_backend.py`)",
        ),
        (
            "Direct `WebRpcBackend(...)` constructions in tests / files using `build_web_backend`",
            f"{len(m['direct_construction_test_files'])} files / "
            f"{len(m['build_web_backend_test_files'])} files",
        ),
        (
            "Catalog `_web/` strings — strict `file.py:Class.method`: JSON / unit test; "
            "any `_web/*.py` path: JSON / authorities script / guardrail",
            f"{cat['strict_json']} / {cat['strict_unit_test']}; {cat['any_json']} / "
            f"{cat['any_authorities_script']} / {cat['any_guardrails']}",
        ),
        (
            "Per-file coverage floors on `_web/`",
            "n/a (tomllib unavailable)" if floors is None else str(floors),
        ),
    ]
    lines = ["| Measure | Value |", "|---|---|"]
    lines.extend(f"| {label} | {value} |" for label, value in rows)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="emit the measurement dict as JSON")
    args = parser.parse_args(argv)
    measurements = measure()
    if args.json:
        print(json.dumps(measurements, indent=2, sort_keys=True))
    else:
        print(format_markdown(measurements))
    return 0


if __name__ == "__main__":
    sys.exit(main())
