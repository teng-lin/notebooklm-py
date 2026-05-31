"""CLI RPC-command error-envelope enforcement.

Any Click leaf command (a function decorated ``@<group>.command(...)``) that
does RPC -- i.e. its call graph reaches ``NotebookLMClient`` -- MUST also route
that RPC through the shared error envelope (``handle_errors`` /
``with_auth_and_errors`` / ``run_client_workflow``; equivalently the
``@with_client`` decorator). Otherwise an auth / network / RPC failure is not
translated into the typed ``{"error": true, "code": ...}`` JSON envelope and a
non-zero exit code -- it surfaces as a raw traceback or, worse, is silently
swallowed.

Lineage
-------
This is the network-command sibling of the exit-path marker gates
(``tests/_lint/test_error_handler_allowlist.py``):

* #1298 / #1299 -- markered, line-shift-immune exit-path allowlist (the
  inline-marker convention reused here).
* #1307 -- widened the exit-path family to envelope-bypassing ``ClickException``
  subclasses.
* #1309 -- the live bug this gate prevents: ``language get`` / ``language set``
  opened ``NotebookLMClient`` *outside* the envelope and silently swallowed
  server errors. A body-only audit missed it; only a *transitive,
  decorator-aware* call-graph walk found it (the ``@with_client`` decorator is
  how 55 commands reach the envelope, so a decorator-blind scan false-flags all
  of them). Fixed in #1310; this lint makes the ``@with_client`` convention
  *enforced*, not merely trusted.

Inline-waiver convention
-------------------------
Mirroring the ``# noqa`` / ``# cli-input-validation:`` style, a command may
opt out with an inline ``# cli-rpc-unenveloped: <reason>`` comment anywhere
within its function body. The reason lives at the site, so (unlike a central
allowlist) it is immune to line shifts and self-documents the conscious choice.
A waiver on a command that is NOT a violation (does not reach RPC, or already
reaches the envelope) is STALE and fails the gate -- waivers cannot linger
after a command is fixed (anti-rot, mirroring the exit-path stale-marker check).
There are zero waivers today.

Honest caveat (scope)
---------------------
This is a NAME-BASED call-graph heuristic -- a safety net, not a proof. It walks
function defs under ``src/notebooklm/cli/**`` by name, so it can MISS RPC reached
through an indirection the name-walk cannot follow (e.g. an opened client passed
in as an opaque parameter, or a dynamic ``getattr`` dispatch). False positives
are unlikely because ``NotebookLMClient`` is a distinctive, unambiguous name.
Same heuristic class as the existing exit-path gates -- strictly better than
trusting the convention by hand.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path

from _fixtures.cli_exit_markers import marker_reasons, marker_reasons_for, parse_cli_file

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli"

#: Inline waiver: ``# cli-rpc-unenveloped: <reason>`` within a command's body.
UNENVELOPED_MARKER = "cli-rpc-unenveloped:"

#: A command whose call graph reaches this name opens a client and does RPC.
RPC_TARGETS = frozenset({"NotebookLMClient"})

#: Reaching any of these means the RPC is wrapped by the error envelope.
#: ``with_client`` (a decorator) and ``run_client_workflow`` both funnel into
#: ``with_auth_and_errors`` -> ``handle_errors``, so naming the funnels suffices.
ENVELOPE_TARGETS = frozenset({"handle_errors", "with_auth_and_errors", "run_client_workflow"})

#: AST node types that define a (possibly nested) callable.
_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)

CallGraph = dict[str, list[frozenset[str]]]


def _names_in(nodes: Iterable[ast.AST]) -> set[str]:
    """Every called name (``Name`` id / ``Attribute`` attr) and bare ``Name``.

    Captures both ``foo()`` / ``obj.bar()`` call targets AND bare references
    like ``NotebookLMClient`` used as ``async with NotebookLMClient(...)`` (a
    ``Call`` whose ``func`` is a ``Name``) or passed as a factory argument. The
    attr leaf of an ``Attribute`` is included so ``settings.get_output_language``
    contributes ``get_output_language`` -- the same lossy-but-distinctive
    matching the exit-path gate uses.
    """
    names: set[str] = set()
    for root in nodes:
        for child in ast.walk(root):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
            elif isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
    return names


def _body_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Referenced names in a function's body + signature, EXCLUDING decorators.

    Excluding ``decorator_list`` keeps decorator-awareness an explicit, separate
    contribution (:func:`_decorator_names`) rather than smuggling decorator names
    into every body walk -- so the call graph an inner def builds is its real
    callee set, and the "body alone vs. decorator-aware" distinction is honest.
    """
    return _names_in([func.args, *func.body])


def _decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names referenced by a command's decorators (``@with_client`` etc.).

    Decorator-awareness is load-bearing: ``@with_client`` is how the bulk of
    commands reach the envelope, so a body-only start set would false-flag them
    all (the #1309 lesson). A ``@grp.command(...)`` decorator is a ``Call``; we
    walk its target so ``with_client`` (a bare-``Name`` decorator) is captured.
    """
    names: set[str] = set()
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
    return names


def _is_click_command(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff *func* carries a ``@<group>.command(...)`` (leaf-command) marker.

    Matches the ``Attribute`` ``.command`` leaf in either the called form
    ``@grp.command("name")`` or the (rare) attribute form ``@grp.command`` --
    ``grp`` may be a module-level group (``@artifact.command``) or the ``cli``
    group passed into a ``register_*`` factory (``@cli.command``).
    """
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "command":
            return True
    return False


def _build_call_graph(trees: Iterable[ast.Module]) -> CallGraph:
    """Map ``function name -> name sets of every same-named def in the CLI tree``.

    Each def contributes the referenced-name set of its WHOLE subtree (including
    nested defs), so a command's nested ``async def body()/ _run()`` -- where the
    actual ``NotebookLMClient`` / ``with_auth_and_errors`` references live -- is
    folded into the enclosing name's reachability. Over-approximating by design:
    this is a safety net, and missing an edge is the failure mode we avoid.
    """
    graph: CallGraph = defaultdict(list)
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, _FUNC_DEFS):
                graph[node.name].append(frozenset(_body_names(node)))
    return graph


def _reaches(start: frozenset[str], targets: frozenset[str], graph: CallGraph) -> bool:
    """BFS the call graph from *start*; True if any name in *targets* is hit.

    Each visited name expands to the union of the name sets of every CLI def
    sharing that name (a name with several definitions over-approximates to all
    of them). Terminates because the visited set is bounded by the finite name
    universe.
    """
    seen: set[str] = set(start)
    queue: deque[str] = deque(start)
    while queue:
        name = queue.popleft()
        if name in targets:
            return True
        for callset in graph.get(name, ()):
            for callee in callset:
                if callee not in seen:
                    seen.add(callee)
                    queue.append(callee)
    return False


def _command_start_set(func: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """A command's reachability seed: body references UNION decorator names."""
    return frozenset(_body_names(func) | _decorator_names(func))


def _command_is_waived(
    func: ast.FunctionDef | ast.AsyncFunctionDef, marker_lines: set[int]
) -> bool:
    """True iff a ``# cli-rpc-unenveloped:`` marker sits within the command span.

    The span is the command's ``[lineno, end_lineno]`` (inclusive, as ``ast``
    reports), which covers the body where the RPC happens.
    """
    end = func.end_lineno or func.lineno
    return any(func.lineno <= line <= end for line in marker_lines)


def _cli_files() -> list[Path]:
    return sorted(CLI_ROOT.rglob("*.py"))


def _audit() -> tuple[list[str], list[str], int]:
    """Return ``(violations, stale_waivers, rpc_command_count)`` over the CLI tree.

    A violation is a command that reaches RPC, does NOT reach the envelope, and
    is NOT waived. A stale waiver is a ``# cli-rpc-unenveloped:`` marker on a
    command that is not otherwise a violation (so the waiver is unnecessary).
    """
    parsed = [(path, parse_cli_file(path)) for path in _cli_files()]
    graph = _build_call_graph(tree for _path, (_src, tree) in parsed)

    violations: list[str] = []
    stale: list[str] = []
    rpc_commands = 0
    for path, (_src, tree) in parsed:
        rel = path.relative_to(REPO_ROOT).as_posix()
        marker_lines = set(marker_reasons_for(path, UNENVELOPED_MARKER))
        for node in ast.walk(tree):
            if not isinstance(node, _FUNC_DEFS) or not _is_click_command(node):
                continue
            start = _command_start_set(node)
            reaches_rpc = _reaches(start, RPC_TARGETS, graph)
            reaches_env = _reaches(start, ENVELOPE_TARGETS, graph)
            waived = _command_is_waived(node, marker_lines)
            is_violation = reaches_rpc and not reaches_env

            if reaches_rpc:
                rpc_commands += 1
            if is_violation and not waived:
                violations.append(f"{rel}:{node.lineno} {node.name}")
            if waived and not is_violation:
                stale.append(f"{rel}:{node.lineno} {node.name}")
    return violations, stale, rpc_commands


def _format(sites: list[str]) -> str:
    return "\n".join(f"  {site}" for site in sorted(sites))


def test_rpc_commands_route_through_error_envelope() -> None:
    """Every RPC-doing CLI command reaches the error envelope (or is waived)."""
    violations, _stale, rpc_commands = _audit()
    # Sanity floor: the heuristic must still see the network-command class.
    # If this drops sharply the call graph likely stopped resolving an edge.
    assert rpc_commands >= 50, (
        f"Only {rpc_commands} CLI commands reach NotebookLMClient; the call-graph "
        "walk likely regressed (expected ~60). Investigate before trusting a green."
    )
    assert not violations, (
        "CLI commands that open NotebookLMClient (do RPC) but do NOT route through "
        "the error envelope (handle_errors / with_auth_and_errors / "
        "run_client_workflow, i.e. @with_client). Each silently bypasses the typed "
        "JSON error envelope -- wrap it, or add an inline "
        f"`# {UNENVELOPED_MARKER} <reason>` waiver:\n" + _format(violations)
    )


def test_no_stale_unenveloped_waivers() -> None:
    """``# cli-rpc-unenveloped:`` markers cannot linger on non-violating commands."""
    _violations, stale, _rpc = _audit()
    assert not stale, (
        f"Stale `# {UNENVELOPED_MARKER}` waivers on commands that are NOT violations "
        "(they don't reach NotebookLMClient, or already reach the envelope) -- delete "
        "them:\n" + _format(stale)
    )


# --------------------------------------------------------------------------- #
# Unit tests of the reachability + waiver primitives on small synthetic ASTs.
# --------------------------------------------------------------------------- #


def _module(source: str) -> ast.Module:
    return ast.parse(source)


def _only_command(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the single ``@<grp>.command(...)`` def in a synthetic module."""
    commands = [
        node for node in ast.walk(tree) if isinstance(node, _FUNC_DEFS) and _is_click_command(node)
    ]
    assert len(commands) == 1, f"expected exactly one command, found {len(commands)}"
    return commands[0]


def _classify(source: str) -> tuple[bool, bool]:
    """Return ``(is_violation, is_stale_waiver)`` for the lone command in *source*."""
    tree = _module(source)
    graph = _build_call_graph([tree])
    command = _only_command(tree)
    start = _command_start_set(command)
    reaches_rpc = _reaches(start, RPC_TARGETS, graph)
    reaches_env = _reaches(start, ENVELOPE_TARGETS, graph)
    # ``marker_reasons`` is source-keyed (the synthetic tests work in memory);
    # ``marker_reasons_for`` is the path-keyed sibling used in the real audit.
    marker_lines = set(marker_reasons(source, UNENVELOPED_MARKER))
    waived = _command_is_waived(command, marker_lines)
    is_violation = reaches_rpc and not reaches_env
    return (is_violation and not waived), (waived and not is_violation)


def test_rpc_without_envelope_is_flagged() -> None:
    """A command opening NotebookLMClient with no envelope is a violation."""
    source = (
        "def run():\n"
        "    @grp.command('bad')\n"
        "    def bad(ctx):\n"
        "        async def _run():\n"
        "            async with NotebookLMClient(auth) as client:\n"
        "                await client.notebooks.list()\n"
        "        return _run()\n"
    )
    is_violation, is_stale = _classify(source)
    assert is_violation
    assert not is_stale


def test_with_client_decorator_command_passes() -> None:
    """``@with_client`` reaches the envelope via its body's call to the funnel."""
    source = (
        "def with_client(f):\n"
        "    return with_auth_and_errors(ctx, body=f)\n"
        "\n"
        "@grp.command('ok')\n"
        "@with_client\n"
        "def ok(ctx, client_auth):\n"
        "    async def _run():\n"
        "        async with NotebookLMClient(client_auth) as client:\n"
        "            await client.notebooks.list()\n"
        "    return _run()\n"
    )
    is_violation, is_stale = _classify(source)
    assert not is_violation
    assert not is_stale


def test_explicit_envelope_call_command_passes() -> None:
    """A command calling ``with_auth_and_errors`` directly reaches the envelope."""
    source = (
        "@grp.command('ok')\n"
        "def ok(ctx):\n"
        "    async def body(auth):\n"
        "        async with NotebookLMClient(auth) as client:\n"
        "            await client.settings.get_output_language()\n"
        "    return with_auth_and_errors(ctx, body=body)\n"
    )
    is_violation, is_stale = _classify(source)
    assert not is_violation
    assert not is_stale


def test_waived_rpc_command_is_not_flagged() -> None:
    """A violation carrying a non-empty ``# cli-rpc-unenveloped:`` waiver passes."""
    source = (
        "@grp.command('bad')\n"
        "def bad(ctx):\n"
        "    async def _run():  # cli-rpc-unenveloped: intentional raw client\n"
        "        async with NotebookLMClient(auth) as client:\n"
        "            await client.notebooks.list()\n"
        "    return _run()\n"
    )
    is_violation, is_stale = _classify(source)
    assert not is_violation
    assert not is_stale


def test_waiver_on_non_violation_is_stale() -> None:
    """A waiver on a command that does no RPC is stale (anti-rot)."""
    source = (
        "@grp.command('list')\n"
        "def list_codes(json_output):  # cli-rpc-unenveloped: no longer needed\n"
        "    print(SUPPORTED_LANGUAGES)\n"
    )
    is_violation, is_stale = _classify(source)
    assert not is_violation
    assert is_stale


def test_decorator_only_envelope_path_is_recognized() -> None:
    """The envelope can be reached purely through the decorator (no body call).

    This is the dominant real-tree shape: the command body only opens the client
    and the ``@with_client`` decorator supplies the envelope. A decorator-blind
    start set would (wrongly) flag this as a violation.
    """
    source = (
        "def with_client(f):\n"
        "    return run_client_workflow(ctx, body=f)\n"
        "\n"
        "@grp.command('ok')\n"
        "@with_client\n"
        "def ok(ctx, client_auth):\n"
        "    return _do_rpc(NotebookLMClient(client_auth))\n"
    )
    tree = _module(source)
    graph = _build_call_graph([tree])
    command = _only_command(tree)
    body_only = frozenset(_body_names(command))
    full_start = _command_start_set(command)

    # The body alone reaches RPC but NOT the envelope ...
    assert _reaches(body_only, RPC_TARGETS, graph)
    assert not _reaches(body_only, ENVELOPE_TARGETS, graph)
    # ... while the decorator-aware start set does reach the envelope.
    assert _reaches(full_start, ENVELOPE_TARGETS, graph)

    is_violation, is_stale = _classify(source)
    assert not is_violation
    assert not is_stale
