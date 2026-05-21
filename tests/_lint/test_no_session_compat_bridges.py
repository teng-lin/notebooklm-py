"""Meta-lint: external code may not access ``Session`` compat-bridge attributes.

The bridges enumerated in :data:`FORBIDDEN_PROPERTIES` are properties on
:class:`notebooklm._session.Session` that delegate to per-seam collaborators
(``ClientLifecycle``, ``ClientMetrics``, ``AuthRefreshCoordinator``,
``CookiePersistence``, etc.). They exist as a back-compat shim for tests
that pre-date the seam extraction (see ADR-001 §Decision, ADR-007).

Retirement is staged as the session-shrink multi-PR arc (8 PRs landing
under the ``session-shrink/*`` branch prefix). This lint gates new
readers while the existing :data:`ALLOWLIST` drains to empty — by the
final demolition PR (ClientLifecycle), the allowlist should be empty
and the lint becomes a permanent guard against re-introducing the
pattern.

Lint shape (modeled after :mod:`tests._lint.test_no_core_imports`):

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
* Files in :data:`ALLOWLIST` are exempted transitionally; the list MUST
  be alphabetized (the self-test :func:`test_allowlist_is_sorted`
  enforces that, and the list is declared as a literal — *not*
  ``sorted([...])`` — so accidental drift causes the test to fail
  rather than be silently re-sorted at import time).

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


# Bridge attribute names — see :file:`src/notebooklm/_session.py`
# bridge block (the ``@property`` declarations between the auth-coord
# ``_ensure_*`` and the ``get_http_client`` tail). Update this set when
# a bridge is added or retired.
FORBIDDEN_PROPERTIES: frozenset[str] = frozenset(
    {
        # ClientLifecycle bridges
        "_http_client",
        "_bound_loop",
        "_timeout",
        "_keepalive_task",
        "_keepalive_interval",
        "_keepalive_storage_path",
        # AuthRefreshCoordinator bridges
        "_refresh_callback",
        "_refresh_task",
        "_refresh_lock",
        # Observability (ClientMetrics + TransportDrainTracker) bridges
        "_metrics",
        "_metrics_lock",
        "_on_rpc_event",
        "_in_flight_posts",
        "_draining",
        "_drain_condition",
        # CookiePersistence + PollingRegistry + ReqidCounter bridges
        "_save_lock",
        "_loaded_cookie_snapshot",
        "_pending_polls",
        "_reqid_counter",
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
        "src/notebooklm/_session_lifecycle.py",
        "src/notebooklm/_session_auth.py",
        "src/notebooklm/_client_metrics.py",
        "src/notebooklm/_transport_drain.py",
        "src/notebooklm/_cookie_persistence.py",
        "src/notebooklm/_polling_registry.py",
        "src/notebooklm/_reqid_counter.py",
        "src/notebooklm/_authed_transport.py",
        "src/notebooklm/_rpc_executor.py",
        "src/notebooklm/_middleware_auth_refresh.py",
        "src/notebooklm/_middleware_chain.py",
        "src/notebooklm/_middleware_drain.py",
        "src/notebooklm/_middleware_error_injection.py",
        "src/notebooklm/_middleware_metrics.py",
        "src/notebooklm/_middleware_retry.py",
        "src/notebooklm/_middleware_semaphore.py",
        "src/notebooklm/_middleware_tracing.py",
    }
)


# Test files that currently access ``Session`` compat bridges. This is
# the transitional allowlist; each session-shrink PR removes entries as
# its readers migrate, and the allowlist must be empty by the final
# demolition PR (see ``docs/architecture.md`` for the per-PR arc).
#
# DO NOT wrap in ``sorted([...])`` — the self-test
# :func:`test_allowlist_is_sorted` compares the literal against its
# sorted form, so any drift caused by an accidental out-of-order
# insertion fails the test instead of being silently re-sorted at
# import time.
ALLOWLIST: list[str] = [
    "tests/integration/concurrency/test_aexit_exception_masking.py",
    "tests/integration/concurrency/test_artifact_poll_dedupe.py",
    "tests/integration/concurrency/test_auth_snapshot_torn_read.py",
    "tests/integration/concurrency/test_chat_history_race.py",
    "tests/integration/concurrency/test_cross_loop_affinity.py",
    "tests/integration/concurrency/test_harness_smoke.py",
    "tests/integration/concurrency/test_idempotency_create.py",
    "tests/integration/concurrency/test_keepalive_path_canonicalize.py",
    "tests/integration/concurrency/test_max_concurrent_rpcs.py",
    "tests/integration/concurrency/test_note_create_cancel.py",
    "tests/integration/concurrency/test_rate_limit_default.py",
    "tests/integration/concurrency/test_refresh_cancellation_propagation.py",
    "tests/integration/test_artifact_generation_idempotency.py",
    "tests/integration/test_auth_refresh_vcr.py",
    "tests/integration/test_auto_refresh.py",
    "tests/integration/test_error_paths_vcr.py",
    "tests/integration/test_notes_idempotency.py",
    "tests/integration/test_research_idempotency.py",
    "tests/integration/test_session_integration.py",
    "tests/integration/test_side_effects_idempotency.py",
    "tests/integration/test_sources_idempotency.py",
    "tests/unit/concurrency/test_close_cancellation_leak.py",
    "tests/unit/concurrency/test_session_close_refresh_race.py",
    "tests/unit/conftest.py",
    "tests/unit/test_api_coverage.py",
    "tests/unit/test_artifacts_coverage.py",
    "tests/unit/test_auth_cookie_save_race.py",
    "tests/unit/test_auth_session.py",
    "tests/unit/test_authed_transport.py",
    "tests/unit/test_chat_ask_invariants.py",
    "tests/unit/test_client.py",
    "tests/unit/test_client_keepalive.py",
    "tests/unit/test_client_metrics.py",
    "tests/unit/test_cookie_persistence.py",
    "tests/unit/test_drain_middleware.py",
    "tests/unit/test_idempotency_registry.py",
    "tests/unit/test_logging_correlation.py",
    "tests/unit/test_metrics_middleware.py",
    "tests/unit/test_observability.py",
    "tests/unit/test_polling_registry.py",
    "tests/unit/test_quota_failure_detection.py",
    "tests/unit/test_rate_limit_retry.py",
    "tests/unit/test_refresh_lock_lazy_init.py",
    "tests/unit/test_refresh_state_machine.py",
    "tests/unit/test_rpc_executor.py",
    "tests/unit/test_rpc_overrides.py",
    "tests/unit/test_save_lock_contract.py",
    "tests/unit/test_session_auth.py",
    "tests/unit/test_session_close.py",
    "tests/unit/test_session_lifecycle.py",
    "tests/unit/test_session_reqid.py",
    "tests/unit/test_session_reqid_concurrent.py",
    "tests/unit/test_source_selection.py",
    "tests/unit/test_transport_drain.py",
    "tests/unit/test_vcr_config.py",
]


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


def test_allowlist_is_sorted() -> None:
    """:data:`ALLOWLIST` must be alphabetized so additions land in stable positions.

    The literal-list form (not ``sorted([...])``) is intentional: drift
    must fail the test rather than be silently fixed at import time.
    """
    assert sorted(ALLOWLIST) == ALLOWLIST, (
        "ALLOWLIST is not alphabetized. Re-sort the literal list in "
        "tests/_lint/test_no_session_compat_bridges.py so the diff is "
        "stable. Do NOT wrap in sorted([...])."
    )


def test_allowlist_entries_exist() -> None:
    """Every allowlisted path must still exist; otherwise drain the entry."""
    missing = [rel for rel in ALLOWLIST if not (REPO_ROOT / rel).is_file()]
    assert not missing, (
        "Allowlisted paths no longer exist; remove them from ALLOWLIST:\n"
        + "\n".join(f"  - {rel}" for rel in missing)
    )


def test_allowlist_entries_currently_violate() -> None:
    """Every allowlisted file must STILL access at least one bridge.

    This is the drain mechanism. As session-shrink PRs migrate test files
    off the bridges, the corresponding ALLOWLIST entry must be removed —
    otherwise the gate silently weakens. If this test fails, drop the
    listed file from ALLOWLIST; the lint will then enforce zero bridge
    access on it going forward.
    """
    clean = [rel for rel in ALLOWLIST if not _scan_file(REPO_ROOT / rel)]
    assert not clean, (
        "Allowlisted files no longer access any bridge; remove them from "
        "ALLOWLIST so the lint enforces zero bridge access going forward:\n"
        + "\n".join(f"  - {rel}" for rel in clean)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Direct attribute access — the four AST contexts.
        ("def f(c):\n    return c._http_client\n", [(2, "_http_client", "Load")]),
        ("def f(c):\n    c._http_client = None\n", [(2, "_http_client", "Store")]),
        ("def f(c):\n    del c._http_client\n", [(2, "_http_client", "Del")]),
        (
            "def f(c):\n    c._in_flight_posts += 1\n",
            [(2, "_in_flight_posts", "AugAssign")],
        ),
        # Newly-included bridge that was missed in the first revision.
        (
            "def f(c):\n    return c._metrics_lock\n",
            [(2, "_metrics_lock", "Load")],
        ),
        # Dynamic-access builtins with constant-string second arg.
        (
            'def f(c):\n    return getattr(c, "_http_client")\n',
            [(2, "_http_client", "getattr")],
        ),
        (
            'def f(c):\n    setattr(c, "_http_client", None)\n',
            [(2, "_http_client", "setattr")],
        ),
        (
            'def f(c):\n    delattr(c, "_http_client")\n',
            [(2, "_http_client", "delattr")],
        ),
    ],
    ids=[
        "load",
        "store",
        "del",
        "augassign",
        "metrics_lock_load",
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
        'def f(c):\n    return c.getattr("_http_client")\n',
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


def test_is_carve_out_for_session_lifecycle() -> None:
    """Sanity: the canonical lifecycle filename is ``_session_lifecycle.py``."""
    assert is_carve_out("src/notebooklm/_session_lifecycle.py"), (
        "_session_lifecycle.py must be in CARVE_OUT_MODULES."
    )
    assert not is_carve_out("src/notebooklm/_lifecycle.py"), (
        "There is no _lifecycle.py — the canonical name is _session_lifecycle.py. "
        "If the carve-out lists the wrong name, fix it."
    )


def test_no_session_compat_bridges() -> None:
    """Every test file outside ALLOWLIST + CARVE_OUT_MODULES must be clean."""
    failures: list[str] = []
    for path in _collect_test_files():
        # Use as_posix() so the comparison against ALLOWLIST (forward-slash
        # POSIX paths) is correct on Windows, where ``str(Path)`` produces
        # backslashes that miss every allowlist entry.
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWLIST or is_carve_out(rel):
            continue
        violations = _scan_file(path)
        for lineno, attr, ctx in violations:
            failures.append(f"{rel}:{lineno} {ctx} {attr}")

    assert not failures, (
        "Test files outside ALLOWLIST may not access Session compat bridges. "
        "If the file legitimately needs access during the migration, add it to "
        "ALLOWLIST (alphabetized) in this file. If it can be migrated to "
        "make_fake_core(...) (see ADR-007), do that instead. Each "
        "session-shrink PR drains entries until the list is empty.\n\n"
        "Violations:\n" + "\n".join(f"  - {f}" for f in failures)
    )
