"""ADR-014 Rule 3 enforcement: features take collaborators, not Session.

Four AST guards. The first two were introduced in Wave 13 of the
session-decoupling plan (see ``docs/session-decoupling-plan-2026-05-26.md``
Task 6.3); the third is a follow-up boundary rule that closes out the
client-side reach-through cleanup; the fourth was added in Wave 4 of plan
[`host-protocol-removal`](../../.sisyphus/phases/host-protocol-removal/phase-1.md)
to tighten the private-attr rule into a positive allowlist for the few
``self._session`` attributes the composition root may legitimately read.

1. :func:`test_no_feature_constructed_with_session_at_composition_root`
   parses ``src/notebooklm/client.py`` and fails if any feature-API
   constructor call passes ``self._session`` (positionally or by keyword).
   The composition root MUST wire features with the specific collaborator
   or feature-local adapter that satisfies their Protocol — never the
   whole ``Session``. This is the most likely future-drift vector ADR-014
   names: a contributor under time pressure adds a new feature
   constructor that takes ``self._session`` "just for now."

2. :func:`test_stage_a_accessors_only_used_in_allowlist` walks every
   module under ``src/notebooklm/`` and fails if any read of
   ``Session.collaborators``, ``Session.session_transport``, or
   ``Session.rpc_executor`` happens outside the allowlist
   (``client.py`` + ``_session.py``). These three Stage-A accessors are
   the transitional discovery surface ``NotebookLMClient.__init__`` uses
   to wire features; feature modules MUST NOT reach for them or they
   would re-establish ``Session`` as a discoverability hub — exactly the
   pattern ADR-014 Rule 3 closes. Stage B (tracked as a Wave 7
   follow-up) moves ``build_collaborators`` ownership to
   ``NotebookLMClient`` and deletes the three accessors entirely.

3. :func:`test_client_does_not_dereference_session_privates` parses
   ``src/notebooklm/client.py`` and fails if any expression of the
   shape ``self._session._<name>`` appears anywhere in the module
   (read, write, delete, or augmented assignment). The composition
   root MUST consume ``Session`` through its narrow public surface
   (e.g. :meth:`Session.drain`, :meth:`Session.open`,
   :meth:`Session.close`, :attr:`Session.is_open`), never through the
   underscore-prefixed collaborator slots on ``Session``. The rule is
   boundary-focused: it does not pin line-history-specific reach-through
   call sites and so does not need to be rewritten every time a private
   slot is renamed. New private slots automatically come under the rule
   the moment they get an underscore-prefixed name.

4. :func:`test_client_self_session_access_is_allowlisted` is the
   stricter sibling of Guard 3 — it walks ``client.py`` and fails if
   any ``self._session.<X>`` access reads an attribute not in the
   :data:`SESSION_ALLOWED_ATTRS` set (``open`` / ``close`` / ``drain``
   / ``is_open``). Wave 3 of plan ``host-protocol-removal`` deleted the
   transitional ``self._session.lifecycle`` / ``self._session.auth``
   reads from ``NotebookLMClient`` (the lifecycle is now reached via
   ``self._collaborators.lifecycle`` and the auth tokens via
   ``self._auth``). Guard 3 only catches underscore-prefixed slot leaks;
   this guard ALSO catches public-attribute drift like a future
   ``self._session.lifecycle`` re-introduction, which Guard 3 would
   silently allow. The allowlist is intentionally narrow: any
   ``self._session.<X>`` outside the four lifecycle hot-path
   accessors must surface here at PR time.

The AST shape is deliberate: a regex over the source would either
over-match (e.g. ``collaborators`` as a variable name) or under-match
(attribute chains like ``self._session.rpc_executor`` versus
``session.rpc_executor`` versus ``foo.rpc_executor``). The AST walk
checks the attribute name only, so any chain ending in ``.rpc_executor``
outside the allowlist trips the guard regardless of how the receiver was
spelled.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "src" / "notebooklm" / "client.py"
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# Top-level feature APIs + the two domain services that
# ``NotebookLMClient.__init__`` constructs directly with the Session-
# derived collaborators. Scope boundary: this set is intentionally the
# constructor names that appear in ``client.py`` and that take a
# ``RpcCaller`` (or richer composite) as a primary dependency. Second-
# level services constructed *from* one of these (e.g.
# ``NoteBackedMindMapService`` which receives ``NoteService`` only) are
# out of scope — they cannot accidentally take ``self._session`` because
# they don't see ``self`` at the composition root.
FEATURE_API_NAMES = {
    "SettingsAPI",
    "SharingAPI",
    "ResearchAPI",
    "NotesAPI",
    "SourcesAPI",
    "NotebooksAPI",
    "ChatAPI",
    "ArtifactsAPI",
    "SourceUploadPipeline",
    "NoteService",
}

STAGE_A_ACCESSORS = {"collaborators", "session_transport", "rpc_executor"}

# Files allowed to read the Stage-A accessors. The composition root
# (``client.py``) wires features with them; ``_session.py`` owns the
# storage + the property bodies themselves. Everything under ``tests/``
# is excluded by being outside ``src/notebooklm/`` rather than via this
# allowlist. ``_session_init.py`` is intentionally NOT on this list
# (verified at write time: ``_session_init.py`` constructs the
# collaborators that the accessors expose, not the other way around —
# it never reads the accessors back).
#
# ``_auth/session.py`` was historically allowlisted because
# :func:`refresh_auth_session` read ``core.collaborators.lifecycle.save_cookies``
# to persist rotated cookies through the canonical chokepoint (Wave 11c of
# session-decoupling deleted ``Session.save_cookies`` and routed the
# auth-refresh persist call through the Stage-A accessor). Wave 2 of plan
# ``host-protocol-removal`` eliminated the read entirely: ``refresh_auth_session``
# now takes the five concrete collaborators as keyword-only kwargs and the
# deleted ``RefreshAuthCore`` Protocol no longer exposes ``collaborators``
# at all. Wave 3 narrowed the allowlist back to ``client.py`` +
# ``_session.py`` so any accidental future Stage-A reads in
# ``_auth/session.py`` are caught immediately by this static guard
# (gemini-code-assist / coderabbit review on PR #1134).
ACCESSOR_ALLOWLIST = {
    "src/notebooklm/client.py",
    "src/notebooklm/_session.py",
}


def _passes_self_session(arg: ast.expr) -> bool:
    """True if ``arg`` is the AST shape of ``self._session``."""
    return (
        isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "self"
        and arg.attr == "_session"
    )


def test_no_feature_constructed_with_session_at_composition_root() -> None:
    """ADR-014 Rule 3: no feature constructor in ``client.py`` receives ``self._session``."""
    tree = ast.parse(CLIENT_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in FEATURE_API_NAMES:
            continue
        for arg in node.args:
            if _passes_self_session(arg):
                violations.append(
                    f"{node.func.id} at line {node.lineno}: passes self._session positionally"
                )
        for kw in node.keywords:
            if _passes_self_session(kw.value):
                # ``kw.arg`` is ``None`` for ``**spread`` unpacking
                # (``FeatureAPI(**self._session)``); render that as
                # ``**`` so the diagnostic is unambiguous instead of
                # printing the literal string ``None``.
                kwarg_name = kw.arg if kw.arg is not None else "**"
                violations.append(
                    f"{node.func.id} at line {node.lineno}: passes self._session via kwarg {kwarg_name}"
                )
    assert not violations, (
        "ADR-014 Rule 3 violation — feature APIs must receive their "
        "specific collaborator or adapter, not the whole Session:\n  " + "\n  ".join(violations)
    )


def test_stage_a_accessors_only_used_in_allowlist() -> None:
    """ADR-014 Rule 3 Stage A: the three Session accessors are only
    legitimate reads inside ``client.py`` / ``_session.py``. A read from
    any other production module would re-establish Session as a
    discoverability hub — exactly what Stage A is gated against until
    Stage B deletes the accessors entirely.
    """
    violations: list[str] = []
    for src in SRC_ROOT.rglob("*.py"):
        # rglob can return absolute paths; normalize to a repo-relative
        # POSIX form for stable allowlist matching (the round-4 fix in
        # the plan calls out using ``as_posix()`` against the repo-rel
        # path rather than ``relative_to(Path.cwd())`` so the lint is
        # cwd-independent).
        rel = src.relative_to(REPO_ROOT).as_posix()
        if rel in ACCESSOR_ALLOWLIST:
            continue
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in STAGE_A_ACCESSORS:
                violations.append(f"{rel}:{node.lineno}: reads .{node.attr}")
    assert not violations, (
        "ADR-014 Rule 3 Stage-A accessor leak — feature modules must "
        "not reach Session.collaborators / .session_transport / "
        ".rpc_executor:\n  " + "\n  ".join(violations)
    )


def _is_self_session_private_attribute(node: ast.AST) -> tuple[int, str] | None:
    """Return ``(lineno, attr)`` if ``node`` is ``self._session._<name>``.

    Matches read context, write context, ``del`` context, and the
    ``Attribute`` target of an :class:`ast.AugAssign` — the AST shape
    is the same in all four. Returns ``None`` for anything else.

    The receiver must be exactly the AST shape of ``self._session``:
    an :class:`ast.Attribute` whose ``value`` is a bare :class:`ast.Name`
    ``self`` and whose ``attr`` is ``_session``. Chains like
    ``other._session._foo`` are deliberately not flagged here — this
    lint is scoped to the composition root, and ``client.py`` does not
    construct alternate references to the session under any other name.

    Python dunder attributes (``__name__``-style — start AND end with a
    double underscore) are intentionally excluded: they are Python
    protocol surface, not project-defined private implementation slots,
    so a ``self._session.__class__`` access does not signal a boundary
    leak. The lint targets only project-owned private slots.
    """
    if not isinstance(node, ast.Attribute):
        return None
    if not node.attr.startswith("_"):
        return None
    # Exclude Python dunder attributes — ``__class__``, ``__dict__``, etc.
    # are protocol surface, not project-defined private slots.
    if node.attr.startswith("__") and node.attr.endswith("__"):
        return None
    inner = node.value
    if not isinstance(inner, ast.Attribute):
        return None
    if inner.attr != "_session":
        return None
    if not (isinstance(inner.value, ast.Name) and inner.value.id == "self"):
        return None
    return node.lineno, node.attr


def test_client_does_not_dereference_session_privates() -> None:
    """``client.py`` must not access ``self._session._<name>`` anywhere.

    Boundary rule (not line-history-focused): the composition root
    consumes :class:`Session` through narrow public/internal accessors
    (e.g. :meth:`Session.drain`, :meth:`Session.open`,
    :meth:`Session.close`, :attr:`Session.is_open`). Any
    ``self._session._<name>`` read, write, delete, or augmented
    assignment reintroduces a private reach-through and fails this
    test. New private slots come under the rule automatically when
    they get an underscore-prefixed name — no edits required here.
    """
    tree = ast.parse(CLIENT_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        match = _is_self_session_private_attribute(node)
        if match is not None:
            lineno, attr = match
            violations.append(f"line {lineno}: self._session.{attr}")
    assert not violations, (
        "client.py must not dereference private attributes of "
        "self._session — route through a narrow Session accessor "
        "(e.g. Session.drain, Session.open, Session.close, "
        "Session.is_open) instead:\n  " + "\n  ".join(violations)
    )


# Allowlist of ``self._session.<X>`` attribute names that the composition
# root in ``client.py`` may legitimately read. The four entries are the
# lifecycle hot-path methods + the ``is_open`` boolean that
# ``NotebookLMClient`` forwards to its public surface:
#
#   - ``open``    — entry point for ``async with NotebookLMClient(...)``
#   - ``close``   — drain + transport teardown
#   - ``drain``   — graceful in-flight wait, also exposed publicly
#   - ``is_open`` — public open-state read
#
# Wave 3 of plan ``host-protocol-removal`` deleted the transitional
# ``self._session.lifecycle`` accessor (the lifecycle collaborator is
# now reached via ``self._collaborators.lifecycle``) and the
# ``self._session.auth`` read (the auth tokens are now reached via
# ``self._auth`` per the Auth Instance Invariant). Wave 4 (this guard)
# pins the post-Wave-3 surface so neither read can quietly come back.
#
# Any addition to this set MUST come with a documented retention reason
# in ``docs/session-method-retention.md`` and a corresponding row in the
# Inventory table there. The accompanying lint
# ``tests/_lint/test_session_retention.py`` AST-parses ``_session.py``
# and cross-checks the inventory; the two lints together pin the
# Session surface from both directions (caller-side allowlist here +
# definer-side inventory there).
SESSION_ALLOWED_ATTRS: frozenset[str] = frozenset({"open", "close", "drain", "is_open"})


def _self_session_attribute_access(node: ast.AST) -> tuple[int, str] | None:
    """Return ``(lineno, attr)`` if ``node`` is ``self._session.<X>``.

    Mirrors :func:`_is_self_session_private_attribute` but matches the
    *broader* shape: ANY attribute name (public or private) on
    ``self._session``, not just the underscore-prefixed ones. Returns
    the outer Attribute's ``lineno`` and ``attr`` so the caller can
    filter by allowlist membership and surface a precise diagnostic.

    Dunder attributes (``__class__``, ``__dict__``, etc.) are
    intentionally excluded — they are Python protocol surface and not
    project-defined attributes; a ``self._session.__class__`` access is
    introspection, not a boundary leak. The lint targets only
    project-owned attribute names.

    The receiver must be exactly the AST shape of ``self._session`` —
    an :class:`ast.Attribute` whose ``value`` is a bare :class:`ast.Name`
    ``self`` and whose ``attr`` is ``_session``. Chains like
    ``other._session.<X>`` are deliberately not flagged here; ``client.py``
    does not construct alternate references to the session under any
    other name. This keeps the guard scoped tightly to the composition
    root's known reach pattern.
    """
    if not isinstance(node, ast.Attribute):
        return None
    # Exclude Python dunder attributes (``__class__``, ``__dict__``, etc.).
    if node.attr.startswith("__") and node.attr.endswith("__"):
        return None
    inner = node.value
    if not isinstance(inner, ast.Attribute):
        return None
    if inner.attr != "_session":
        return None
    if not (isinstance(inner.value, ast.Name) and inner.value.id == "self"):
        return None
    return node.lineno, node.attr


def test_client_self_session_access_is_allowlisted() -> None:
    """``self._session.<X>`` in ``client.py`` must be one of the allowed attrs.

    Stricter sibling of :func:`test_client_does_not_dereference_session_privates`:
    that guard only catches underscore-prefixed slot reads. This one is
    a positive allowlist — any access ``self._session.<X>`` where ``X``
    is not in :data:`SESSION_ALLOWED_ATTRS` fails the lint, including
    public-attribute drift like a future ``self._session.lifecycle``
    re-introduction.

    Failure mode: Wave 3 of plan ``host-protocol-removal`` deleted
    ``Session.lifecycle`` and the ``self._session.auth`` read in
    ``NotebookLMClient``. A future PR that "just adds back
    ``self._session.lifecycle`` for one read site" would slip past
    Guard 3 (the attribute is public, not underscore-prefixed) but
    surfaces here because ``lifecycle`` is not in the allowlist.

    The allowlist is intentionally narrow. Widening it requires
    cross-updating ``docs/session-method-retention.md`` (which the
    sibling lint ``test_session_retention.py`` cross-checks) so the
    retention contract stays synchronised end-to-end.
    """
    tree = ast.parse(CLIENT_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        match = _self_session_attribute_access(node)
        if match is None:
            continue
        lineno, attr = match
        if attr in SESSION_ALLOWED_ATTRS:
            continue
        violations.append(f"line {lineno}: self._session.{attr}")
    assert not violations, (
        "client.py may only access self._session.<X> for X in "
        f"{sorted(SESSION_ALLOWED_ATTRS)!r}. Any other attribute access "
        "reopens the Session-as-discoverability-hub pattern that ADR-014 "
        "Rule 3 closed and that Waves 1-3 of plan host-protocol-removal "
        "narrowed further. Route through the explicit collaborator "
        "(e.g. self._collaborators.lifecycle for the lifecycle, "
        "self._auth for the auth tokens) instead:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Self-coverage for ``_self_session_attribute_access`` — pin the helper's
# contract before the live production test exercises it. Each case below
# pairs a synthetic expression with the expected helper return so the
# documented filtering conditions (receiver shape, dunder exclusion,
# private/public attr coverage) can be regressed independently of the
# production scan over ``client.py``. Mirrors the self-coverage pattern
# every other helper in this suite (see ``test_no_session_compat_bridges.py``
# and ``test_session_runtime_boundaries.py``) already follows.
# ---------------------------------------------------------------------------


def _single_attribute_node(source: str) -> ast.AST:
    """Parse ``source`` and return its outermost expression's AST node.

    Centralised so the parametrized cases below don't repeat the
    ``parse → body[0] → value`` unwrap. ``source`` must be exactly one
    expression statement; anything else is a test-author error and will
    surface as an ``IndexError`` or ``AttributeError`` here rather than
    silently returning the wrong node.
    """
    tree = ast.parse(source)
    expr = tree.body[0]
    assert isinstance(expr, ast.Expr), (
        f"Expected single Expr statement, got {type(expr).__name__} for {source!r}"
    )
    return expr.value


@pytest.mark.parametrize(
    ("source", "expected_attr"),
    [
        # Positive: allowlisted attribute (the helper returns the match;
        # the allowlist filtering happens at the caller, not here).
        ("self._session.open", "open"),
        ("self._session.close", "close"),
        ("self._session.drain", "drain"),
        ("self._session.is_open", "is_open"),
        # Positive: non-allowlisted public attribute — the helper still
        # surfaces it, the caller filters against ``SESSION_ALLOWED_ATTRS``.
        # This is the regression vector the live test catches.
        ("self._session.lifecycle", "lifecycle"),
        ("self._session.auth", "auth"),
        # Positive: underscore-prefixed slot — the helper surfaces this
        # too; the existing ``_is_self_session_private_attribute`` covers
        # the same shape from a different angle.
        ("self._session._kernel", "_kernel"),
    ],
    ids=[
        "allowed-open",
        "allowed-close",
        "allowed-drain",
        "allowed-is_open",
        "forbidden-public-lifecycle",
        "forbidden-public-auth",
        "forbidden-private-_kernel",
    ],
)
def test_self_session_attribute_access_matches_canonical_shape(
    source: str, expected_attr: str
) -> None:
    """``self._session.<X>`` for any X (public or private) returns ``(lineno, X)``.

    The helper deliberately does NOT do allowlist filtering — that's
    the caller's job. Self-coverage here pins the AST-shape contract:
    the canonical receiver (``self._session``) with any non-dunder
    attribute name surfaces.
    """
    node = _single_attribute_node(source)
    result = _self_session_attribute_access(node)
    assert result is not None, f"Expected a match for {source!r}, got None"
    _lineno, attr = result
    assert attr == expected_attr


@pytest.mark.parametrize(
    "source",
    [
        # Receiver is not exactly ``self._session`` — the helper is
        # scoped to the composition root's canonical reach pattern.
        # ``other._session.open`` is out of scope (no such alias in
        # ``client.py``).
        "other._session.open",
        # No ``_session`` intermediary — direct attribute access on ``self``
        # is not what the guard targets.
        "self.open",
        # Receiver chain is too deep — ``self.foo._session.open`` does not
        # match because the immediate inner attribute is not ``_session``
        # (the parent is ``self.foo``, not ``self``).
        "self.foo._session.open",
        # Dunder attribute — Python protocol surface, intentionally excluded.
        "self._session.__class__",
        "self._session.__dict__",
        # Receiver is ``self._session`` but the outer node is the receiver
        # itself, not an attribute access on it — ``self._session`` standalone
        # is not a ``self._session.<X>`` chain.
        "self._session",
    ],
    ids=[
        "other-not-self",
        "no-_session-intermediary",
        "chain-too-deep",
        "dunder-__class__-excluded",
        "dunder-__dict__-excluded",
        "receiver-only-no-trailing-attr",
    ],
)
def test_self_session_attribute_access_rejects_non_canonical_shapes(
    source: str,
) -> None:
    """Receiver-shape and dunder exclusions documented in the helper's docstring."""
    node = _single_attribute_node(source)
    assert _self_session_attribute_access(node) is None, f"Expected no match for {source!r}"
