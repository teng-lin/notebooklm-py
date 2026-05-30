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

Modelled after the AST-based lints in ``tests/_lint/`` (e.g.
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

# Tracking issue for bringing the ``ChatAPI`` conversation locks under the
# canonical owner-level protocol. They are guarded indirectly today (the
# injected ``loop_guard.assert_bound_loop()`` fires in ``ChatAPI.ask`` before
# the lock is acquired), but ``ChatAPI`` itself does not own
# ``set_bound_loop`` / ``reset_after_open``.
CHAT_LOCKS_FOLLOWUP_ISSUE = 1225


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
    # NOTE: ``ClientComposed``, ``TransportDrainTracker``, and
    # ``SourceUploadPipeline`` are NOT allowlisted — they each define the full
    # ``set_bound_loop`` + ``reset_after_open`` protocol and so are detected as
    # compliant by the owner-method scan.
    #
    # ``set_bound_loop`` only (no ``reset_after_open``): the lazy ``asyncio.Lock``
    # is rebuilt implicitly because these coordinators are reconstructed per
    # ``open()`` and the call-site ``assert_bound_loop(self._bound_loop)`` in
    # ``await_refresh`` / ``snapshot`` rejects cross-loop misuse before the
    # lazy lock is touched. ``set_bound_loop(None)`` on close clears the
    # binding so the next ``open()`` rebinds. A ``reset_after_open`` would be
    # a no-op here (the locks are never held across ``open()``).
    _AllowlistEntry(
        "src/notebooklm/_session_auth.py",
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
    # KNOWN FOLLOW-UP GAP (#1225): the ChatAPI per-conversation /
    # per-notebook locks are NOT under the owner-level protocol yet. They are
    # guarded indirectly (ChatAPI.ask calls loop_guard.assert_bound_loop()
    # before acquiring the lock) and GC themselves via WeakValueDictionary,
    # but ChatAPI does not own set_bound_loop / reset_after_open. Tracked by
    # #1225; remove this entry when ChatAPI joins the protocol.
    _AllowlistEntry(
        "src/notebooklm/_chat.py",
        "ChatAPI",
        "Known follow-up: guarded indirectly by injected "
        "loop_guard.assert_bound_loop() in ask(); owner-level protocol pending.",
        issue=CHAT_LOCKS_FOLLOWUP_ISSUE,
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


def _is_primitive_construction(node: ast.AST) -> str | None:
    """Return the primitive name if *node* constructs an ``asyncio`` primitive."""
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node.func)
    if not name.startswith("asyncio."):
        return None
    leaf = name.rsplit(".", 1)[-1]
    return leaf if leaf in LOOP_BOUND_PRIMITIVES else None


class _ConstructionSite:
    __slots__ = ("path", "lineno", "primitive", "owner")

    def __init__(self, path: str, lineno: int, primitive: str, owner: str | None) -> None:
        self.path = path
        self.lineno = lineno
        self.primitive = primitive
        self.owner = owner

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.path, self.owner)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        owner = self.owner or "<module>"
        return f"{self.path}:{self.lineno} asyncio.{self.primitive} (owner={owner})"


def _class_methods(module: ast.Module) -> dict[str, set[str]]:
    """Map each top-level/nested class name to the methods it defines."""
    methods: dict[str, set[str]] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            methods[node.name] = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return methods


def _enclosing_class(class_ranges: list[tuple[int, int, str]], lineno: int) -> str | None:
    """Return the innermost class whose body spans *lineno*, if any."""
    best: tuple[int, int, str] | None = None
    for start, end, name in class_ranges:
        if start <= lineno <= end and (best is None or start > best[0]):
            best = (start, end, name)
    return best[2] if best else None


def _scan() -> tuple[list[_ConstructionSite], dict[str, dict[str, set[str]]]]:
    sites: list[_ConstructionSite] = []
    methods_by_file: dict[str, dict[str, set[str]]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        methods_by_file[rel] = _class_methods(module)
        class_ranges = [
            (node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, node.name)
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef)
        ]
        for node in ast.walk(module):
            primitive = _is_primitive_construction(node)
            if primitive is None:
                continue
            owner = _enclosing_class(class_ranges, node.lineno)
            sites.append(_ConstructionSite(rel, node.lineno, primitive, owner))
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
        owner_methods = methods_by_file.get(site.path, {}).get(site.owner or "", set())
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
            "tests/_lint/test_asyncio_loop_affinity_guard.py::ALLOWLIST.\n\n"
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
    """Known-gap allowlist entries (not alt-guarded) must cite a tracking issue."""
    # The ChatAPI locks are the only entry that is a *gap* rather than an
    # alternative documented guard; it must carry an issue reference so the
    # follow-up is trackable and the entry can be retired.
    chat_entry = _ALLOWLIST_BY_KEY[("src/notebooklm/_chat.py", "ChatAPI")]
    assert chat_entry.issue == CHAT_LOCKS_FOLLOWUP_ISSUE, (
        "ChatAPI loop-affinity gap must reference its tracking issue "
        f"(#{CHAT_LOCKS_FOLLOWUP_ISSUE})."
    )
