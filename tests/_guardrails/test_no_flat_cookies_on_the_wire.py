"""Guard that no request is built from a domain-blind cookie projection.

The flat ``name -> value`` shape has one slot per cookie name, so when the same
name exists on two domains -- ``OSID`` on the app host *and* on
``accounts.google.com``, which is the ordinary post-rebrand state -- every value
but one is discarded, and *which* one survives is arbitrary: the first entry of
the highest populated tier in ``storage_state`` iteration order. Reordering the
file changes the result (issue #2054).

That is harmless as a *display* projection and fatal as a *request* input. The
affordance produced three bad call sites in one file -- the probe POSTs in
``scripts/check_rpc_health.py`` -- each written by an author who had the "use
``cookie_jar``" docstring available. #2019 fixed that script's auth half and the
request half survived it, which is why a docstring is not enough here: the
property is *named* ``cookie_header``, and the name is an instruction.

(``scripts/diagnose_get_notebook.py`` was broken in the same commit range but by
a *different* defect -- it joined the domain-preserving ``AuthTokens.cookies``
map, whose keys are ``(name, domain, path)`` tuples, into a syntactically
malformed header. This lint does not catch that and should not be read as
covering it.)

So this lint enforces the one rule the docstrings ask for -- **a flat projection
may not reach an HTTP request** -- rather than restating it in prose:

* no ``"Cookie"`` header value derived from ``cookie_header`` / ``flat_cookies``
  / ``load_auth_from_storage(...)``
* no ``cookies=`` keyword to an ``httpx`` client or request built from one

Use :meth:`notebooklm._auth.tokens.AuthTokens.cookie_header_for` (RFC 6265
selection for a specific URL) or pass ``auth.cookie_jar`` to the client.

**Passive ratchet, not a burndown.** ``KNOWN_FLAT_WIRE_SITES`` exists so a
pre-existing site can be recorded rather than block unrelated work. It is empty
today because #2054 cleared every site it detects; entries should be deleted,
never added. Adding one means a new wire-facing flat projection was introduced,
which is precisely what this lint exists to stop.

Scope is deliberately narrow. Reading ``auth.flat_cookies`` to *print* cookie
names, to compare sets, or in a test fixture is legitimate and untouched -- only
the two request-building shapes above are policed.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (REPO_ROOT / "src" / "notebooklm", REPO_ROOT / "scripts")

#: Attribute/function names whose result is a domain-blind ``name -> value`` map.
FLAT_SOURCES = frozenset({"cookie_header", "flat_cookies", "load_auth_from_storage"})

#: ``repo-relative path::line`` for sites that predate the rule. Delete, never add.
KNOWN_FLAT_WIRE_SITES: frozenset[str] = frozenset()


def _is_flat_source(node: ast.AST) -> bool:
    """Return True when ``node`` evaluates -- or wraps -- a flat cookie projection.

    Deliberately structural, not data-flow: it recognises the expression shapes
    a flat projection is written in at the point of use, and does **not** chase
    a value through an intervening variable. ``cookies = auth.cookie_header``
    followed later by ``headers={"Cookie": cookies}`` is a known blind spot;
    closing it needs real alias tracking, which is a different tool. The lint's
    job is to stop the *idiom* being re-copied, and every real instance so far
    (three of them, in one file) was written inline.
    """
    if isinstance(node, ast.Attribute):
        # ``auth.flat_cookies.copy`` -- the outer attribute is ``copy``, so keep
        # walking left until a projection name is found or the chain runs out.
        return node.attr in FLAT_SOURCES or _is_flat_source(node.value)
    # Deliberately NO bare ``ast.Name`` branch. Matching a local by name flags
    # the *correct* pattern -- ``cookie_header = auth.cookie_header_for(url)``
    # followed by ``{"Cookie": cookie_header}`` -- which would punish the fix
    # this lint exists to encourage. ``load_auth_from_storage(...)`` is still
    # caught below as a Call.
    if isinstance(node, ast.Call):
        # A bare name in *callee* position is unambiguous -- ``load_auth_from_storage()``
        # is the loader, whereas a bare name in *value* position may be a local
        # holding a correctly-routed header. That asymmetry is why the general
        # Name branch is omitted but this one is not.
        if isinstance(node.func, ast.Name) and node.func.id in FLAT_SOURCES:
            return True
        # The callee chain (``auth.flat_cookies.copy()``) and the arguments
        # (``dict(auth.flat_cookies)``, ``"; ".join(... auth.flat_cookies.items())``)
        # -- wrapping a projection in another call does not make it domain-aware.
        if _is_flat_source(node.func):
            return True
        return any(
            _is_flat_source(arg) for arg in [*node.args, *(kw.value for kw in node.keywords)]
        )
    # ``f"{auth.cookie_header}"`` / ``"a" + auth.cookie_header``.
    if isinstance(node, ast.JoinedStr):
        return any(
            _is_flat_source(v.value) for v in node.values if isinstance(v, ast.FormattedValue)
        )
    if isinstance(node, ast.BinOp):
        return _is_flat_source(node.left) or _is_flat_source(node.right)
    # ``f"{k}={v}" for k, v in auth.flat_cookies.items()`` -- the generator that
    # ``cookie_header`` is itself built from, i.e. the first thing an author
    # writes after being told not to use the property.
    if isinstance(node, ast.GeneratorExp | ast.ListComp | ast.SetComp):
        return any(_is_flat_source(gen.iter) for gen in node.generators)
    if isinstance(node, ast.DictComp):
        return any(_is_flat_source(gen.iter) for gen in node.generators)
    return False


def _flat_wire_sites(tree: ast.AST) -> list[int]:
    """Return line numbers where a flat projection feeds an HTTP request."""
    hits: list[int] = []

    for node in ast.walk(tree):
        # Shape 1: {"Cookie": <flat>} -- a hand-built header.
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.lower() == "cookie"
                    and _is_flat_source(value)
                ):
                    hits.append(node.lineno)
        # Shape 2: cookies=<flat> -- passed to a client or request constructor.
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "cookies" and _is_flat_source(kw.value):
                    hits.append(node.lineno)
        # Shape 3: headers["Cookie"] = <flat> -- the most ordinary way to set a
        # header in Python, and invisible to a dict-literal-only matcher.
        elif isinstance(node, ast.Assign) and _is_flat_source(node.value):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and target.slice.value.lower() == "cookie"
                ):
                    hits.append(node.lineno)

    return hits


def _python_files() -> list[Path]:
    return sorted(p for root in SCANNED_ROOTS for p in root.rglob("*.py"))


def test_no_flat_cookie_projection_reaches_an_http_request() -> None:
    """A flat ``name -> value`` map must never build a request (issue #2054)."""
    offenders: list[str] = []

    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno in _flat_wire_sites(tree):
            site = f"{rel}::{lineno}"
            if site not in KNOWN_FLAT_WIRE_SITES:
                offenders.append(site)

    assert not offenders, (
        "A domain-blind cookie projection is being used to build an HTTP request. "
        "It keeps one value per cookie name, so a name present on two domains "
        "loses all but one -- arbitrarily (#2054).\n"
        "Use auth.cookie_header_for(url) for a raw header, or pass "
        "auth.cookie_jar as the client's cookies=.\n"
        f"Offending sites: {offenders}"
    )


def test_lint_actually_detects_the_shapes_it_polices() -> None:
    """Fail loudly if the matcher stops matching -- a silent pass is the failure mode.

    Without this, a refactor of ``_flat_wire_sites`` that matched nothing would
    leave the guardrail green forever while policing nothing. That vacuous-pass
    mode is exactly the defect class tracked in #2055.
    """
    header_shape = ast.parse(
        'headers = {"Content-Type": "x", "Cookie": auth.cookie_header}',
    )
    kwarg_shape = ast.parse("client = httpx.AsyncClient(cookies=auth.flat_cookies)")
    call_shape = ast.parse('headers = {"Cookie": load_auth_from_storage(path)}')
    clean_shape = ast.parse(
        "client = httpx.AsyncClient(cookies=auth.cookie_jar)\n"
        "names = sorted(auth.flat_cookies)\n"
        "header = auth.cookie_header_for(url)\n"
    )

    fstring_shape = ast.parse('headers = {"Cookie": f"{auth.cookie_header}"}')
    subscript_shape = ast.parse('headers["Cookie"] = auth.cookie_header')
    comprehension_shape = ast.parse(
        'h = {"Cookie": "; ".join(f"{k}={v}" for k, v in auth.flat_cookies.items())}'
    )
    wrapper_shape = ast.parse("client = httpx.AsyncClient(cookies=dict(auth.flat_cookies))")
    # The *correct* pattern must not be flagged: punishing the fix is worse
    # than missing a violation, because it teaches authors to avoid the lint.
    correct_shape = ast.parse(
        'cookie_header = auth.cookie_header_for(url)\nheaders = {"Cookie": cookie_header}'
    )
    method_shape = ast.parse("client = httpx.AsyncClient(cookies=auth.flat_cookies.copy())")

    assert _flat_wire_sites(header_shape), 'the {"Cookie": <flat>} shape must be detected'
    assert _flat_wire_sites(kwarg_shape), "the cookies=<flat> shape must be detected"
    assert _flat_wire_sites(call_shape), "a flat loader call must be detected"
    assert _flat_wire_sites(fstring_shape), "an f-string wrapper must not defeat the lint"
    assert _flat_wire_sites(method_shape), "a method call on a projection is still the projection"
    assert _flat_wire_sites(subscript_shape), 'headers["Cookie"] = <flat> must be detected'
    assert _flat_wire_sites(comprehension_shape), (
        "the generator cookie_header is built from must be detected -- it is what an "
        "author writes next after being told not to use the property"
    )
    assert _flat_wire_sites(wrapper_shape), "a dict() wrapper must not defeat the lint"
    assert not _flat_wire_sites(clean_shape), "domain-aware usage must not be flagged"
    assert not _flat_wire_sites(correct_shape), (
        "cookie_header_for() assigned to a local named cookie_header is the CORRECT "
        "pattern and must never be flagged"
    )


def test_allowlist_is_empty_so_the_lint_cannot_be_silenced() -> None:
    """``KNOWN_FLAT_WIRE_SITES`` must stay empty.

    The allowlist exists so a *pre-existing* site could be recorded rather than
    block unrelated work — but #2054 cleared both files, so there is nothing
    left to excuse. Without this assertion the cheapest way to satisfy the lint
    is to add the offending line to the allowlist, which turns a ratchet into a
    rubber stamp. If a genuinely unavoidable site ever appears, delete this test
    deliberately and say why in the PR.
    """
    assert frozenset() == KNOWN_FLAT_WIRE_SITES, (
        "the lint is being silenced by allowlisting rather than by fixing the call site"
    )


def test_the_scan_reaches_the_files_this_rule_exists_for() -> None:
    """Fail if the roots stop resolving -- an empty scan is a vacuous pass.

    ``test_no_flat_cookie_projection_reaches_an_http_request`` iterates whatever
    ``_python_files()`` yields, so a moved directory or a mistyped root would
    turn it green while policing nothing (the #2055 defect class). Unparseable
    files need no guard of their own: ``ast.parse`` raises there and fails the
    scan loudly. What it cannot notice is a file never handed to it, so this
    pins the two the rule was written for by name.
    """
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _python_files()}

    assert "scripts/check_rpc_health.py" in scanned
    assert "scripts/diagnose_get_notebook.py" in scanned
    assert "src/notebooklm/_auth/tokens.py" in scanned
