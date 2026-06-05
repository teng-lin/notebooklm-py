"""Meta-lint: external code may not access retired ``Session`` private attrs.

The names enumerated in :data:`FORBIDDEN_PROPERTIES` were once private
properties on the concrete session type that delegated to per-seam
collaborators. The session-shrink arc retired those properties
and migrated tests to the owning collaborators or ``make_fake_core(...)``.
This lint is now a strict regression guard; :data:`ALLOWLIST` must stay
empty.

Lint shape (modeled after :mod:`tests._guardrails.test_no_core_imports`):

* ``ast.parse`` + ``ast.walk`` (true AST, no regex).
* Catches :class:`ast.Attribute` nodes in ``Load`` + ``Store`` + ``Del``
  contexts, plus :class:`ast.AugAssign` whose target is an Attribute
  matching the bridge set. So read, write, delete, and ``x._b += 1`` all
  fail.
* Catches the constant-string forms of dynamic attribute access:
  ``getattr(obj, "_bridge")``, ``setattr(obj, "_bridge", v)``, and
  ``delattr(obj, "_bridge")``. Computed attribute names (e.g.
  ``getattr(obj, name)`` with a runtime ``name``) are NOT caught — that
  would require type/dataflow analysis. Code that wants to evade the
  gate via ``vars(obj)["_bridge"]`` / ``obj.__dict__["_bridge"]`` /
  ``object.__getattribute__(obj, "_bridge")`` is also out of scope; the
  practical answer is that nothing in the current test suite uses those
  shapes for bridge access, and if a future evasion appears the fix is
  to extend :func:`scan_code` with the appropriate AST branch.
* Files in :data:`CARVE_OUT_MODULES` are skipped — they are the seam
  modules themselves and legitimately store the underscore-prefixed
  attributes on their own instances.
* :data:`ALLOWLIST` is intentionally empty. Adding a new exception means
  re-opening the retired bridge policy and must fail this lint.

The four AST contexts (Load / Store / Del / AugAssign) plus the three
dynamic forms (``getattr`` / ``setattr`` / ``delattr``) each get a
parametrized negative test below so the lint proves its own coverage in
the same file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# Retired Session-private attribute names. Keep this set even after the
# properties are gone so tests cannot quietly reintroduce those reach-ins.
FORBIDDEN_PROPERTIES: frozenset[str] = frozenset(
    {
        # ClientLifecycle bridges retired in session-shrink PR 6 — readers
        # now go straight to ``session._kernel`` / ``session._lifecycle``
        # and call ``session._lifecycle.get_bound_loop()`` for the open-time
        # loop (the ``session.bound_loop`` property forward was deleted in
        # Wave 11c of session-decoupling). Lifecycle bridge names
        # (``_http_client``, ``_bound_loop``, ``_timeout``, ``_connect_timeout``,
        # ``_limits``, ``_keepalive_interval``, ``_keepalive_task``) are
        # intentionally NOT listed below: ``Session`` no longer defines them,
        # so any stray reference raises ``AttributeError`` at access time —
        # no lint guard required.
        # AuthRefreshCoordinator bridges retired in session-shrink PR 5 —
        # readers (and the two ``_refresh_callback`` writers in
        # ``tests/integration/test_session_integration.py``) now go straight
        # to ``session._auth_coord.<attr>``. Names intentionally NOT listed
        # so the lint no longer flags legitimate direct-coordinator reads
        # in ``test_runtime_auth.py`` (and the unrelated ``owner._refresh_callback``
        # attribute used by the fake ``_Owner`` in ``test_rpc_executor.py`` /
        # ``test_idempotency_registry.py``).
        # Observability (ClientMetrics + TransportDrainTracker) bridges
        # retired in session-shrink PR 4 — readers now go straight to
        # ``session._metrics_obj.<attr>`` / ``session._drain_tracker.<attr>``
        # or the lock-safe ``session.metrics_snapshot()``. The names are
        # intentionally NOT listed here so the lint no longer flags the
        # legitimate direct-collaborator reads in tests like
        # ``test_client_metrics.py`` / ``test_transport_drain.py`` that
        # exercise the helpers in isolation.
        "_loaded_cookie_snapshot",
        "_pending_polls",
        "_reqid_counter",
        "_save_lock",
    }
)


# ``getattr`` / ``setattr`` / ``delattr`` — the three builtins that
# constant-string-dispatch an attribute access. Each is caught when
# called with a constant-string second arg matching
# :data:`FORBIDDEN_PROPERTIES`.
_DYNAMIC_ACCESS_BUILTINS: frozenset[str] = frozenset({"getattr", "setattr", "delattr"})


# Source modules that legitimately store these underscore-prefixed
# attributes on their own instances. The lint MUST NOT flag self-reads
# inside these modules. The set is defensive — the test-suite scan
# below is currently scoped to ``tests/``, but if a future PR widens the
# scope to ``src/`` the carve-out keeps the seam modules clean. Adding
# the wrong entry here is a no-op for the test-suite lint; missing an
# entry only matters once the scope widens.
CARVE_OUT_MODULES: frozenset[str] = frozenset(
    {
        "src/notebooklm/_session.py",
        "src/notebooklm/_kernel.py",
        "src/notebooklm/_runtime/lifecycle.py",
        "src/notebooklm/_runtime/auth.py",
        "src/notebooklm/_client_metrics.py",
        "src/notebooklm/_transport_drain.py",
        "src/notebooklm/_cookie_persistence.py",
        "src/notebooklm/_polling_registry.py",
        "src/notebooklm/_reqid_counter.py",
        "src/notebooklm/_rpc_executor.py",
        "src/notebooklm/_request_types.py",
        "src/notebooklm/_streaming_post.py",
        "src/notebooklm/_transport_errors.py",
        "src/notebooklm/_middleware/auth_refresh.py",
        "src/notebooklm/_middleware/chain.py",
        "src/notebooklm/_middleware/drain.py",
        "src/notebooklm/_middleware/error_injection.py",
        "src/notebooklm/_middleware/metrics.py",
        "src/notebooklm/_middleware/retry.py",
        "src/notebooklm/_middleware/semaphore.py",
        "src/notebooklm/_middleware/tracing.py",
    }
)


# No transitional exceptions remain.
ALLOWLIST: list[str] = []


def _augassign_target_attrs(tree: ast.AST) -> set[int]:
    """Return ``id(attr_node)`` for every AugAssign target that is an Attribute.

    ``ast.AugAssign.target`` is an :class:`ast.Attribute` when the
    augmented-assign LHS is an attribute (``c._x += 1``). The same node
    also appears in the broader walk with a ``Store`` context; we tag
    AugAssign separately so the failure message is precise.
    """
    return {
        id(node.target)
        for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute)
    }


def _dynamic_access_attr(call: ast.Call) -> str | None:
    """Return the forbidden attribute name if ``call`` is a constant-string
    ``getattr`` / ``setattr`` / ``delattr`` on the bridge set; ``None`` otherwise.

    Only the builtin form (``Name`` callable, not ``builtins.getattr``) and
    constant-string second arg are recognised; computed names like
    ``getattr(obj, name)`` are out of scope (see module docstring).
    """
    if not isinstance(call.func, ast.Name) or call.func.id not in _DYNAMIC_ACCESS_BUILTINS:
        return None
    if len(call.args) < 2:
        return None
    second = call.args[1]
    if not isinstance(second, ast.Constant) or not isinstance(second.value, str):
        return None
    return second.value if second.value in FORBIDDEN_PROPERTIES else None


def scan_code(source: str) -> list[tuple[int, str, str]]:
    """Return ``[(lineno, attr_name, context), ...]`` for forbidden bridge access.

    Public so the carve-out routing can be tested independently from the
    file-walk that drives the main test. ``context`` is one of
    ``Load``, ``Store``, ``Del``, ``AugAssign``, ``getattr``, ``setattr``,
    ``delattr``.
    """
    tree = ast.parse(source)
    augassign_ids = _augassign_target_attrs(tree)
    violations: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_PROPERTIES:
            if id(node) in augassign_ids:
                ctx = "AugAssign"
            elif isinstance(node.ctx, ast.Load):
                ctx = "Load"
            elif isinstance(node.ctx, ast.Store):
                ctx = "Store"
            elif isinstance(node.ctx, ast.Del):
                ctx = "Del"
            else:  # pragma: no cover - exhaustive over ast.expr_context subclasses
                continue
            violations.append((node.lineno, node.attr, ctx))
            continue
        if isinstance(node, ast.Call):
            attr = _dynamic_access_attr(node)
            if attr is not None:
                assert isinstance(node.func, ast.Name)  # narrowed by _dynamic_access_attr
                violations.append((node.lineno, attr, node.func.id))
    return violations


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    return scan_code(path.read_text(encoding="utf-8"))


def is_carve_out(rel_path: str) -> bool:
    """Return ``True`` if ``rel_path`` is a seam module exempt from the lint."""
    return rel_path in CARVE_OUT_MODULES


_SELF_PATH = Path(__file__).resolve()


def _collect_test_files() -> list[Path]:
    """Every ``.py`` under ``tests/``, sorted, excluding caches and the lint itself."""
    test_root = REPO_ROOT / "tests"
    return sorted(
        p
        for p in test_root.rglob("*.py")
        if "__pycache__" not in p.parts and p.resolve() != _SELF_PATH
    )


def test_allowlist_is_empty() -> None:
    """The bridge-retirement arc is closed; no test-file exceptions remain."""
    assert ALLOWLIST == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Direct attribute access — the four AST contexts.
        ("def f(c):\n    return c._save_lock\n", [(2, "_save_lock", "Load")]),
        ("def f(c):\n    c._save_lock = None\n", [(2, "_save_lock", "Store")]),
        ("def f(c):\n    del c._save_lock\n", [(2, "_save_lock", "Del")]),
        (
            "def f(c):\n    c._reqid_counter += 1\n",
            [(2, "_reqid_counter", "AugAssign")],
        ),
        # Sample a second bridge name so the parametrized suite does not
        # accidentally depend on one attribute.
        (
            "def f(c):\n    return c._pending_polls\n",
            [(2, "_pending_polls", "Load")],
        ),
        # Dynamic-access builtins with constant-string second arg.
        (
            'def f(c):\n    return getattr(c, "_save_lock")\n',
            [(2, "_save_lock", "getattr")],
        ),
        (
            'def f(c):\n    setattr(c, "_save_lock", None)\n',
            [(2, "_save_lock", "setattr")],
        ),
        (
            'def f(c):\n    delattr(c, "_save_lock")\n',
            [(2, "_save_lock", "delattr")],
        ),
    ],
    ids=[
        "load",
        "store",
        "del",
        "augassign",
        "save_lock_load",
        "getattr",
        "setattr",
        "delattr",
    ],
)
def test_linter_catches_each_context(source: str, expected: list[tuple[int, str, str]]) -> None:
    """Each of the AST + dynamic contexts must be caught."""
    assert scan_code(source) == expected


def test_linter_does_not_flag_non_forbidden_attrs() -> None:
    """Attribute access unrelated to the bridge set must NOT be flagged."""
    source = "def f(c):\n    return c.public_attr + c._private_helper + c.some_method()\n"
    assert scan_code(source) == []


@pytest.mark.parametrize(
    "source",
    [
        # Non-forbidden attribute name → not flagged.
        'def f(c):\n    return getattr(c, "public_attr")\n',
        # Computed second arg → out of scope, not flagged.
        "def f(c, name):\n    return getattr(c, name)\n",
        # Single-arg getattr (hasattr-style) → not flagged.
        "def f(c):\n    return getattr(c)\n",
        # Method named getattr → not the builtin.
        'def f(c):\n    return c.getattr("_save_lock")\n',
    ],
    ids=[
        "non_forbidden_string",
        "computed_attr_name",
        "single_arg_getattr",
        "method_named_getattr",
    ],
)
def test_linter_dynamic_access_known_negatives(source: str) -> None:
    """Cases that look like dynamic access but must NOT be flagged.

    Documents the known limits: computed attribute names slip the gate
    (intentional — requires dataflow analysis), and method-named
    ``getattr`` is not the builtin.
    """
    assert scan_code(source) == []


def test_is_carve_out_for_runtime_lifecycle() -> None:
    """Sanity: the canonical lifecycle filename is ``_runtime/lifecycle.py``."""
    assert is_carve_out("src/notebooklm/_runtime/lifecycle.py"), (
        "_runtime/lifecycle.py must be in CARVE_OUT_MODULES."
    )
    assert not is_carve_out("src/notebooklm/_lifecycle.py"), (
        "There is no _lifecycle.py — the canonical name is _runtime/lifecycle.py. "
        "If the carve-out lists the wrong name, fix it."
    )


def test_no_session_compat_bridges() -> None:
    """Every test file outside CARVE_OUT_MODULES must be clean."""
    failures: list[str] = []
    for path in _collect_test_files():
        # Use as_posix() so failure output is stable on Windows, where
        # ``str(Path)`` produces backslashes.
        rel = path.relative_to(REPO_ROOT).as_posix()
        if is_carve_out(rel):
            continue
        violations = _scan_file(path)
        for lineno, attr, ctx in violations:
            failures.append(f"{rel}:{lineno} {ctx} {attr}")

    assert not failures, (
        "Test files may not access retired Session private attributes. Use "
        "make_fake_core(...) for lightweight cores, or target the owning "
        "collaborator directly when a collaborator invariant is under test.\n\n"
        "Violations:\n" + "\n".join(f"  - {f}" for f in failures)
    )
