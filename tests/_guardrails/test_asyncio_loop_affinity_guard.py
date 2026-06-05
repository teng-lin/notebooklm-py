"""Loop-affinity invariant guard for ``asyncio`` synchronisation primitives.

The #1196 class of bug: a lazily-constructed ``asyncio`` primitive
(``Lock`` / ``Semaphore`` / ``Event`` / ``Condition``) is created the first
time it is touched, which binds it to *whatever event loop is running at that
moment*. If the owning client is closed and reopened on a **different** loop,
reusing the stale primitive either raises "bound to a different event loop"
(Python 3.10/3.11) or misparks waiters. The fix that landed in #1196 (and its
siblings) is a uniform loop-affinity protocol on the owning class:

* ``set_bound_loop(loop)`` — ``ClientLifecycle.open()`` captures the running
  loop and propagates it to every collaborator so a cross-loop call can be
  rejected at the call site.
* ``reset_after_open()`` — discards the cached primitive so the next access
  from inside the new loop rebuilds it on that loop.

This lint enumerates **every** ``asyncio`` primitive construction site under
``src/notebooklm/`` (via AST, so docstring mentions don't count) and asserts
that the construction site is *guarded*: either the owning class exposes the
``set_bound_loop`` + ``reset_after_open`` protocol, or the site is on a
documented allowlist with a reason (and, for known follow-up gaps, a
tracking-issue reference).

Without this guard, a sibling primitive added later silently regresses the
#1196 class: nothing fails until a user reopens a client on a fresh loop in
production. The lint fails loudly the moment a new unguarded primitive lands.

Modelled after the AST-based lints in ``tests/_guardrails/`` (e.g.
``test_error_handler_allowlist.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# ``asyncio`` synchronisation primitives whose construction binds to the
# running event loop. ``Queue`` is intentionally excluded — the codebase does
# not construct loop-bound ``asyncio.Queue`` instances on a lazy path today;
# if one is added, extend this set and add it to the scan deliberately.
LOOP_BOUND_PRIMITIVES = frozenset({"Lock", "Semaphore", "BoundedSemaphore", "Event", "Condition"})

# Methods that together make up the canonical #1196 loop-affinity protocol.
# A class is considered compliant when it defines BOTH.
REQUIRED_GUARD_METHODS = ("set_bound_loop", "reset_after_open")

# The template-method base that supplies ``set_bound_loop`` (and the
# ``_bound_loop`` field) to its subclasses. A class inheriting this provides the
# ``set_bound_loop`` half of the protocol by inheritance, not by a direct
# ``def`` — the AST method scan must credit it so the owners that dropped their
# duplicated ``set_bound_loop`` body in favour of the mixin stay compliant. They
# still must define ``reset_after_open`` directly (each resets distinct
# owner-specific state, intentionally not unified into the base).
LOOP_BOUND_BASE = "LoopBoundPrimitive"


class _AllowlistEntry:
    """A documented exemption for one primitive construction site.

    Keyed by ``(relative-posix-path, owning-class-or-None)`` so it survives
    line-number churn from rebases and reorderings. Every entry carries a
    human reason; follow-up gaps additionally carry a tracking issue.
    """

    __slots__ = ("path", "owner", "reason", "issue")

    def __init__(
        self,
        path: str,
        owner: str | None,
        reason: str,
        issue: int | None = None,
    ) -> None:
        self.path = path
        self.owner = owner
        self.reason = reason
        self.issue = issue

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.path, self.owner)


# ---------------------------------------------------------------------------
# Allowlist — documented exemptions from the owner-level protocol.
#
# Each entry is a primitive whose construction site is loop-safe by an
# ALTERNATIVE documented mechanism, or a known follow-up gap with a tracking
# issue. The lint asserts exact membership: an allowlisted site that no longer
# constructs a primitive is reported as stale so the list keeps tightening.
# ---------------------------------------------------------------------------
ALLOWLIST: tuple[_AllowlistEntry, ...] = (
    # NOTE: ``ClientComposed``, ``TransportDrainTracker``,
    # ``SourceUploadPipeline``, and ``ChatAPI`` are NOT allowlisted — they
    # inherit ``set_bound_loop`` from ``LoopBoundPrimitive`` (credited by the
    # base-class check in ``_class_methods``) and each still define
    # ``reset_after_open`` directly, so the owner-method scan detects them as
    # compliant.
    #
    # ``set_bound_loop`` only (no ``reset_after_open``): the lazy ``asyncio.Lock``
    # is rebuilt implicitly because these coordinators are reconstructed per
    # ``open()`` and the call-site ``assert_bound_loop(self._bound_loop)`` in
    # ``await_refresh`` / ``snapshot`` rejects cross-loop misuse before the
    # lazy lock is touched. ``set_bound_loop(None)`` on close clears the
    # binding so the next ``open()`` rebinds. A ``reset_after_open`` would be
    # a no-op here (the locks are never held across ``open()``).
    _AllowlistEntry(
        "src/notebooklm/_runtime/auth.py",
        "AuthRefreshCoordinator",
        "Guarded by set_bound_loop + call-site assert_bound_loop; the lazy "
        "Lock is never held across open() so reset_after_open is unnecessary.",
    ),
    _AllowlistEntry(
        "src/notebooklm/_reqid_counter.py",
        "ReqidCounter",
        "Guarded by set_bound_loop + call-site assert_bound_loop in "
        "next_reqid; the lazy Lock is never held across open().",
    ),
    # Module-global, PER-RUNNING-LOOP registries: the lock is keyed by
    # ``asyncio.get_running_loop()`` in a ``WeakKeyDictionary``, so every loop
    # gets its own lock and a stale cross-loop primitive can never be reused.
    # These have no enclosing class to host the protocol; the per-loop keying
    # is the structural guard.
    _AllowlistEntry(
        "src/notebooklm/_auth/keepalive.py",
        None,
        "Module-global per-running-loop lock registry (keyed by "
        "asyncio.get_running_loop()); structurally immune to cross-loop reuse.",
    ),
    _AllowlistEntry(
        "src/notebooklm/_auth/refresh.py",
        None,
        "Module-global per-running-loop lock registry (keyed by "
        "asyncio.get_running_loop()); structurally immune to cross-loop reuse.",
    ),
)

_ALLOWLIST_BY_KEY = {entry.key: entry for entry in ALLOWLIST}


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_primitive_construction(
    node: ast.AST,
    asyncio_alias: str,
    imported_primitives: dict[str, str],
) -> str | None:
    """Return the primitive name if *node* constructs an ``asyncio`` primitive.

    Handles all three import styles so a primitive can't bypass the lint by
    importing differently:

    * ``asyncio.Lock()`` — attribute access on the module (possibly aliased
      via ``import asyncio as aio``; ``asyncio_alias`` carries the local name).
    * ``Lock()`` / ``L()`` — a name brought in by
      ``from asyncio import Lock`` / ``from asyncio import Lock as L``
      (``imported_primitives`` maps the local name to the canonical primitive).
    """
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node.func)
    prefix = f"{asyncio_alias}."
    if name.startswith(prefix):
        leaf = name.rsplit(".", 1)[-1]
        return leaf if leaf in LOOP_BOUND_PRIMITIVES else None
    if name in imported_primitives:
        return imported_primitives[name]
    return None


class _ConstructionSite:
    __slots__ = ("path", "lineno", "primitive", "owner", "owner_line")

    def __init__(
        self,
        path: str,
        lineno: int,
        primitive: str,
        owner: str | None,
        owner_line: int | None,
    ) -> None:
        self.path = path
        self.lineno = lineno
        self.primitive = primitive
        # ``owner`` is the class *name* (used for allowlist keying + display);
        # ``owner_line`` is its start line (used for the methods lookup so
        # same-named classes in different scopes don't collide).
        self.owner = owner
        self.owner_line = owner_line

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.path, self.owner)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        owner = self.owner or "<module>"
        return f"{self.path}:{self.lineno} asyncio.{self.primitive} (owner={owner})"


def _inherits_loop_bound_base(node: ast.ClassDef) -> bool:
    """Return ``True`` if *node* lists :data:`LOOP_BOUND_BASE` among its bases.

    Matches both ``class C(LoopBoundPrimitive)`` (a bare ``Name``) and a
    dotted/aliased reference ``class C(mod.LoopBoundPrimitive)`` (an
    ``Attribute``) so an owner can't bypass the credit by importing the base
    under a module-qualified path.
    """
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == LOOP_BOUND_BASE:
            return True
        if isinstance(base, ast.Attribute) and base.attr == LOOP_BOUND_BASE:
            return True
    return False


def _class_methods(module: ast.Module) -> dict[int, set[str]]:
    """Map each class's *start line* to the methods it provides.

    Keyed by start line (unique within a file) rather than by class name so two
    same-named classes in different scopes (e.g. a helper ``_State`` nested in
    two separate outer classes) don't collide and silently misreport
    compliance.

    A class inheriting :data:`LOOP_BOUND_BASE` is credited with
    ``set_bound_loop`` even though it no longer defines it directly: the
    template-method base supplies that half of the protocol. ``reset_after_open``
    is *not* credited by inheritance — each owner must still define it directly.
    """
    methods: dict[int, set[str]] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            provided = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if _inherits_loop_bound_base(node):
                provided.add("set_bound_loop")
            methods[node.lineno] = provided
    return methods


def _enclosing_class(
    class_ranges: list[tuple[int, int, str]], lineno: int
) -> tuple[str, int] | None:
    """Return ``(name, start_line)`` of the innermost class spanning *lineno*."""
    best: tuple[int, int, str] | None = None
    for start, end, name in class_ranges:
        if start <= lineno <= end and (best is None or start > best[0]):
            best = (start, end, name)
    return (best[2], best[0]) if best else None


def _asyncio_import_bindings(module: ast.Module) -> tuple[str, dict[str, str]]:
    """Resolve how ``asyncio`` and its primitives are bound in *module*.

    Returns ``(asyncio_alias, imported_primitives)`` where ``asyncio_alias`` is
    the local name for ``import asyncio[ as X]`` (default ``"asyncio"``) and
    ``imported_primitives`` maps each local name introduced by
    ``from asyncio import Primitive[ as Y]`` to the canonical primitive name.
    """
    asyncio_alias = "asyncio"
    imported_primitives: dict[str, str] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    asyncio_alias = alias.asname or "asyncio"
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in LOOP_BOUND_PRIMITIVES:
                    imported_primitives[alias.asname or alias.name] = alias.name
    return asyncio_alias, imported_primitives


def _scan() -> tuple[list[_ConstructionSite], dict[str, dict[int, set[str]]]]:
    sites: list[_ConstructionSite] = []
    methods_by_file: dict[str, dict[int, set[str]]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        methods_by_file[rel] = _class_methods(module)
        class_ranges = [
            (node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, node.name)
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef)
        ]
        asyncio_alias, imported_primitives = _asyncio_import_bindings(module)
        for node in ast.walk(module):
            primitive = _is_primitive_construction(node, asyncio_alias, imported_primitives)
            if primitive is None:
                continue
            enclosing = _enclosing_class(class_ranges, node.lineno)
            owner = enclosing[0] if enclosing else None
            owner_line = enclosing[1] if enclosing else None
            sites.append(_ConstructionSite(rel, node.lineno, primitive, owner, owner_line))
    return sites, methods_by_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_asyncio_primitive_is_loop_affinity_guarded() -> None:
    """Every lazy ``asyncio`` primitive is guarded or explicitly allowlisted."""
    sites, methods_by_file = _scan()

    assert sites, "scan found no asyncio primitives — the AST walk likely broke"

    violations: list[str] = []
    for site in sites:
        file_methods = methods_by_file.get(site.path, {})
        owner_methods = (
            file_methods.get(site.owner_line, set()) if site.owner_line is not None else set()
        )
        compliant = site.owner is not None and all(
            method in owner_methods for method in REQUIRED_GUARD_METHODS
        )
        if compliant:
            continue
        if site.key in _ALLOWLIST_BY_KEY:
            continue
        owner = site.owner or "<module-level>"
        missing = [m for m in REQUIRED_GUARD_METHODS if m not in owner_methods]
        violations.append(
            f"  {site.path}:{site.lineno}  asyncio.{site.primitive}  "
            f"(owner={owner}; missing {missing or 'class'}). "
            "Add set_bound_loop + reset_after_open to the owning class (the "
            "#1196 pattern), or add a documented allowlist entry."
        )

    if violations:
        raise AssertionError(
            "Unguarded asyncio synchronisation primitive(s) detected. Each lazy "
            "Lock/Semaphore/Event/Condition binds to the loop it is first built "
            "on; an owning class must expose the #1196 loop-affinity protocol "
            "(set_bound_loop + reset_after_open) or be allowlisted in "
            "tests/_guardrails/test_asyncio_loop_affinity_guard.py::ALLOWLIST.\n\n"
            + "\n".join(violations)
        )


def test_loop_affinity_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still correspond to a real primitive site."""
    sites, _ = _scan()
    live_keys = {site.key for site in sites}
    stale = sorted(
        f"  {entry.path} (owner={entry.owner or '<module-level>'})"
        for entry in ALLOWLIST
        if entry.key not in live_keys
    )
    if stale:
        raise AssertionError(
            "Stale loop-affinity allowlist entries (no matching primitive "
            "construction site found — remove from ALLOWLIST):\n" + "\n".join(stale)
        )


def test_loop_affinity_followup_entries_reference_a_tracking_issue() -> None:
    """Any known-gap allowlist entry (not alt-guarded) must cite a tracking issue.

    A *gap* entry is one that carries an ``issue`` (vs. an alternative-guard
    entry, which is documented by reason only). Every such entry must reference
    a positive issue number so the follow-up is trackable and the entry can be
    retired. Iterating (rather than hard-keying on a specific class) keeps the
    guard robust if a future gap class is renamed or relocated.

    There are intentionally **no** gap entries today. This test therefore
    validates the *shape* of any gap entry that lands in the future rather
    than requiring one to exist.
    """
    gap_entries = [entry for entry in ALLOWLIST if entry.issue is not None]
    for entry in gap_entries:
        assert isinstance(entry.issue, int) and entry.issue > 0, (
            f"gap entry for {entry.path} (owner={entry.owner}) must cite a "
            "positive tracking issue number."
        )


def _detect(source: str) -> set[str]:
    """Run the import-aware primitive detector over a synthetic module body."""
    module = ast.parse(source)
    asyncio_alias, imported = _asyncio_import_bindings(module)
    found: set[str] = set()
    for node in ast.walk(module):
        primitive = _is_primitive_construction(node, asyncio_alias, imported)
        if primitive is not None:
            found.add(primitive)
    return found


def test_detector_handles_all_import_styles() -> None:
    """``asyncio.Lock()``, aliased module, and ``from asyncio import`` all match."""
    assert _detect("import asyncio\nx = asyncio.Lock()\n") == {"Lock"}
    assert _detect("import asyncio as aio\nx = aio.Semaphore(1)\n") == {"Semaphore"}
    assert _detect("from asyncio import Lock\nx = Lock()\n") == {"Lock"}
    assert _detect("from asyncio import Event as E\nx = E()\n") == {"Event"}
    # A same-named symbol from an unrelated module must NOT match.
    assert _detect("from threading import Lock\nx = Lock()\n") == set()
    # Non-primitive asyncio constructs must NOT match.
    assert _detect("import asyncio\nx = asyncio.Queue()\n") == set()


def _first_classdef(source: str) -> ast.ClassDef:
    """Return the first ``ClassDef`` node parsed from *source*."""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ClassDef)
    return node


def test_inherits_loop_bound_base_credits_only_the_right_classes() -> None:
    """The base-class check matches bare + dotted bases and nothing else.

    Guards the inheritance credit in ``_class_methods``: if this regressed,
    the four mixin owners would silently lose their ``set_bound_loop`` credit
    and either be (wrongly) flagged or, worse, an unrelated class could be
    (wrongly) credited and an unguarded primitive could slip through.
    """
    # Bare ``class C(LoopBoundPrimitive)``.
    assert _inherits_loop_bound_base(_first_classdef("class C(LoopBoundPrimitive): pass"))
    # Dotted / module-qualified base.
    assert _inherits_loop_bound_base(_first_classdef("class C(mod.LoopBoundPrimitive): pass"))
    # Mixed bases — credited as long as the base appears anywhere.
    assert _inherits_loop_bound_base(_first_classdef("class C(Base, LoopBoundPrimitive): pass"))
    # No inheritance / unrelated base must NOT be credited.
    assert not _inherits_loop_bound_base(_first_classdef("class C: pass"))
    assert not _inherits_loop_bound_base(_first_classdef("class C(SomethingElse): pass"))
