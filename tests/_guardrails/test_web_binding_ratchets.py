"""P9.4 ratchets for the web binding table and the ``_web/`` package shape.

Three shrink-only guards, each pinned at a *measured* value on the PR that
installed it (the P9.4a custom-row core), so that later P9.4b/P9.2 slices can
only move them in the direction the plan requires:

1. **Residual composites.** ``RESIDUAL_COMPOSITE_CEILING`` is the number of
   supported operations that are *not* single-native codec rows — the
   ``CustomBinding`` rows plus the still-handler-backed composites. P9.4b
   converts handlers into custom rows one domain at a time (the residual is
   unchanged) and every P9.2 hoist removes one (the residual shrinks). Per
   category the custom-row counts are exact literals that each PR updates as
   derivation; the *deferred-product* count must reach zero before any second
   backend is approved (plan, P9.4 acceptance criteria).

2. **Class size.** No class under ``src/notebooklm/_web/`` may exceed
   ``CLASS_BODY_LINE_CEILING`` body lines unless it is listed in
   ``OVERSIZED_CLASS_CEILINGS`` with its measured size; listed classes may only
   shrink, and an entry is removed once the class is under the ceiling.

3. **Workflow shape.** Below the port, only the custom section may sequence
   more than one transport call: an ``async def`` under ``_web/`` (excluding
   the transport modules) that reaches two or more transport sites — directly
   (``_rpc_call``/``rpc_call``, ``_transport.call``/``stream``,
   ``invoke.call``/``stream``) or through one level of ``self.<helper>()``
   calls into a sibling method that has such a site — must be a function bound
   as ``handler=`` of a module-level ``CustomBinding`` or be listed in
   ``MULTI_CALL_HANDLER_ALLOWLIST``, whose entries only disappear.

Burndown: the plan's P9.4 exit report re-runs the Measurements table; the
allowlists here are the per-slice checklist for that report.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from notebooklm._binding import CodecBinding, CustomBinding
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.registry import WEB_OPERATION_REGISTRY

pytestmark = pytest.mark.repo_lint

WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_web"

# --- 1. residual composites ---------------------------------------------------

#: custom rows + handler-backed operations at P9.4a; shrinks with every hoist.
#: Sharing, artifact-rename, and notebook create/update hoists removed six
#: custom rows; the label- and collection-create hoists removed the final two
#: handlers from the P9.2 stop/go baseline.
RESIDUAL_COMPOSITE_CEILING = 20
#: Exact custom-row counts per justification category: five source-add rows,
#: ``CHAT_ASK``, the Studio
#: generation/prompt rows and the notebook/mind-map/catalog composites.
#: P9.4b PRs raise these as handlers convert;
#: P9.2 hoists lower ``deferred-product``, which must reach zero before any
#: second backend.
CUSTOM_ROW_COUNTS = {"protocol": 5, "compatibility": 4, "deferred-product": 11}

# --- 2. class size ---------------------------------------------------------------

CLASS_BODY_LINE_CEILING = 500
#: Measured at P9.4a; shrink-only. ``WebExecutionRuntime`` is the transport engine
#: and shrinks on its own schedule; the remaining chain classes are P9.4b targets
#: (``WebRpcBackend`` dropped under the ceiling with the P9.4b notebook/mind-map rows).
OVERSIZED_CLASS_CEILINGS = {
    "runtime.py:WebExecutionRuntime": 597,
}

# --- 3. workflow shape ---------------------------------------------------------

TRANSPORT_MODULES = frozenset({"runtime.py", "transport.py", "chat_transport.py"})
_DIRECT_VERBS = frozenset({"_rpc_call", "rpc_call"})
_OWNER_VERBS = {
    "_transport": frozenset({"call", "stream"}),
    "invoke": frozenset({"call", "stream"}),
}
#: ``file.py:Class.method`` handler-backed composites that still sequence more
#: than one transport call; each P9.4b PR removes the entries it converts.
MULTI_CALL_HANDLER_ALLOWLIST = frozenset()


def _attribute_parts(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _is_transport_site(call: ast.Call) -> bool:
    parts = _attribute_parts(call.func)
    if not parts:
        return False
    if parts[-1] in _DIRECT_VERBS:
        return True
    return len(parts) >= 2 and parts[-1] in _OWNER_VERBS.get(parts[-2], frozenset())


def _self_helpers(fn: ast.AST) -> set[str]:
    return {
        parts[1]
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and len(parts := _attribute_parts(node.func)) == 2
        and parts[0] == "self"
        and parts[1] not in _DIRECT_VERBS
    }


def _iter_web_modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(WEB_ROOT.rglob("*.py")):
        yield (
            path.relative_to(WEB_ROOT).as_posix(),
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )


def measure_class_sizes() -> dict[str, int]:
    return {
        f"{relative}:{node.name}": node.end_lineno - node.lineno + 1
        for relative, tree in _iter_web_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.end_lineno is not None
    }


def _collect_functions(
    tree: ast.Module,
) -> tuple[dict[str, int], dict[str, set[str]], set[str]]:
    """Return ``(direct transport sites, self helpers, async qualnames)`` per function."""
    direct: dict[str, int] = {}
    helpers: dict[str, set[str]] = {}
    asyncs: set[str] = set()
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        parent, owner = pending.pop()
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, ast.ClassDef):
                pending.append((child, child.name))
            elif isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)):
                qualname = f"{owner}.{child.name}" if owner else child.name
                direct[qualname] = sum(
                    1 for n in ast.walk(child) if isinstance(n, ast.Call) and _is_transport_site(n)
                )
                helpers[qualname] = _self_helpers(child)
                if isinstance(child, ast.AsyncFunctionDef):
                    asyncs.add(qualname)
            else:
                pending.append((child, owner))
    return direct, helpers, asyncs


def measure_multi_call_functions() -> tuple[set[str], set[str]]:
    """Return ``(multi-call async functions, custom-row handlers)`` as ``file:qualname``."""
    multi: set[str] = set()
    custom_handlers: set[str] = set()
    for relative, tree in _iter_web_modules():
        if relative.rsplit("/", 1)[-1] in TRANSPORT_MODULES:
            continue
        for node in tree.body:
            value = getattr(node, "value", None)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Call):
                if _attribute_parts(value.func)[-1:] == ("CustomBinding",):
                    for keyword in value.keywords:
                        if keyword.arg == "handler" and isinstance(keyword.value, ast.Name):
                            custom_handlers.add(f"{relative}:{keyword.value.id}")
        direct, helpers, asyncs = _collect_functions(tree)
        for qualname in asyncs:
            owner = qualname.rsplit(".", 1)[0] if "." in qualname else ""
            total = direct[qualname] + sum(
                direct.get(f"{owner}.{helper}", 0)
                for helper in helpers[qualname]
                if f"{owner}.{helper}" != qualname
            )
            if total > 1:
                multi.add(f"{relative}:{qualname}")
    return multi, custom_handlers


def _custom_rows() -> list[CustomBinding]:
    return [row for row in WEB_BINDING_ROWS.values() if isinstance(row, CustomBinding)]


def test_residual_composites_only_shrink() -> None:
    custom = _custom_rows()
    handler_backed = [
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported and binding.handler_name is not None
    ]
    residual = len(custom) + len(handler_backed)
    assert residual <= RESIDUAL_COMPOSITE_CEILING, (
        f"residual composites grew to {residual} (ceiling {RESIDUAL_COMPOSITE_CEILING}); "
        "a supported operation must be a single-native codec row or a justified custom row"
    )
    assert residual == RESIDUAL_COMPOSITE_CEILING, (
        f"residual composites shrank to {residual}; tighten RESIDUAL_COMPOSITE_CEILING"
    )
    counts = dict.fromkeys(CUSTOM_ROW_COUNTS, 0)
    for row in custom:
        counts[row.category] += 1
    assert counts == CUSTOM_ROW_COUNTS, (
        f"custom-row counts per category changed to {counts}; update CUSTOM_ROW_COUNTS as "
        "derivation (deferred-product must reach zero before any second backend)"
    )
    for row in custom:
        assert row.justification.strip(), f"{row.definition.key.value} lacks a justification"
        assert row.category in CUSTOM_ROW_COUNTS
    codec = sum(1 for row in WEB_BINDING_ROWS.values() if isinstance(row, CodecBinding))
    assert codec + residual == len([b for b in WEB_OPERATION_REGISTRY.values() if b.is_supported])


def test_web_classes_stay_under_the_body_line_ceiling() -> None:
    sizes = measure_class_sizes()
    offenders = {
        name: size
        for name, size in sizes.items()
        if size > CLASS_BODY_LINE_CEILING and name not in OVERSIZED_CLASS_CEILINGS
    }
    assert not offenders, (
        f"_web/ classes over {CLASS_BODY_LINE_CEILING} body lines without an allowlist "
        f"entry: {offenders}"
    )
    for name, ceiling in OVERSIZED_CLASS_CEILINGS.items():
        assert name in sizes, f"allowlisted class no longer exists: {name}"
        size = sizes[name]
        assert size <= ceiling, f"{name} grew to {size} body lines (ceiling {ceiling})"
        if size <= CLASS_BODY_LINE_CEILING:
            pytest.fail(f"{name} is under the ceiling ({size}); remove its allowlist entry")
        assert size == ceiling, f"{name} shrank to {size}; tighten its ceiling from {ceiling}"


def test_only_the_custom_section_sequences_transport_calls() -> None:
    multi, custom_handlers = measure_multi_call_functions()
    unexplained = multi - custom_handlers - MULTI_CALL_HANDLER_ALLOWLIST
    assert not unexplained, (
        "async functions under _web/ sequencing more than one transport call outside the "
        f"custom section: {sorted(unexplained)}"
    )
    stale = MULTI_CALL_HANDLER_ALLOWLIST - multi
    assert not stale, f"allowlisted handlers no longer sequence >1 call; remove: {sorted(stale)}"
