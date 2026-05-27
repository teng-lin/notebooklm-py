"""ADR-014 Rule 3 enforcement: features take collaborators, not Session.

Three AST guards. The first two were introduced in Wave 13 of the
session-decoupling plan (see ``docs/session-decoupling-plan-2026-05-26.md``
Task 6.3); the third is a follow-up boundary rule that closes out the
client-side reach-through cleanup.

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
   (e.g. :meth:`Session.drain`, :attr:`Session.lifecycle`,
   :attr:`Session.auth`, :meth:`Session.open` / :meth:`Session.close`),
   never through the underscore-prefixed collaborator slots on
   ``Session``. The rule is boundary-focused: it does not pin
   line-history-specific reach-through call sites and so does not need
   to be rewritten every time a private slot is renamed. New private
   slots automatically come under the rule the moment they get an
   underscore-prefixed name.

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
# ``_auth/session.py`` is allowlisted because :func:`refresh_auth_session`
# operates on the ``RefreshAuthCore`` Protocol that explicitly declares
# ``collaborators`` as a structural member (see Wave 11c of
# session-decoupling, which deleted ``Session.save_cookies`` and routed
# the auth-refresh persist call through ``core.collaborators.lifecycle.save_cookies``).
# The read is on a Protocol member, not opportunistic discovery, so it
# does not re-establish Session as a hub — it consumes a contract the
# Protocol pins. When Stage B (Wave 7 follow-up) deletes the Stage-A
# accessors entirely, ``RefreshAuthCore`` will be reshaped to take the
# lifecycle collaborator directly and this allowlist entry goes away.
ACCESSOR_ALLOWLIST = {
    "src/notebooklm/client.py",
    "src/notebooklm/_session.py",
    "src/notebooklm/_auth/session.py",
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
    (e.g. :meth:`Session.drain`, :attr:`Session.lifecycle`,
    :attr:`Session.auth`, :meth:`Session.open`, :meth:`Session.close`).
    Any ``self._session._<name>`` read, write, delete, or augmented
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
        "(e.g. Session.drain, Session.lifecycle) instead:\n  " + "\n  ".join(violations)
    )
