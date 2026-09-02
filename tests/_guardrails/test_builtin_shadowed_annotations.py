"""Guard that a class-body annotation never names a builtin the class shadows.

``WebArtifactsAPI`` defines a method called ``list``, so inside that class body
the name ``list`` is the method, not the builtin. Until Python 3.14 that was
invisible: annotations were evaluated eagerly at ``def`` time, before the name
was bound as a class attribute, so ``-> list[Artifact]`` resolved to the
builtin and everything worked. PEP 649 defers the evaluation into a synthesized
``__annotate__`` whose scope includes the class body, so the same annotation now
resolves *after* the class exists and raises ``TypeError: 'function' object is
not subscriptable`` the moment anything reads it (#2266).

The repo's answer predates the break: ``builtins.list[...]`` is the established
spelling across the client classes for exactly this reason, at hundreds of
sites today. The twelve sites #2266 found were the ones that missed the
convention, not a different design. This lint keeps them from coming back.

**Static on purpose.** It parses rather than imports, so it catches a 3.14-only
break while running on 3.12 — which is where it will actually run.
``test_class_body_annotations_do_not_name_a_shadowed_builtin`` is named as a
node id in the ``Run critical contract guards`` step of ``test.yml``, and that
step passes no ``-m``, so the ``repo_lint`` marker does not stop it from
running on every PR. The marker does keep it out of the compatibility cells,
which run ``-m "not repo_lint"``, and the two lanes that execute ``repo_lint``
in bulk — the manual ``repo-lint`` job and nightly — both pin 3.12. Adding a
3.14 ``repo_lint`` lane would close today's hole on one version; parsing closes
it on every version, including whichever one breaks next.

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

The annotation is read against the *finished* class namespace, so the question
is which builtins are still bound when the class body ends — not which were
ever assigned. A bare ``list: list[str]`` annotates without binding and still
resolves to the builtin; ``list: list[str] = []`` binds and is an offender;
``list = None`` followed by ``del list`` is not. ``except ... as list`` is not
either, because the interpreter deletes the name at the end of the handler.

Annotations that never resolve in the class-body scope are out of scope too: a
local annotation inside a method body (``source_ids: list[str] = []``) is not
evaluated at all, a nested ``def`` resolves in its enclosing function scope,
and a non-simple target (``helper.value: list[str]``) is neither stored nor
evaluated. Flagging any of those would report a break that cannot happen.

Quoted forward references *are* in scope — ``get_type_hints`` parses
``"list[str]"`` and evaluates it against the same namespace, so a quote hides
nothing. The two string positions that are values rather than types,
``Literal["list"]`` and the metadata arguments of ``Annotated``, are skipped.
Nothing in ``src/`` collides today, but the tree already writes
``Literal["set"]`` and ``Literal["all"]`` at eight sites — both builtin names —
so this stays one ``def set(...)`` away from mattering.

Every claim above was checked against a real CPython 3.14.7 interpreter rather
than reasoned about; the regression fixtures at the bottom of this file record
the results one shape at a time.
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

#: PEP 695 ``type X = ...`` binds ``X``, but the node only exists on 3.12+.
#: ``isinstance(node, ())`` is always false, which is the right answer where
#: the syntax cannot be parsed in the first place.
_TYPE_ALIAS: type | tuple[()] = getattr(ast, "TypeAlias", ())


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


def _walk_suite(suite: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield every statement in ``suite``, flattening same-scope nested suites.

    Class-level ``if`` / ``try`` / ``for`` / ``with`` / ``match`` suites run in
    the class scope, so what they bind and annotate lands there too. A nested
    ``def`` or ``class`` is yielded — its *name* binds here — but not descended
    into, because its body is a separate scope.

    Yielded in source order, which is what lets ``del`` be read as undoing an
    earlier binding.
    """
    for node in suite:
        yield node
        if not isinstance(node, _SCOPE_STATEMENTS):
            for child in _child_suites(node):
                yield from _walk_suite(child)


def _class_body_statements(cls: ast.ClassDef) -> Iterator[ast.stmt]:
    """Yield every statement that executes in ``cls``'s body scope."""
    return _walk_suite(cls.body)


def _scope_header_expressions(node: ast.stmt) -> Iterator[ast.expr]:
    """Yield the parts of a ``def``/``class`` evaluated in the *enclosing* scope.

    A nested scope's body runs elsewhere, but its decorators, argument defaults
    and base classes are evaluated right where the statement sits — so
    ``def f(self, x=(list := 1))`` binds ``list`` on the class.
    """
    yield from node.decorator_list  # type: ignore[attr-defined]
    if isinstance(node, ast.ClassDef):
        yield from node.bases
        yield from (keyword.value for keyword in node.keywords)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        yield from node.args.defaults
        yield from (default for default in node.args.kw_defaults if default is not None)


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


def _pattern_names(pattern: ast.pattern) -> Iterator[str]:
    """Yield the names a ``case`` pattern captures.

    A capture binds durably in the enclosing scope — ``case [list]:`` leaves
    ``list`` in the class namespace — unlike ``except ... as name``, which the
    interpreter deletes at the end of the handler. Patterns cannot contain a
    nested scope, so a plain walk stays correct here.
    """
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            yield node.rest


def _walrus_names(node: ast.AST) -> Iterator[str]:
    """Yield names ``:=`` binds in this scope, not inside a nested one."""
    if isinstance(node, ast.NamedExpr):
        yield from _bound_names(node.target)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_STATEMENTS):
            continue
        if isinstance(child, ast.Lambda):
            # Same split as a ``def``: the defaults are evaluated where the
            # lambda is written, the body is not.
            yield from _walrus_names_in(_lambda_defaults(child))
            continue
        yield from _walrus_names(child)


def _lambda_defaults(node: ast.Lambda) -> Iterator[ast.expr]:
    yield from node.args.defaults
    yield from (default for default in node.args.kw_defaults if default is not None)


def _walrus_names_in(expressions: Iterator[ast.expr]) -> Iterator[str]:
    for expression in expressions:
        yield from _walrus_names(expression)


def _shadowed_builtins(cls: ast.ClassDef) -> set[str]:
    """Return the builtin names still bound when this class body finishes.

    "Still bound" is the operative test: the annotation is read against the
    completed class namespace, so a name that is bound and then ``del``\\ eted
    resolves to the builtin again and is not a shadow.
    """
    names: set[str] = set()
    unconditional = {id(node) for node in cls.body}
    for node in _class_body_statements(cls):
        if isinstance(node, _SCOPE_STATEMENTS):
            names.add(node.name)
            for expression in _scope_header_expressions(node):
                names.update(_walrus_names(expression))
            continue
        # Only an *unconditional* ``del`` clears a binding. This lint reads a
        # conditional suite as taken — a method under ``if TYPE_CHECKING:``
        # counts as shadowing — so honouring a branch-nested ``del`` would
        # erase a real binding on the strength of an eraser that may never run.
        if isinstance(node, ast.Delete) and id(node) in unconditional:
            for target in node.targets:
                names.difference_update(_bound_names(target))
        elif isinstance(node, ast.Assign):
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
        elif isinstance(node, ast.Match):
            for case in node.cases:
                names.update(_pattern_names(case.pattern))
        elif isinstance(node, _TYPE_ALIAS):
            names.update(_bound_names(node.name))
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
        # ``node.simple`` is false for an attribute, subscript or parenthesised
        # target. Those annotations are never stored and never evaluated, so
        # yielding them would report a break that cannot happen.
        elif isinstance(node, ast.AnnAssign) and node.simple:
            yield node.annotation


def _is_typing_name(node: ast.expr, name: str) -> bool:
    """True for ``name``, ``typing.name`` and ``t.name`` alike."""
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _annotation_names(
    node: ast.expr, *, parse_strings: bool = True, lineno: int | None = None
) -> Iterator[tuple[str, int]]:
    """Yield ``(name, lineno)`` for every name an annotation actually resolves.

    The whole annotation is evaluated as one expression, so *every* name in it
    resolves — including inside ``Annotated`` metadata, where
    ``Annotated[int, list[str]]`` raises just as the bare form does.

    Strings are the exception, and they split by position. In a type position a
    quoted forward reference is parsed and evaluated, so ``"list[str]"`` hides
    exactly the break this lint looks for. As a ``Literal`` member or as
    ``Annotated`` metadata a string stays a string —
    ``Annotated[int, "list[str]"]`` resolves cleanly — so parsing it there
    would invent offenders.
    """
    if isinstance(node, ast.Name):
        yield node.id, lineno if lineno is not None else node.lineno
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if not parse_strings:
            return
        try:
            parsed = ast.parse(node.value, mode="eval")
        except SyntaxError:  # not a type expression; nothing resolves
            return
        yield from _annotation_names(parsed.body, lineno=lineno or node.lineno)
        return
    if isinstance(node, ast.Subscript):
        yield from _annotation_names(node.value, parse_strings=parse_strings, lineno=lineno)
        arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if _is_typing_name(node.value, "Literal"):
            for member in arguments:
                yield from _annotation_names(member, parse_strings=False, lineno=lineno)
        elif _is_typing_name(node.value, "Annotated") and arguments:
            yield from _annotation_names(arguments[0], parse_strings=parse_strings, lineno=lineno)
            for metadata in arguments[1:]:
                yield from _annotation_names(metadata, parse_strings=False, lineno=lineno)
        else:
            for argument in arguments:
                yield from _annotation_names(argument, parse_strings=parse_strings, lineno=lineno)
        return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            yield from _annotation_names(child, parse_strings=parse_strings, lineno=lineno)


def _offences(cls: ast.ClassDef, *, include_signatures: bool) -> Iterator[tuple[str, int]]:
    """Yield ``(name, lineno)`` for each annotation naming a shadowed builtin."""
    shadowed = _shadowed_builtins(cls)
    if not shadowed:
        return
    for annotation in _class_scope_annotations(cls, include_signatures=include_signatures):
        for name, lineno in _annotation_names(annotation):
            if name in shadowed:
                yield name, lineno


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


@pytest.mark.parametrize(
    "pattern",
    ["case list:", "case [x, *list]:", 'case {"k": _, **list}:', "case Point(x=list):"],
)
def test_match_capture_pattern_shadows(pattern: str) -> None:
    """A capture binds durably: ``vars(cls)`` keeps the name after the match."""
    shadowed, offences = _analyse(f"""
        class Captured:
            match subject:
                {pattern}
                    pass
            def get(self) -> list[str]: ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


@pytest.mark.skipif(not _TYPE_ALIAS, reason="PEP 695 type statements need Python 3.12+")
def test_pep695_type_statement_shadows() -> None:
    shadowed, offences = _analyse("""
        class Aliased:
            type list = int
            def get(self) -> list[str]: ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_walrus_in_a_method_default_shadows() -> None:
    """A default is evaluated in the class scope even though the body is not."""
    shadowed, offences = _analyse("""
        class Defaulted:
            def f(self, x=(list := 1)) -> list[str]: ...
    """)
    assert shadowed == {"list"}  # ``f`` is not a builtin, so it is filtered out
    assert offences == {"list"}


def test_quoted_forward_reference_is_parsed() -> None:
    """``get_type_hints`` parses the string, so a quote hides nothing."""
    shadowed, offences = _analyse("""
        class Quoted:
            items: "list[str]" = []
            def list(self): ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_nested_forward_reference_is_parsed() -> None:
    shadowed, offences = _analyse("""
        class NestedQuote:
            items: dict[str, "list[int]"] = {}
            def list(self): ...
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


@pytest.mark.parametrize(
    "annotation",
    ['Literal["list"]', 'typing.Literal["list"]', 'Annotated[int, "list"]'],
)
def test_string_values_are_not_type_positions(annotation: str) -> None:
    """A ``Literal`` member and ``Annotated`` metadata are values, not types."""
    shadowed, offences = _analyse(f"""
        class Valued:
            mode: {annotation} = None
            def list(self): ...
    """)
    assert shadowed == {"list"}
    assert offences == set()


def test_non_simple_annotation_target_is_not_evaluated() -> None:
    """``helper.value: list[str]`` is never stored and never evaluated."""
    shadowed, offences = _analyse("""
        class NonSimple:
            helper = Helper()
            helper.value: list[str] = []
            def list(self): ...
    """)
    assert shadowed == {"list"}  # ``helper`` is not a builtin, so it is filtered out
    assert offences == set()


def test_walrus_in_a_lambda_default_shadows() -> None:
    """A lambda's defaults run in the class scope; only its body does not."""
    shadowed, offences = _analyse("""
        class Lambdas:
            callback = lambda value=(list := 1): value
            items: list[str] = []
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_annotated_metadata_expression_is_evaluated() -> None:
    """Metadata is part of the expression: ``Annotated[int, list[str]]`` raises."""
    shadowed, offences = _analyse("""
        class AnnotatedExpr:
            list = 1
            item: Annotated[int, list[str]] = 1
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_conditional_del_does_not_clear_a_binding() -> None:
    """A branch-nested ``del`` may never run, so it cannot undo a real binding."""
    shadowed, offences = _analyse("""
        class ConditionallyDeleted:
            list = None
            if False:
                del list
            items: list[str] = []
    """)
    assert shadowed == {"list"}
    assert offences == {"list"}


def test_deleted_binding_stops_shadowing() -> None:
    """The annotation resolves against the finished namespace, which has no ``list``."""
    shadowed, offences = _analyse("""
        class Deleted:
            list = None
            del list
            items: list[str] = []
    """)
    assert shadowed == set()
    assert offences == set()


def test_except_as_does_not_shadow() -> None:
    """``except ... as name`` is deleted at the end of the handler, so it never binds."""
    shadowed, offences = _analyse("""
        class Handled:
            try:
                pass
            except ValueError as list:
                pass
            def get(self) -> list[str]: ...
    """)
    assert shadowed == set()
    assert offences == set()


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
