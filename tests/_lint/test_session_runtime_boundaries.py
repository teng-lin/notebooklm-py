"""Meta-lint: regression guards closing plan ``host-protocol-removal``.

Wave 2 of plan ``host-protocol-removal`` deleted the ``_LifecycleHost``
and ``RefreshAuthCore`` Protocols and rewrote ``refresh_auth_session``
to take five explicit keyword-only collaborators instead of a
Session-shaped core. Wave 3 deleted the surviving Session-level
auth/lifecycle forwards (``update_auth_tokens`` / ``update_auth_headers``
/ ``lifecycle``) that those Protocols had backed. Wave 4 (this file)
adds the AST regression guards that catch any future reintroduction of
the Session-as-host pattern at PR time.

The four guards in this module are deliberately AST-based — string-vs-name
spelling, ``typing.cast`` qualification, and re-imported ``cast`` aliases
all slip past a regex but not past an :mod:`ast` walk.

1. :func:`test_lifecycle_host_symbol_does_not_appear_in_src` walks every
   module under ``src/notebooklm/`` and fails if the bare identifier
   ``_LifecycleHost`` appears anywhere — as a :class:`ast.Name`, as an
   :class:`ast.Attribute`, or as a :class:`ast.Constant` string (the
   forward-reference shape ``cast("_LifecycleHost", ...)``). Reappearance
   is a regression: Wave 2 deleted the Protocol with #1133 and no
   surviving code path needs the host shape.

   Failure mode: introducing a typing import like
   ``from .._session_lifecycle import _LifecycleHost`` or annotating a
   parameter ``host: "_LifecycleHost"`` would re-establish the
   Session-as-host coupling Waves 1-3 dismantled.

2. :func:`test_no_cast_to_lifecycle_host_in_src` walks every module
   under ``src/notebooklm/`` and fails if any :class:`ast.Call` to a
   callable whose name is literally ``cast`` (bare or as a trailing
   attribute like ``typing.cast``) targets ``_LifecycleHost``:

   - ``cast("_LifecycleHost", obj)`` — first-arg string forward ref
   - ``cast(_LifecycleHost, obj)`` — first-arg bare name
   - ``typing.cast("_LifecycleHost", obj)`` / ``typing.cast(_LifecycleHost, obj)`` — qualified

   Guard 2 keys on BOTH the callable shape (must end in ``cast``) AND
   the first-arg literal (must spell ``_LifecycleHost``). A truly-aliased
   ``cast`` import such as ``from typing import cast as c`` followed by
   ``c("_LifecycleHost", obj)`` is NOT caught by Guard 2 because the
   callable spelling ``c`` doesn't match. That spelling is handled
   instead by Guard 1, which surfaces the literal ``"_LifecycleHost"``
   string regardless of the surrounding call context. The two guards
   together are alias-spelling invariant.

   Failure mode: Wave 2 retired the ``typing.cast(_LifecycleHost, core)``
   call site in ``_auth/session.py``. A naive future "I'll cast it for
   one line using the canonical ``cast`` spelling" trips Guard 2; a
   "...with an aliased ``cast`` import" trips Guard 1 instead.

3. :func:`test_refresh_auth_core_symbol_does_not_appear_in_src` mirrors
   Guard 1 for ``RefreshAuthCore`` — Wave 2 deleted that Protocol with
   #1133 and the symbol must stay gone.

   Failure mode: re-declaring ``class RefreshAuthCore(Protocol): ...``
   anywhere in ``src/notebooklm/`` (most likely in ``_auth/session.py``)
   would re-establish the Session-shaped argument contract that Wave 2
   replaced with five explicit kwargs.

4. :func:`test_auth_session_module_has_no_host_protocol_residue` is a
   focused contract for ``src/notebooklm/_auth/session.py`` — the
   module Wave 2 rewrote. Four sub-checks, all AST-based:

   - no import of the ``Session`` class (from any module path), so the
     refresh helper cannot regrow a typing dependency on the concrete
     lifecycle root
   - no Protocol class body that declares a ``_kernel`` attribute, so a
     new host Protocol cannot quietly resurrect the Wave 2 shape
   - no call of the form ``X.update_auth_tokens(...)`` or
     ``X.update_auth_headers(...)`` unless the receiver ``X`` is
     coordinator-shaped — either a bare name in
     :data:`AUTH_COORD_RECEIVER_NAMES` (``auth_coord``) OR an attribute
     chain whose terminal segment contains ``coord``/``coordinator``
     (``self._auth_coord.update_*``, ``client._collaborators.auth_coordinator.update_*``).
     The live caller invokes ``auth_coord.update_auth_*(...)`` on the
     explicit kwarg; calling either method on ``core``, ``session``,
     ``host``, ``self``, or ``self._session`` restores the
     Session-as-host pattern
   - no ``cast`` to either ``_LifecycleHost`` or ``RefreshAuthCore``
     (this duplicates Guards 1-3 for the one file that historically
     carried both casts; the duplication is intentional belt-and-braces)

   Failure mode: a future refactor that "just adds back the Session
   import for a typing-only annotation" or "re-introduces a cast for
   one production call site" trips this guard before the PR opens.

The AST shape is deliberate for the same reason Guard 1 / 2 enumerate
attribute and string forms separately: a regex over the source would
miss ``cast(_LifecycleHost, ...)`` if the import was renamed
(``from typing import cast as _cast``) or miss the string form
(``cast("_LifecycleHost", ...)``) if the file imports were spelled
differently. The AST walks match on the call shape and the literal
identifier, so the lint is invariant to import-style or alias choices.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_SESSION_PATH = SRC_ROOT / "_auth" / "session.py"

# The two deleted Protocol identifiers. Centralised so future plan
# closures (or accidental reintroductions) can extend the set in one
# place. Both were deleted in Wave 2 of plan ``host-protocol-removal``
# (PR #1133).
DELETED_HOST_PROTOCOL_NAMES: frozenset[str] = frozenset({"_LifecycleHost", "RefreshAuthCore"})

# Coordinator-shaped receiver names that may legitimately appear on the
# LHS of ``.update_auth_tokens(...)`` / ``.update_auth_headers(...)`` in
# the auth-refresh code path. The live caller in
# ``_auth/session.py::refresh_auth_session`` invokes
# ``auth_coord.update_auth_tokens(...)`` / ``auth_coord.update_auth_headers(...)``
# on the explicit ``auth_coord`` kwarg. Names like ``core``, ``host``,
# ``session``, or ``self`` are the Wave-2-deleted Session-as-host shape
# and must not reappear.
AUTH_COORD_RECEIVER_NAMES: frozenset[str] = frozenset({"auth_coord", "coord"})

# The two ``update_auth_*`` method names that historically pinned the
# ``RefreshAuthCore`` Protocol surface. Wave 3 deleted the Session-level
# forwards (PR #1134) — they now live on ``AuthRefreshCoordinator`` and
# must be reached through that collaborator's name.
AUTH_FORWARD_METHOD_NAMES: frozenset[str] = frozenset({"update_auth_tokens", "update_auth_headers"})

MOVED_SESSION_SYMBOL_NAMES: frozenset[str] = frozenset(
    # Only symbols that used to be reachable through `notebooklm._session`
    # aliases belong here. Moves between other modules, such as default sleep
    # resolution moving onto `ClientSeams`, are outside this guard's scope.
    {
        "compose_session_internals",
        "ComposedSession",
        "resolve_seam_defaults",
        "_default_decode_response",
        "_default_is_auth_error",
    }
)


def _iter_src_files() -> list[Path]:
    """Return every ``.py`` under ``src/notebooklm/``, sorted, excluding caches."""
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _iter_src_and_test_files() -> list[Path]:
    """Return every checked ``.py`` file under ``src/notebooklm/`` and ``tests/``."""
    test_root = REPO_ROOT / "tests"
    return sorted(
        p
        for root in (SRC_ROOT, test_root)
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _find_symbol_appearances(tree: ast.AST, symbol: str) -> list[tuple[int, str]]:
    """Return ``[(lineno, context), ...]`` for every appearance of ``symbol``.

    Contexts:
      - ``"Name"`` — bare identifier reference (``_LifecycleHost``)
      - ``"Attribute"`` — attribute access (``mod._LifecycleHost``)
      - ``"Constant"`` — string-literal forward reference
        (``"_LifecycleHost"`` inside annotations / ``cast``)
      - ``"ClassDef"`` — a class definition by that name
      - ``"alias"`` — an ``import ... as _LifecycleHost`` or
        ``from ... import _LifecycleHost`` (the ``alias`` AST node)
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            hits.append((node.lineno, "Name"))
        elif isinstance(node, ast.Attribute) and node.attr == symbol:
            hits.append((node.lineno, "Attribute"))
        elif isinstance(node, ast.Constant) and node.value == symbol:
            hits.append((node.lineno, "Constant"))
        elif isinstance(node, ast.ClassDef) and node.name == symbol:
            hits.append((node.lineno, "ClassDef"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # Walk the parent import node rather than ``ast.alias`` itself —
            # the parent always carries a valid ``lineno`` across all
            # supported Python versions, whereas ``ast.alias.lineno`` is
            # populated only from 3.10 onward. Using the parent's
            # ``lineno`` keeps the diagnostic accurate without a
            # version-dependent fallback (gemini-code-assist review).
            for alias in node.names:
                if alias.name == symbol or alias.asname == symbol:
                    hits.append((node.lineno, "alias"))
    return hits


def _is_cast_call(node: ast.AST) -> bool:
    """Return True if ``node`` is a :class:`ast.Call` to ``cast`` (any spelling).

    Matches:
      - ``cast(...)``       (bare name; whether imported as ``cast`` or
        re-imported under another alias — the name match catches the
        canonical spelling, and Guard 2 also gates on the second-arg
        literal so alias rewrites still trip)
      - ``typing.cast(...)`` / ``t.cast(...)``  (attribute access ending in ``cast``)
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "cast":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "cast"


def _cast_target_name(call: ast.Call) -> str | None:
    """Return the target-type identifier if ``call`` is ``cast(<target>, value)``.

    Returns the string content for ``cast("_LifecycleHost", v)``, the
    ``id`` for ``cast(_LifecycleHost, v)``, or ``None`` for anything
    else (computed target, missing args, attribute chain, etc.). The
    runtime contract of ``typing.cast`` requires exactly two positional
    arguments; we are tolerant of length >= 1 so a malformed cast in
    new code still surfaces the violation rather than silently skipping.
    """
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.Name):
        return first.id
    return None


def _protocol_class_declares_kernel(class_def: ast.ClassDef) -> bool:
    """Return True if ``class_def`` is a Protocol body that declares ``_kernel``.

    A Protocol body in stub form is a sequence of annotated assignments
    (``_kernel: Kernel``) or annotated-only statements. We check both
    the bases for ``Protocol`` membership and the body for a top-level
    ``_kernel`` annotation. ``runtime_checkable`` decorators do not
    change the body shape.
    """
    base_names: set[str] = set()
    for base in class_def.bases:
        if isinstance(base, ast.Name):
            base_names.add(base.id)
        elif isinstance(base, ast.Attribute):
            base_names.add(base.attr)
    if "Protocol" not in base_names:
        return False
    for stmt in class_def.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "_kernel":
                return True
    return False


def _imports_session_class(tree: ast.AST) -> list[int]:
    """Return line numbers of any import that brings ``Session`` into scope.

    Catches both ``from notebooklm._session import Session`` and
    ``import notebooklm._session as ...`` followed by ``Session``
    reference. We focus on the direct ``Session`` import — the second
    form would also surface as a ``Session`` Name/Attribute reference,
    but that's a Guard-2-style concern and out of scope for this guard.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Session" or alias.asname == "Session":
                    hits.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname == "Session":
                    hits.append(node.lineno)
    return hits


def _is_coordinator_receiver(receiver: ast.expr) -> bool:
    """Return ``True`` if ``receiver`` is a coordinator-shaped access target.

    Two legitimate shapes (Guard 4 sub-check 3):

    1. Bare :class:`ast.Name` whose ``id`` is in
       :data:`AUTH_COORD_RECEIVER_NAMES` — the canonical live shape in
       ``refresh_auth_session`` (``auth_coord.update_auth_tokens(...)``).
    2. :class:`ast.Attribute` whose terminal ``attr`` contains
       ``coord`` or ``coordinator`` — covers both the private slot
       (``self._auth_coord``) and any future fully-spelled accessor
       (``self._collaborators.auth_coordinator``). The match is on the
       terminal attribute name only, not the upstream chain, so the
       intent is clear: "the call lands on the coordinator collaborator".

    Anything else (bare-name receivers like ``core`` / ``session`` /
    ``host`` / ``self``; Attribute chains terminating in
    non-coordinator segments like ``self._session``; computed
    receivers like ``[a, b][0]``; Subscripts; etc.) is the regression
    surface and returns ``False``.
    """
    if isinstance(receiver, ast.Name):
        return receiver.id in AUTH_COORD_RECEIVER_NAMES
    if isinstance(receiver, ast.Attribute):
        attr_lower = receiver.attr.lower()
        return "coord" in attr_lower or "coordinator" in attr_lower
    return False


def _format_receiver_for_diagnostic(receiver: ast.expr) -> str:
    """Render ``receiver`` as a short human-readable string for failure messages.

    - ``ast.Name`` → its ``id`` (``core``, ``session``, ...).
    - ``ast.Attribute`` → ``...<terminal_attr>`` (``...session`` for
      ``self._session``, ``..._kernel`` for ``payload._kernel``). The
      leading ``...`` signals that the upstream chain is elided so the
      reader can grep the file by the terminal segment.
    - Anything else → the literal string ``"expression"`` (we cannot
      reconstruct an arbitrary AST shape cheaply, and the location
      lineno is already in the message).
    """
    if isinstance(receiver, ast.Name):
        return receiver.id
    if isinstance(receiver, ast.Attribute):
        return f"...{receiver.attr}"
    return "expression"


def _session_module_aliases(tree: ast.AST) -> set[str]:
    """Return local aliases bound to the ``notebooklm._session`` module."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm._session" and alias.asname is not None:
                    aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            imports_session_from_notebooklm = node.module == "notebooklm"
            imports_session_from_relative_package = node.module is None and node.level > 0
            if not (imports_session_from_notebooklm or imports_session_from_relative_package):
                continue
            for alias in node.names:
                if alias.name == "_session":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _moved_session_symbol_alias_violations(tree: ast.AST, *, rel: str) -> list[str]:
    """Return uses like ``session_mod.compose_session_internals``."""
    aliases = _session_module_aliases(tree)
    if not aliases:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in MOVED_SESSION_SYMBOL_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            violations.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
    return violations


def test_moved_session_symbols_are_not_reached_through_session_module_aliases() -> None:
    """Moved composition helpers must not be reached through aliased ``_session``."""
    violations: list[str] = []
    for path in _iter_src_and_test_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations.extend(_moved_session_symbol_alias_violations(tree, rel=rel))
    assert not violations, (
        "Composition helpers moved out of notebooklm._session. Import them from "
        "notebooklm._session_init or notebooklm._client_seams instead. Offenders:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Guard 1: ``_LifecycleHost`` must not appear in ``src/notebooklm/``.
# ---------------------------------------------------------------------------


def test_lifecycle_host_symbol_does_not_appear_in_src() -> None:
    """``_LifecycleHost`` was deleted in Wave 2 (PR #1133); reappearance is a regression.

    Failure mode: a future PR that re-introduces
    ``from .._session_lifecycle import _LifecycleHost`` or annotates
    a parameter ``host: "_LifecycleHost"`` would surface here as a
    ``Name``, ``Attribute``, ``Constant``, ``ClassDef``, or ``alias``
    violation depending on the exact spelling.
    """
    violations: list[str] = []
    for src in _iter_src_files():
        rel = src.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(src.read_text(encoding="utf-8"))
        hits = _find_symbol_appearances(tree, "_LifecycleHost")
        for lineno, ctx in hits:
            violations.append(f"{rel}:{lineno} ({ctx}) _LifecycleHost")
    assert not violations, (
        "_LifecycleHost was deleted in Wave 2 of plan host-protocol-removal "
        "(PR #1133). Reappearance reintroduces the Session-as-host pattern "
        "that Wave 2 dismantled — refresh_auth_session now takes five "
        "explicit keyword-only collaborators (auth, kernel, auth_coord, "
        "lifecycle, cookie_persistence). Offenders:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Guard 2: no cast to ``_LifecycleHost`` in any spelling.
# ---------------------------------------------------------------------------


def test_no_cast_to_lifecycle_host_in_src() -> None:
    """``cast(_LifecycleHost, ...)`` in any spelling must not appear in src.

    Spellings caught (per docstring):
    - ``cast("_LifecycleHost", obj)`` — string forward ref
    - ``cast(_LifecycleHost, obj)`` — bare name
    - ``typing.cast("_LifecycleHost", obj)`` — qualified
    - aliased ``cast`` import (e.g. ``from typing import cast as c``)
      where the second-arg literal still says ``_LifecycleHost`` (the
      target match is what trips the guard, not the callable's name)

    Failure mode: Wave 2 retired the ``typing.cast(_LifecycleHost, core)``
    line in ``_auth/session.py``. Adding it back — or a sibling cast in
    any other module — would trip this guard.
    """
    violations: list[str] = []
    for src in _iter_src_files():
        rel = src.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _is_cast_call(node):
                continue
            assert isinstance(node, ast.Call)  # narrowed by _is_cast_call
            target = _cast_target_name(node)
            if target == "_LifecycleHost":
                violations.append(f"{rel}:{node.lineno} cast(..., _LifecycleHost)")
    assert not violations, (
        "cast(..., _LifecycleHost) was retired in Wave 2 of plan "
        "host-protocol-removal (PR #1133). Reintroducing the cast — even "
        "for a one-line typing accommodation — reopens the Session-as-host "
        "coupling Waves 1-3 closed. Offenders:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Guard 3: ``RefreshAuthCore`` must not appear in ``src/notebooklm/``.
# ---------------------------------------------------------------------------


def test_refresh_auth_core_symbol_does_not_appear_in_src() -> None:
    """``RefreshAuthCore`` was deleted in Wave 2 (PR #1133); reappearance is a regression.

    Failure mode: re-declaring ``class RefreshAuthCore(Protocol): ...``
    in ``src/notebooklm/_auth/session.py`` (or anywhere else under
    ``src/notebooklm/``) would surface here as a ``ClassDef`` violation;
    a typing-only ``from ._auth.session import RefreshAuthCore`` would
    surface as an ``alias`` violation; a string forward reference
    ``"RefreshAuthCore"`` in an annotation would surface as a
    ``Constant`` violation.
    """
    violations: list[str] = []
    for src in _iter_src_files():
        rel = src.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(src.read_text(encoding="utf-8"))
        hits = _find_symbol_appearances(tree, "RefreshAuthCore")
        for lineno, ctx in hits:
            violations.append(f"{rel}:{lineno} ({ctx}) RefreshAuthCore")
    assert not violations, (
        "RefreshAuthCore was deleted in Wave 2 of plan host-protocol-removal "
        "(PR #1133). Reappearance restores the Session-shaped argument "
        "contract that refresh_auth_session(core) once required; the live "
        "helper takes five explicit collaborators by keyword. Offenders:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Guard 4: ``_auth/session.py`` must carry no host-Protocol residue.
# ---------------------------------------------------------------------------


def test_auth_session_module_has_no_host_protocol_residue() -> None:
    """``_auth/session.py`` must remain free of every Wave 2 / Wave 3 residue.

    Four sub-checks (any failure surfaces as a separate violation line):

    1. no import of the ``Session`` class — keeps the refresh helper
       free of any typing dependency on the lifecycle root
    2. no Protocol body declaring ``_kernel`` — prevents resurrection of
       a host-shaped Protocol under a new name
    3. no call ``X.update_auth_tokens(...)`` / ``X.update_auth_headers(...)``
       where ``X`` is not a coordinator-shaped name. The live caller
       routes through ``auth_coord.update_auth_*(...)``; calling either
       method on ``core`` / ``session`` / ``host`` / ``self`` would
       restore the deleted Session-as-host shape.
    4. no cast to ``_LifecycleHost`` or ``RefreshAuthCore`` — duplicates
       Guards 1-3 for the one file that historically carried both casts;
       the duplication is belt-and-braces.

    Failure mode: a future PR that "re-adds the Session import for a
    typing-only annotation" or "re-introduces a cast for one call site"
    trips this guard before the PR opens.
    """
    source = AUTH_SESSION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []

    # Sub-check 1: no import of ``Session``.
    for lineno in _imports_session_class(tree):
        violations.append(
            f"_auth/session.py:{lineno} imports `Session` — Wave 2 removed "
            "the Session-shaped core argument; refresh_auth_session takes "
            "five explicit collaborators."
        )

    # Sub-check 2: no Protocol class with ``_kernel: ...`` annotation.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _protocol_class_declares_kernel(node):
            violations.append(
                f"_auth/session.py:{node.lineno} declares Protocol `{node.name}` "
                "with `_kernel` annotation — host-shaped Protocols were "
                "retired in Wave 2."
            )

    # Sub-check 3: no ``X.update_auth_tokens(...)`` / ``X.update_auth_headers(...)``
    # unless the receiver is coordinator-shaped. Two receiver shapes
    # may legitimately reach the coordinator:
    #
    #   1. Bare ``Name`` matching :data:`AUTH_COORD_RECEIVER_NAMES`
    #      (the canonical live shape — ``auth_coord.update_auth_tokens(...)``
    #      in ``refresh_auth_session``).
    #   2. ``Attribute`` chain whose terminal ``attr`` is coordinator-shaped
    #      (``self._auth_coord.update_*``, ``client._collaborators.auth_coord.update_*``).
    #      "Coordinator-shaped" means the terminal segment contains
    #      either ``coord`` or ``coordinator`` — covering both the
    #      private slot name (``_auth_coord``) and a hypothetical
    #      fully-spelled accessor (``auth_coordinator``).
    #
    # Everything else — bare-name receivers like ``core`` / ``session``
    # / ``host`` / ``self``, and Attribute chains terminating in
    # non-coordinator segments like ``self._session.update_*`` —
    # restores the Session-as-host shape Wave 3 deleted and surfaces
    # here as a violation. The widened receiver coverage closes the
    # gap that gemini-code-assist flagged: the previous code only
    # checked ``ast.Name`` receivers and silently passed
    # ``self._session.update_auth_tokens(...)`` because that receiver
    # is an ``ast.Attribute``.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in AUTH_FORWARD_METHOD_NAMES:
            continue
        receiver = node.func.value
        if _is_coordinator_receiver(receiver):
            continue
        receiver_repr = _format_receiver_for_diagnostic(receiver)
        violations.append(
            f"_auth/session.py:{node.lineno} calls "
            f"`{receiver_repr}.{node.func.attr}(...)` — "
            f"`{node.func.attr}` now lives on AuthRefreshCoordinator; "
            "route through the `auth_coord` kwarg explicitly."
        )

    # Sub-check 4: no cast to ``_LifecycleHost`` / ``RefreshAuthCore``.
    for node in ast.walk(tree):
        if not _is_cast_call(node):
            continue
        assert isinstance(node, ast.Call)  # narrowed by _is_cast_call
        target = _cast_target_name(node)
        if target in DELETED_HOST_PROTOCOL_NAMES:
            violations.append(
                f"_auth/session.py:{node.lineno} casts to `{target}` — both "
                "host Protocols were retired in Wave 2 of plan "
                "host-protocol-removal (PR #1133)."
            )

    assert not violations, (
        "_auth/session.py must remain free of Session-as-host residue after "
        "Waves 2-3 of plan host-protocol-removal. Offenders:\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Self-coverage — prove each guard fires on a synthetic regression.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "symbol", "expected_contexts"),
    [
        # Bare Name reference
        ("def f(x: _LifecycleHost) -> None: ...\n", "_LifecycleHost", {"Name"}),
        # Attribute access
        ("mod._LifecycleHost\n", "_LifecycleHost", {"Attribute"}),
        # String forward reference
        ('x: "_LifecycleHost"\n', "_LifecycleHost", {"Constant"}),
        # Class definition
        ("class _LifecycleHost: ...\n", "_LifecycleHost", {"ClassDef"}),
        # Import alias
        (
            "from foo import _LifecycleHost\n",
            "_LifecycleHost",
            {"alias"},
        ),
        # Mirror for RefreshAuthCore
        ("class RefreshAuthCore(Protocol): ...\n", "RefreshAuthCore", {"ClassDef"}),
    ],
    ids=[
        "lifecycle-host-name",
        "lifecycle-host-attribute",
        "lifecycle-host-string",
        "lifecycle-host-classdef",
        "lifecycle-host-import",
        "refresh-auth-core-classdef",
    ],
)
def test_find_symbol_appearances_catches_each_context(
    source: str, symbol: str, expected_contexts: set[str]
) -> None:
    """``_find_symbol_appearances`` must catch every context the docstring claims."""
    tree = ast.parse(source)
    hits = _find_symbol_appearances(tree, symbol)
    assert hits, f"Expected at least one hit for {symbol!r} in {source!r}"
    seen_contexts = {ctx for _, ctx in hits}
    assert expected_contexts.issubset(seen_contexts), (
        f"Expected contexts {expected_contexts!r} for {source!r}, got {seen_contexts!r}"
    )


def test_find_symbol_appearances_ignores_unrelated_names() -> None:
    """Unrelated identifiers must not be flagged."""
    tree = ast.parse("class Other: ...\nfrom foo import Bar\n")
    assert _find_symbol_appearances(tree, "_LifecycleHost") == []
    assert _find_symbol_appearances(tree, "RefreshAuthCore") == []


@pytest.mark.parametrize(
    ("source", "expected_target"),
    [
        # String form
        ('cast("_LifecycleHost", x)\n', "_LifecycleHost"),
        # Name form
        ("cast(_LifecycleHost, x)\n", "_LifecycleHost"),
        # Qualified
        ('typing.cast("_LifecycleHost", x)\n', "_LifecycleHost"),
        # Aliased callable (``c = cast``) is NOT matched by Guard 2 — the
        # callable spelling ``c`` doesn't end in ``cast``. Guard 1 catches
        # the literal ``"_LifecycleHost"`` independently, so the system is
        # still alias-spelling invariant; ``_cast_target_name`` returns
        # ``None`` here because ``_is_cast_call`` rejects the callable
        # shape before the target check runs.
        ('c("_LifecycleHost", x)\n', None),
        # Sibling Protocol
        ("cast(RefreshAuthCore, x)\n", "RefreshAuthCore"),
    ],
    ids=[
        "string-form",
        "name-form",
        "qualified-form",
        "aliased-not-named-cast",
        "refresh-auth-core",
    ],
)
def test_cast_target_extraction(source: str, expected_target: str | None) -> None:
    """``_cast_target_name`` resolves first-arg targets across spellings."""
    tree = ast.parse(source)
    casts = [
        _cast_target_name(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_cast_call(node)
    ]
    if expected_target is None:
        assert casts == [], (
            f"Expected no cast call (callable is not named `cast`) for {source!r}, got {casts!r}"
        )
    else:
        assert expected_target in casts, (
            f"Expected to find cast target {expected_target!r} in {source!r}, got {casts!r}"
        )


def test_protocol_class_declares_kernel_positive() -> None:
    """A Protocol body with ``_kernel: T`` must be detected."""
    tree = ast.parse(
        "class _HostProto(Protocol):\n    _kernel: object\n    def f(self) -> None: ...\n"
    )
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert len(classes) == 1
    assert _protocol_class_declares_kernel(classes[0])


def test_protocol_class_declares_kernel_skips_non_protocol() -> None:
    """A plain dataclass with ``_kernel`` annotation must NOT trip the guard.

    The Protocol membership gate prevents over-matching on legitimate
    collaborator dataclasses (e.g. ``ClientLifecycle._kernel: Kernel``).
    """
    tree = ast.parse("class Plain:\n    _kernel: object\n")
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert not _protocol_class_declares_kernel(classes[0])


def test_imports_session_class_catches_both_shapes() -> None:
    """``from foo import Session`` and ``import foo as Session`` both surface."""
    tree = ast.parse(
        "from notebooklm._session import Session\n"
        "import other as Session\n"
        "from notebooklm._session import Session as S\n"  # aliased rename also surfaces
    )
    # All three lines bring some name into scope that resolves to Session.
    # The third form (``Session as S``) imports the Session class itself
    # and renames it; we surface that too because the typing dependency
    # is what the guard is preventing, not the local name choice.
    hits = _imports_session_class(tree)
    assert len(hits) == 3, f"Expected 3 hits, got {hits!r}"


@pytest.mark.parametrize(
    ("source", "is_coordinator"),
    [
        # Bare-name receivers that ARE coordinators.
        ("auth_coord.x", True),
        ("coord.x", True),
        # Bare-name receivers that are NOT coordinators (the regression surface).
        ("core.x", False),
        ("session.x", False),
        ("host.x", False),
        ("self.x", False),
        # Attribute-chain receivers whose terminal segment IS coordinator-shaped.
        # The terminal segment is the one immediately before the called method,
        # so ``self._auth_coord.update_*`` has terminal ``_auth_coord``.
        ("self._auth_coord.x", True),
        ("self._collaborators.auth_coordinator.x", True),
        # Attribute-chain receivers whose terminal segment is NOT coordinator-shaped.
        # ``self._session.update_*`` was historically the deleted Session-as-host
        # shape; the previous version of this guard silently passed it.
        ("self._session.x", False),
        ("client._collaborators.x", False),  # terminal segment is plain `_collaborators`
        ("payload.kernel.x", False),
    ],
    ids=[
        "bare-auth-coord",
        "bare-coord",
        "bare-core-reject",
        "bare-session-reject",
        "bare-host-reject",
        "bare-self-reject",
        "chain-self-_auth_coord",
        "chain-collaborators-auth_coordinator",
        "chain-self-_session-reject",
        "chain-collaborators-terminal-only",
        "chain-payload-kernel-reject",
    ],
)
def test_is_coordinator_receiver_covers_both_shapes(source: str, is_coordinator: bool) -> None:
    """``_is_coordinator_receiver`` accepts bare coordinator names AND
    attribute chains whose terminal segment is coordinator-shaped.

    The terminal-segment rule is what closes the gap gemini-code-assist
    flagged on PR #1135: the previous code only checked bare-name
    receivers, so ``self._session.update_auth_tokens(...)`` slipped
    past the guard because its receiver is an ``ast.Attribute`` (the
    ``self._session`` chain) rather than an ``ast.Name``.
    """
    tree = ast.parse(source)
    # The source above is a single bare attribute expression; the
    # outermost node is an ``Expr`` wrapping the ``Attribute`` we want.
    expr = tree.body[0]
    assert isinstance(expr, ast.Expr)
    outer = expr.value
    assert isinstance(outer, ast.Attribute), (
        f"Expected outer Attribute for {source!r}, got {type(outer).__name__}"
    )
    # The receiver of the (would-be) call is ``outer.value`` —
    # everything up to (but not including) the trailing ``.x``.
    receiver = outer.value
    assert _is_coordinator_receiver(receiver) is is_coordinator, (
        f"For receiver in {source!r}: expected is_coordinator={is_coordinator}, "
        f"got {_is_coordinator_receiver(receiver)!r}"
    )


def test_format_receiver_for_diagnostic_shapes() -> None:
    """Diagnostic rendering elides the upstream chain but pins the terminal segment."""
    tree = ast.parse("self._session.x\nbare.x\n(1 + 2).x\n")
    receivers = [
        node.value.value  # type: ignore[union-attr]
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Attribute)
    ]
    assert _format_receiver_for_diagnostic(receivers[0]) == "..._session"
    assert _format_receiver_for_diagnostic(receivers[1]) == "bare"
    assert _format_receiver_for_diagnostic(receivers[2]) == "expression"


def test_moved_session_symbol_alias_guard_catches_synthetic_alias() -> None:
    """Prove aliasing ``notebooklm._session`` cannot hide moved helper access."""
    tree = ast.parse(
        "import notebooklm._session as session_mod\nsession_mod.compose_session_internals\n"
    )
    assert _moved_session_symbol_alias_violations(tree, rel="synthetic.py") == [
        "synthetic.py:2 session_mod.compose_session_internals"
    ]
