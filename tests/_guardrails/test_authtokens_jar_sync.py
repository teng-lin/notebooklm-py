"""Pin AuthTokens cookie-shadow ownership and compatibility sync-back.

ADR-0032 Phase A makes the kernel-owned ``httpx.Cookies`` the sole first-party
live authority. ``AuthTokens.cookies`` and ``AuthTokens.cookie_jar`` remain two
public compatibility shadows during the v0.x runway. Assigning the jar directly
would still leave those public projections describing different sessions, even
though transport, routing, recovery, and persistence no longer read them.

``replace_cookie_jar`` rebinds both. This gate keeps a future rebind from
reintroducing the public divergence and equality-pins every statically provable
cookie-shadow read/write. Direct assignments are scanned anywhere under
``src/``, excluding ``_auth/tokens.py`` itself, which legitimately owns
``__post_init__`` and ``replace_cookie_jar``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_SRC = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
#: The class's own module: it defines the field and the sanctioned rebind.
_OWNER = "_auth/tokens.py"
_COOKIE_SHADOWS = frozenset(
    {
        "cookies",
        "cookie_jar",
        "jar",
        "flat_cookies",
        "cookie_header",
        "cookie_header_for",
    }
)


class _AuthTokensShadowVisitor(ast.NodeVisitor):
    """Inventory bounded static AuthTokens receivers and compatibility writes.

    Function parameters/locals annotated with ``AuthTokens``, same-scope direct
    aliases, owner ``self``, attributes ending in ``auth``/``_auth``/``tokens``,
    and call-return receivers are tracked. Calls and containers that *carry* an
    AuthTokens to an unrelated name remain outside this bounded proof; the
    model deliberately errs toward treating ambiguous getter returns as
    candidates, with equality-pinned non-AuthTokens exceptions below.
    """

    def __init__(self, *, path: str) -> None:
        self.path = path
        self.class_names: list[str] = []
        self.scope_names: list[str] = []
        self.receiver_stack: list[set[str]] = [set()]
        self.hits: Counter[tuple[str, str, str, str, str]] = Counter()

    @staticmethod
    def _annotation_mentions_auth_tokens(annotation: ast.expr | None) -> bool:
        return annotation is not None and "AuthTokens" in ast.unparse(annotation)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_names.append(node.name)
        self.generic_visit(node)
        self.class_names.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        receivers = set(self.receiver_stack[-1])
        receivers.update(
            arg.arg for arg in args if self._annotation_mentions_auth_tokens(arg.annotation)
        )
        if self.class_names and self.class_names[-1] == "AuthTokens" and args:
            receivers.add(args[0].arg)
        self.scope_names.append(node.name)
        self.receiver_stack.append(receivers)
        self.generic_visit(node)
        self.receiver_stack.pop()
        self.scope_names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _is_auth_tokens_receiver(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.receiver_stack[-1] or node.id in {"auth", "auth_tokens"}
        if isinstance(node, ast.Attribute):
            return node.attr in {"auth", "_auth", "auth_tokens", "tokens"}
        # A getter/cast/factory result followed immediately by a cookie-shadow
        # attribute is ambiguous. Scan it conservatively; known httpx/kernel
        # getters are equality-pinned as non-AuthTokens candidates below.
        return isinstance(node, ast.Call)

    def _is_auth_tokens_alias_source(self, node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return self._is_auth_tokens_receiver(node)
        callee = ast.unparse(node.func).lower()
        annotation = ast.unparse(node.args[0]) if node.args else ""
        return "auth" in callee or "token" in callee or "AuthTokens" in annotation

    def _record(self, node: ast.Attribute, *, context: str) -> None:
        scope = ".".join([*self.class_names, *self.scope_names]) or "<module>"
        receiver = ast.unparse(node.value)
        self.hits[(self.path, scope, receiver, node.attr, context)] += 1

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if self._is_auth_tokens_alias_source(node.value):
            self.receiver_stack[-1].update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if isinstance(node.target, ast.Name) and (
            self._annotation_mentions_auth_tokens(node.annotation)
            or (node.value is not None and self._is_auth_tokens_alias_source(node.value))
        ):
            self.receiver_stack[-1].add(node.target.id)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace_cookie_jar"
            and self._is_auth_tokens_receiver(node.func.value)
        ):
            self._record(node.func, context="Call")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _COOKIE_SHADOWS and self._is_auth_tokens_receiver(node.value):
            self._record(node, context=type(node.ctx).__name__)
        self.generic_visit(node)


def _raw_cookie_shadow_candidates() -> set[tuple[str, str, str, str, str, int]]:
    hits: Counter[tuple[str, str, str, str, str]] = Counter()
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        visitor = _AuthTokensShadowVisitor(path=rel)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        hits.update(visitor.hits)
    return {(*key, count) for key, count in hits.items()}


# Conservative call-return scanning also sees known non-AuthTokens owners such
# as ``get_http_client().cookies``. Pin those exact expressions rather than
# weakening getter coverage; stale exceptions fail alongside stale inventory.
_NON_AUTHTOKENS_CANDIDATES: frozenset[tuple[str, str, str, str, str, int]] = frozenset(
    {
        (
            "_auth/session.py",
            "_try_headless_reauth",
            "kernel.get_http_client(expected_epoch=expected_epoch)",
            "cookies",
            "Load",
            1,
        ),
        (
            "_auth/session.py",
            "_try_master_token_reauth",
            "kernel.get_http_client(expected_epoch=expected_epoch)",
            "cookies",
            "Load",
            1,
        ),
        (
            "_auth/session.py",
            "_try_refresh_cmd_reauth",
            "kernel.get_http_client(expected_epoch=expected_epoch)",
            "cookies",
            "Load",
            1,
        ),
        (
            "_auth/session.py",
            "_try_storage_cookie_reload",
            "kernel.get_http_client(expected_epoch=expected_epoch)",
            "cookies",
            "Load",
            1,
        ),
        (
            "_web/sources/upload.py",
            "SourceUploadPipeline._live_cookies",
            "self._kernel.get_http_client(expected_epoch=expected_epoch)",
            "cookies",
            "Load",
            1,
        ),
    }
)


def _cookie_shadow_inventory() -> set[tuple[str, str, str, str, str, int]]:
    candidates = _raw_cookie_shadow_candidates()
    assert set(_NON_AUTHTOKENS_CANDIDATES) <= candidates
    return candidates - set(_NON_AUTHTOKENS_CANDIDATES)


# Every statically provable first-party read/write of an AuthTokens cookie
# compatibility shadow. Roles are documented in ADR-0032's Phase-A audit.
# Equality-pinned: additions and stale entries both fail review. The kernel's
# two reads are bootstrap-only; every other entry is contained in the public
# compatibility owner itself.
_COOKIE_SHADOW_INVENTORY: frozenset[tuple[str, str, str, str, str, int]] = frozenset(
    {
        (
            "_auth/session.py",
            "_try_storage_cookie_reload",
            "auth",
            "replace_cookie_jar",
            "Call",
            1,
        ),
        ("_auth/tokens.py", "AuthTokens.__post_init__", "self", "cookie_jar", "Load", 1),
        ("_auth/tokens.py", "AuthTokens.__post_init__", "self", "cookie_jar", "Store", 1),
        ("_auth/tokens.py", "AuthTokens.__post_init__", "self", "cookies", "Load", 2),
        ("_auth/tokens.py", "AuthTokens.__post_init__", "self", "cookies", "Store", 1),
        ("_auth/tokens.py", "AuthTokens.__repr__", "self", "cookie_jar", "Load", 1),
        ("_auth/tokens.py", "AuthTokens.__repr__", "self", "cookies", "Load", 1),
        (
            "_auth/tokens.py",
            "AuthTokens._replace_profile_session",
            "self",
            "replace_cookie_jar",
            "Call",
            1,
        ),
        (
            "_auth/tokens.py",
            "AuthTokens.cookie_header_for",
            "self",
            "cookie_jar",
            "Load",
            2,
        ),
        (
            "_auth/tokens.py",
            "AuthTokens._flat_cookie_projection",
            "self",
            "cookies",
            "Load",
            1,
        ),
        ("_auth/tokens.py", "AuthTokens.jar", "self", "cookie_jar", "Load", 2),
        (
            "_auth/tokens.py",
            "AuthTokens.replace_cookie_jar",
            "self",
            "cookie_jar",
            "Store",
            1,
        ),
        (
            "_auth/tokens.py",
            "AuthTokens.replace_cookie_jar",
            "self",
            "cookies",
            "Store",
            1,
        ),
        ("_web/transport/kernel.py", "Kernel._bootstrap_cookies", "auth", "cookie_jar", "Load", 1),
        ("_web/transport/kernel.py", "Kernel._bootstrap_cookies", "auth", "cookies", "Load", 1),
        (
            "_web/transport/auth.py",
            "AuthRefreshCoordinator.update_auth_headers",
            "auth",
            "replace_cookie_jar",
            "Call",
            1,
        ),
    }
)


def test_cookie_shadow_inventory_is_exact() -> None:
    assert _cookie_shadow_inventory() == set(_COOKIE_SHADOW_INVENTORY)


@pytest.mark.parametrize(
    ("source", "receiver", "attribute", "context"),
    [
        (
            "def f(auth: AuthTokens):\n alias = auth\n return alias.cookie_jar\n",
            "alias",
            "cookie_jar",
            "Load",
        ),
        ("class C:\n def f(self):\n  return self._auth.cookies\n", "self._auth", "cookies", "Load"),
        (
            "def f(holder):\n return holder.auth.flat_cookies\n",
            "holder.auth",
            "flat_cookies",
            "Load",
        ),
        ("def f():\n return get_auth().cookie_jar\n", "get_auth()", "cookie_jar", "Load"),
        (
            "def f(auth: AuthTokens, jar):\n auth.replace_cookie_jar(jar)\n",
            "auth",
            "replace_cookie_jar",
            "Call",
        ),
    ],
)
def test_shadow_inventory_catches_bounded_receiver_spellings(
    source: str,
    receiver: str,
    attribute: str,
    context: str,
) -> None:
    visitor = _AuthTokensShadowVisitor(path="example.py")
    visitor.visit(ast.parse(source))
    assert any(key[2:] == (receiver, attribute, context) for key in visitor.hits), visitor.hits


def _direct_assignments() -> list[str]:
    """Return ``file:line`` for every ``<obj>.cookie_jar = ...`` outside the owner."""
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == _OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "cookie_jar":
                    # ``self.cookie_jar = ...`` inside an unrelated class (e.g. a
                    # transport wrapper owning its own jar) is not an AuthTokens
                    # rebind; those live outside _auth and are matched by name
                    # only, so keep the exclusion explicit rather than implicit.
                    hits.append(f"{rel}:{node.lineno}")
    return hits


#: Assignments that are NOT ``AuthTokens`` rebinds. SHRINK-ONLY.
_NON_AUTHTOKENS: frozenset[str] = frozenset()


def test_no_direct_cookie_jar_rebind() -> None:
    """A rebind must go through ``replace_cookie_jar`` so both views move."""
    found = set(_direct_assignments()) - _NON_AUTHTOKENS
    assert not found, (
        "Direct ``.cookie_jar = ...`` assignment(s) outside _auth/tokens.py:\n"
        + "\n".join(f"  {h}" for h in sorted(found))
        + "\n\nUse ``auth.replace_cookie_jar(jar)`` — assigning the field alone "
        "leaves the public ``auth.cookies`` describing the previous session.\n"
        "If the target is not an AuthTokens (a transport owning its own jar, "
        "say), add it to _NON_AUTHTOKENS with that reason."
    )


def test_the_sanctioned_rebind_exists_and_sets_both() -> None:
    """The method the gate points callers at must actually set both views.

    Without this the gate could keep redirecting people to a method that had
    silently lost half its job — the failure mode where a guardrail's name
    outlives what it verifies.
    """
    source = (_SRC / "_auth" / "tokens.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "replace_cookie_jar"
        ),
        None,
    )
    assert fn is not None, "AuthTokens.replace_cookie_jar disappeared"

    assigned = {
        t.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute)
    }
    assert {"cookie_jar", "cookies"} <= assigned, (
        f"replace_cookie_jar must set BOTH views; it sets {sorted(assigned)}"
    )
