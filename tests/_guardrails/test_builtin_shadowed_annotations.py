"""Guard that a class-body annotation never names a builtin the class shadows.

``WebArtifactsAPI`` defines a method called ``list``, so inside that class body
the name ``list`` is the method, not the builtin. Until Python 3.14 that was
invisible: annotations were evaluated eagerly at ``def`` time, before the name
was bound as a class attribute, so ``-> list[Artifact]`` resolved to the
builtin and everything worked. PEP 649 defers the evaluation into a synthesized
``__annotate__`` whose scope includes the class body, so the same annotation now
resolves *after* the class exists and raises ``TypeError: 'function' object is
not subscriptable`` the moment anything reads it (#2266).

The repo's answer predates the break: ``builtins.list[...]`` is spelled
explicitly at ~40 sites across the client classes for exactly this reason. The
twelve sites #2266 found were the ones that missed the convention, not a
different design. This lint keeps them from coming back.

**Static on purpose.** It parses rather than imports, so it catches a 3.14-only
break while running on 3.12 — which is where it will actually run. The failing
signature check is marked ``repo_lint``, and every compatibility cell in
``test.yml`` runs ``-m "not repo_lint"``, while both lanes that do execute
``repo_lint`` pin 3.12. Adding a 3.14 lane would close today's hole on one
version; parsing closes it on every version, including whichever one breaks
next.

**Every builtin, not just ``list``.** Only ``list`` is shadowed today. A future
method named ``type``, ``filter`` or ``id`` fails the same way for the same
reason, and there is no cost to covering it now.

Two scopes matter, and modules using ``from __future__ import annotations`` sit
in only one of them:

* **Method signatures** break only under PEP 649's deferred evaluation. With
  the future import the annotation is a string that ``get_type_hints(func)``
  later evaluates against the module globals, where the builtin is still the
  builtin — so those modules are exempt from the signature half of this check.
* **Class-level variable annotations** break either way. ``get_type_hints(cls)``
  evaluates each entry with ``dict(vars(base))`` as locals, so a class that
  binds ``list`` shadows the builtin there on *every* version, future import or
  not. Those are checked everywhere.

Only names actually *bound* in the class body shadow anything. A bare
``list: list[str]`` records an annotation without binding ``list``, so it still
resolves to the builtin and is not an offender; ``list: list[str] = []`` binds
and is. Annotations that never resolve in the class-body scope are likewise out
of scope: a local annotation inside a method body (``source_ids: list[str] =
[]``) is not evaluated at all, and a nested ``def`` resolves in its enclosing
function scope, so neither can see the class attribute. Flagging those would
report a break that cannot happen.
"""

from __future__ import annotations

import ast
import builtins
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"

#: Statements that both bind their name in the enclosing scope and open a new
#: one. The name counts as a shadow; the body belongs to a different scope.
_SCOPE_STATEMENTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _child_suites(node: ast.stmt) -> Iterator[list[ast.stmt]]:
    """Yield the statement suites ``node`` runs in its own enclosing scope."""
    for field in ("body", "orelse", "finalbody"):
        suite = getattr(node, field, None)
        if isinstance(suite, list) and all(isinstance(item, ast.stmt) for item in suite):
            yield suite
    # ``except`` handlers and ``match`` cases keep their suites one level down.
    for field in ("handlers", "cases"):
        for clause in getattr(node, field, ()):
            yield clause.body


def _class_body_statements(cls: ast.ClassDef) -> Iterator[ast.stmt]:
    """Yield every statement that executes in ``cls``'s body scope.

    Class-level ``if`` / ``try`` / ``for`` / ``with`` / ``match`` suites run in
    the class scope, so what they bind and annotate lands there too. A nested
    ``def`` or ``class`` is yielded — its *name* binds here — but not descended
    into, because its body is a separate scope.
    """
    stack = list(cls.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_STATEMENTS):
            stack.extend(statement for suite in _child_suites(node) for statement in suite)


def _bound_names(target: ast.expr) -> Iterator[str]:
    """Yield the names one assignment target binds.

    Destructuring counts: ``list, sentinel = helpers`` binds ``list``.
    Attribute and subscript targets bind no new name.
    """
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _walrus_names(node: ast.AST) -> Iterator[str]:
    """Yield names ``:=`` binds in this scope, not inside a nested one."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Lambda, *_SCOPE_STATEMENTS)):
            continue
        if isinstance(child, ast.NamedExpr):
            yield from _bound_names(child.target)
        yield from _walrus_names(child)


def _shadowed_builtins(cls: ast.ClassDef) -> set[str]:
    """Return the builtin names this class body rebinds."""
    names: set[str] = set()
    for node in _class_body_statements(cls):
        if isinstance(node, _SCOPE_STATEMENTS):
            names.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_bound_names(target))
        elif isinstance(node, ast.AnnAssign):
            # A bare ``x: int`` annotates without binding, so the name still
            # resolves to the builtin; only ``x: int = 0`` shadows it.
            if node.value is not None:
                names.update(_bound_names(node.target))
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            names.update(_bound_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_bound_names(item.optional_vars))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        names.update(_walrus_names(node))
    return {name for name in names if hasattr(builtins, name)}


def _class_scope_annotations(cls: ast.ClassDef, *, include_signatures: bool) -> Iterator[ast.expr]:
    """Yield the annotations that resolve against ``cls``'s namespace.

    ``include_signatures`` is false for modules carrying ``from __future__
    import annotations``: there a method signature is a string evaluated
    against the module globals, so only the class-level variable annotations —
    which ``get_type_hints`` evaluates with the class namespace as locals —
    remain exposed.
    """
    for node in _class_body_statements(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not include_signatures:
                continue
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _offences(cls: ast.ClassDef, *, include_signatures: bool) -> Iterator[tuple[str, int]]:
    """Yield ``(name, lineno)`` for each annotation naming a shadowed builtin."""
    shadowed = _shadowed_builtins(cls)
    if not shadowed:
        return
    for annotation in _class_scope_annotations(cls, include_signatures=include_signatures):
        for node in ast.walk(annotation):
            if isinstance(node, ast.Name) and node.id in shadowed:
                yield node.id, node.lineno


def test_class_body_annotations_do_not_name_a_shadowed_builtin() -> None:
    offenders: list[str] = []

    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        include_signatures = not _has_future_annotations(tree)
        for cls in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            for name, lineno in _offences(cls, include_signatures=include_signatures):
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{lineno} ({cls.name} shadows '{name}')"
                )

    assert not offenders, (
        "class-body annotation names a builtin the class rebinds, which resolves to the "
        "class attribute under PEP 649 — and, for a class-level variable annotation, under "
        "get_type_hints() on any version (#2266). Qualify it as 'builtins."
        + "<name>[...]', the spelling the rest of the client classes already use:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


# --------------------------------------------------------------------------
# Regression fixtures for the checker itself. Each snippet below was executed
# against CPython 3.14.7 to confirm the behaviour it asserts: the "offends"
# cases raise TypeError out of get_type_hints(), the "clean" cases resolve.
# --------------------------------------------------------------------------


def _analyse(source: str) -> tuple[set[str], set[str]]:
    """Return ``(shadowed builtins, offending names)`` for a one-class snippet."""
    tree = ast.parse(textwrap.dedent(source))
    cls = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    include_signatures = not _has_future_annotations(tree)
    return (
        _shadowed_builtins(cls),
        {name for name, _ in _offences(cls, include_signatures=include_signatures)},
    )


def test_clean_class_has_no_offence() -> None:
    shadowed, offences = _analyse("""
        import builtins

        class Clean:
            def list(self) -> builtins.list[str]: ...
            def get(self, ids: builtins.list[str]) -> None: ...
    """)
    assert shadowed == {"list"}
    assert offences == set()


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(
            """
            class Nested:
                if TYPE_CHECKING:
                    def list(self): ...
                def get(self) -> list[str]: ...
            """,
            id="if",
        ),
        pytest.param(
            """
            class Nested:
                try:
                    def list(self): ...
                except ImportError:
                    list = None
                def get(self) -> list[str]: ...
            """,
            id="try-except",
        ),
        pytest.param(
            """
            class Nested:
                for list in helpers:
                    pass
                def get(self) -> list[str]: ...
            """,
            id="for",
        ),
        pytest.param(
            """
            class Nested:
                with ctx() as list:
                    pass
                def get(self) -> list[str]: ...
            """,
            id="with",
        ),
        pytest.param(
            """
            class Nested:
                match kind:
                    case _:
                        list = None
                def get(self) -> list[str]: ...
            """,
            id="match",
        ),
    ],
)
def test_binding_inside_a_nested_suite_shadows(snippet: str) -> None:
    """Class-level ``if``/``try``/``for``/``with``/``match`` suites run in the class scope."""
    shadowed, offences = _analyse(snippet)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_annotation_inside_a_nested_suite_is_checked() -> None:
    shadowed, offences = _analyse("""
        class Nested:
            def list(self): ...
            if TYPE_CHECKING:
                items: list[str] = []
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_destructuring_assignment_shadows() -> None:
    shadowed, offences = _analyse("""
        class Destructured:
            first, (list, *rest) = helpers
            def get(self) -> list[str]: ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_import_and_walrus_bindings_shadow() -> None:
    shadowed, _ = _analyse("""
        class Bound:
            from helpers import list
            if (type := compute()):
                pass
    """)
    assert shadowed == {"list", "type"}


def test_annotation_without_a_value_does_not_shadow() -> None:
    """``list: list[str]`` annotates without binding, so it still resolves."""
    shadowed, offences = _analyse("""
        class AnnotationOnly:
            list: list[str]
    """)
    assert shadowed == set()
    assert offences == set()


def test_annotation_with_a_value_shadows() -> None:
    shadowed, offences = _analyse("""
        class Assigned:
            list: list[str] = []
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_future_import_still_checks_class_level_annotations() -> None:
    """``get_type_hints(cls)`` uses the class namespace as locals on every version."""
    shadowed, offences = _analyse("""
        from __future__ import annotations

        class ClassLevel:
            items: list[str] = []
            def list(self): ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_future_import_exempts_method_signatures() -> None:
    shadowed, offences = _analyse("""
        from __future__ import annotations

        class SignatureOnly:
            def list(self): ...
            def get(self) -> list[str]: ...
    """)
    assert shadowed == {"list"}
    assert offences == set()


def test_function_scope_annotations_are_out_of_scope() -> None:
    """A local annotation is never evaluated; a nested def resolves elsewhere."""
    shadowed, offences = _analyse("""
        class Local:
            def list(self): ...
            def get(self) -> None:
                source_ids: list[str] = []

                def inner() -> list[str]: ...
    """)
    assert shadowed == {"list"}
    assert offences == set()
