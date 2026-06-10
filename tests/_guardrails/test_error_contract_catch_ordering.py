"""Guard: ADR-0019 catch ordering — narrow transport re-raise before broad
``RPCError`` wrap-and-raise.

**Invariant (ADR-0019, "Cross-cutting" contract row + retry guidance).** Typed
transport faults always propagate as their own type — ``RateLimitError`` (so
callers can back off via ``retry_after``), ``AuthError`` (re-login),
``ServerError`` (transient retry). Because those three subclass the broad
``RPCError`` (``exceptions.py``), a bare ``except RPCError`` clause that
*wraps* into a domain error (``raise SourceAddError(...) from e``) silently
collapses every one of them into the wrapper, and callers can no longer catch
a rate-limited operation. ADR-0019's retry guidance states the general rule:
handle the *narrow* transport exceptions, "never the broad ``RPCError``", when
the broad clause would change what the caller sees.

The sanctioned pattern is the one ``add_url`` / ``add_drive``
(``src/notebooklm/_source/add.py``) and ``register_file_source``
(``src/notebooklm/_source/upload.py``) use::

    try:
        result = await rpc.rpc_call(...)
    except (AuthError, RateLimitError, ServerError, NetworkError):
        raise                                   # narrow types: UNWRAPPED
    except RPCError as e:
        raise SourceAddError(..., cause=e) from e  # residual broad: wrapped

This was documented, not enforced — and re-accreted exactly as ADR-0019's
"Unwanted" section predicted: ``add_text`` shipped with the bare
``except RPCError`` wrap and swallowed ``RateLimitError``/``AuthError``/
``ServerError`` until this gate's introduction fixed it.

**Detector (deliberately NARROW — this violation class only).** Within a
single ``try``, an ``except`` clause that catches ``RPCError`` *by name*
(alone or in a tuple) and whose body raises a **different** exception class
via a direct constructor call (``raise X(...)``, the wrap-and-raise shape)
must be **preceded** in the same ``try`` by an ``except`` clause that catches
at least :data:`REQUIRED_NARROW_RERAISE` and whose body is a bare ``raise``.
Everything else is deliberately ignored:

* a broad clause that **bare-re-raises** (``_rpc_executor.py``'s
  refresh-and-retry catch ends in ``raise``) or re-raises via a helper
  (``_notebooks.py`` ``_raise_quota_error_if_detected(exc)`` + ``raise``) —
  the caller still sees the original type;
* a broad clause that **swallows and continues** (the ``_artifact/listing.py``
  composite-lister partial-availability catch, the ``_research.py`` baseline
  probes, the ``upload.py`` post-upload rename) — deciding those is ADR-0019
  Rule-3 / Scope territory, explicitly out of this gate's contract;
* re-wrapping **into ``RPCError`` itself** — not a *different* class, the
  caller's ``except RPCError`` still works;
* a clause catching only narrow types — no broad swallow to order around.

Known evasions, accepted to keep the detector precise: binding the wrapper
first (``err = X(...)`` … ``raise err``) and re-raising a pre-built exception
name are not detected, and ``except*`` exception groups (:class:`ast.TryStar`,
PEP 654 — zero uses in ``src/`` today) are deliberately out of scope; if any
of those idioms ever appears in a broad-``RPCError`` handler, widen the
detector rather than adopting the idiom.

**Scope.** ADR-0019 scopes the contract to the feature namespaces
(``notebooks``, ``sources``, ``artifacts``, ``chat``, ``research``, ``notes``,
``mind_maps``, ``sharing``, ``settings``) and everything above them; the
``rpc/`` protocol package — where the ``RPCError`` transport subtree
*originates* and broad handling is the layer's own job — is excluded. Every
other file under ``src/notebooklm/`` is scanned.

:data:`REQUIRED_NARROW_RERAISE` is the minimum: the three ``RPCError``
*subclasses* a broad clause would otherwise swallow. The sibling tuple also
re-raises ``NetworkError``, which does **not** subclass ``RPCError`` (it can
never reach the broad clause) but must propagate for ``idempotent_create``'s
probe contract — the gate tolerates it (and any other extras) in the narrow
clause without requiring it. ``test_required_narrow_set_matches_exception_taxonomy``
pins that derivation against the live exception tree.

The :data:`ALLOWLIST` burndown idiom matches the other guardrails: it is
EMPTY (the one violation, ``add_text``, was fixed in the same change that
introduced this gate) and self-draining — do not add entries to grow the
debt; a new offender means new code that should copy the sibling pattern
above instead.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from pathlib import Path

import notebooklm.exceptions as nlm_exceptions

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"

# The broad transport base whose bare catch-and-wrap swallows typed signals.
BROAD_TYPE = "RPCError"

# Minimum narrow set a preceding re-raise clause must cover: exactly the
# RPCError SUBCLASSES the broad clause would otherwise swallow (derived from
# the exception taxonomy and the add_url/add_drive sibling tuple; NetworkError
# is in the sibling tuple but not RPCError-derived, so it is tolerated, not
# required — see the module docstring).
REQUIRED_NARROW_RERAISE = frozenset({"AuthError", "RateLimitError", "ServerError"})

# Every OTHER current RPCError subclass, each with the reason it does NOT
# belong in the required re-raise set. Together with REQUIRED_NARROW_RERAISE
# this PARTITIONS the live RPCError subtree: the taxonomy-pin test asserts
# ``live subclasses == required ∪ excluded``, so a brand-new RPCError subclass
# (e.g. a future QuotaError) FAILS the pin and forces an explicit
# include-or-exclude decision here instead of silently bypassing the gate.
EXCLUDED_FROM_REQUIRED: dict[str, str] = {
    # Semantic absence (ADR-0019 "mutate/read missing target" classes) — a
    # domain wrapper translating these is a deliberate per-callsite contract
    # decision, not the transient-transport swallow this gate targets.
    "ArtifactNotFoundError": "semantic not-found, not transient transport",
    "LabelNotFoundError": "semantic not-found, not transient transport",
    "MindMapNotFoundError": "semantic not-found, not transient transport",
    "NoteNotFoundError": "semantic not-found, not transient transport",
    "NotebookNotFoundError": "semantic not-found, not transient transport",
    "SourceNotFoundError": "semantic not-found, not transient transport",
    # Schema drift — surfacing vs wrapping is the callsite's drift policy
    # (ADR-0011), orthogonal to the retry-ability this gate protects.
    "DecodingError": "schema drift, not transient transport",
    "UnknownRPCMethodError": "schema drift (DecodingError subclass)",
    # Caller-input / feature / setup faults — not retryable transport states.
    "ClientError": "4xx caller fault, not retryable transport",
    "RPCResponseTooLargeError": "payload-size fault, not retryable transport",
    "AuthExtractionError": "login/setup-time extraction fault",
    "ArtifactFeatureUnavailableError": "feature availability, not transport",
}

# Files (relative to src/notebooklm, posix) with a baselined violation that is
# genuinely non-trivial to fix. EMPTY by construction at gate introduction —
# the lone violation (add_text) was fixed in the same change. DO NOT add
# entries to grow the debt: a new offender should copy the sibling pattern
# (narrow re-raise clause first), not get baselined.
ALLOWLIST: frozenset[str] = frozenset()

# ADR-0019 scope carve-out: the RPC protocol layer originates the RPCError
# transport subtree; broad RPCError handling there is the layer's own
# mechanics, not a feature-code contract violation.
EXCLUDED_PACKAGES = frozenset({"rpc"})


def _exception_name(node: ast.expr) -> str | None:
    """The terminal name of an exception reference (``RPCError`` / ``exc.RPCError``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _caught_names(handler: ast.ExceptHandler) -> frozenset[str]:
    """The set of exception-class names an ``except`` clause catches by name."""
    spec = handler.type
    if spec is None:
        return frozenset()
    elts = spec.elts if isinstance(spec, ast.Tuple) else [spec]
    return frozenset(name for name in map(_exception_name, elts) if name is not None)


def _is_bare_reraise(handler: ast.ExceptHandler) -> bool:
    """True when the clause body is exactly a bare ``raise`` (leading docstring ignored).

    Only a LEADING string constant (a docstring/comment-string) is ignored —
    dropping every constant expression would let ``except ...: 0; raise`` pass
    as a compliant bare re-raise (a false negative).
    """
    statements = list(handler.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return (
        len(statements) == 1 and isinstance(statements[0], ast.Raise) and statements[0].exc is None
    )


def _walk_excluding_nested_defs(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Walk ``nodes`` recursively, skipping nested function/class/lambda bodies.

    A ``raise`` inside a function *defined* within an except body executes
    later (if ever), not while the exception is being handled, so it must not
    count as the handler's wrap-and-raise.
    """
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _wrap_and_raise_sites(handler: ast.ExceptHandler) -> list[tuple[int, str]]:
    """``(line, raised-class-name)`` for each ``raise X(...)`` of a non-RPCError class."""
    sites: list[tuple[int, str]] = []
    for node in _walk_excluding_nested_defs(handler.body):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            raised = _exception_name(node.exc.func)
            if raised is not None and raised != BROAD_TYPE:
                sites.append((node.lineno, raised))
    return sites


def _catch_ordering_offenders(tree: ast.AST) -> list[tuple[int, str]]:
    """Return sorted ``(line, wrapped-class)`` violations of the catch-ordering rule.

    A violation is an ``except`` clause that catches :data:`BROAD_TYPE` (alone
    or in a tuple) and wrap-and-raises a different exception class, without
    EARLIER clause(s) in the same ``try`` that bare-re-raise — together —
    at least :data:`REQUIRED_NARROW_RERAISE`. Coverage ACCUMULATES across
    preceding bare-re-raise clauses, so splitting the narrow types over
    multiple clauses (e.g. a dedicated ``except AuthError: raise`` for logging
    symmetry) satisfies the rule as long as the full required set is
    re-raised before the broad clause. Clause order matters: a narrow clause
    *after* the broad one is dead code for RPCError subclasses and does not
    satisfy the rule. Pure on its input so the self-check probes below can
    exercise it without touching the filesystem.
    """
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        reraised_narrows: set[str] = set()
        for handler in node.handlers:
            caught = _caught_names(handler)
            if _is_bare_reraise(handler):
                reraised_narrows |= caught & REQUIRED_NARROW_RERAISE
                continue
            if BROAD_TYPE in caught and not reraised_narrows.issuperset(REQUIRED_NARROW_RERAISE):
                offenders.extend(_wrap_and_raise_sites(handler))
    return sorted(offenders)


def _rel(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def _scanned_files() -> list[Path]:
    return sorted(
        p
        for p in SRC_ROOT.rglob("*.py")
        if p.relative_to(SRC_ROOT).parts[0] not in EXCLUDED_PACKAGES
    )


def _offending_files() -> dict[str, list[tuple[int, str]]]:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites = _catch_ordering_offenders(tree)
        if sites:
            offenders[_rel(path)] = sites
    return offenders


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_unordered_broad_rpc_error_wrap_in_feature_tree() -> None:
    """No feature file may wrap a broad ``RPCError`` without the narrow re-raise first.

    This is the gate: an ``except RPCError`` clause that raises a different
    exception class (``raise SourceAddError(...) from e``) without a preceding
    ``except (AuthError, RateLimitError, ServerError, ...): raise`` clause in
    the same ``try`` swallows the typed transport signals ADR-0019 guarantees
    to callers — the exact bug ``add_text`` shipped with.
    """
    offenders = _offending_files()
    unbaselined = {f: sites for f, sites in offenders.items() if f not in ALLOWLIST}
    assert not unbaselined, (
        "ADR-0019 catch-ordering violation: an `except RPCError` clause that "
        "wraps into a different exception class swallows the typed transport "
        "signals (RateLimitError -> back-off via retry_after, AuthError -> "
        "re-login, ServerError -> transient retry), because those types "
        "SUBCLASS RPCError. Precede the broad clause, in the same `try`, with\n"
        "    except (AuthError, RateLimitError, ServerError, NetworkError):\n"
        "        raise\n"
        "so they propagate unwrapped — copy the add_url/add_drive pattern in "
        "src/notebooklm/_source/add.py (see ADR-0019 'Cross-cutting' row + "
        "retry guidance, docs/adr/0019-error-and-return-contract.md).\n\n"
        + "\n".join(
            f"  src/notebooklm/{f}: "
            + ", ".join(f"line {ln} wraps into {cls}" for ln, cls in sites)
            for f, sites in sorted(unbaselined.items())
        )
    )


def test_no_stale_allowlist_entries() -> None:
    """Every allowlisted file must still offend — fixed files must be removed.

    Keeps the burndown honest (self-draining, like the other guardrail
    allowlists): once a baselined file adopts the sibling pattern it stops
    offending and must drop off :data:`ALLOWLIST`, re-arming the gate for it.
    """
    offenders = _offending_files()
    stale = sorted(f for f in ALLOWLIST if f not in offenders)
    assert not stale, (
        "Stale entries in ALLOWLIST — these files no longer violate the "
        "catch-ordering rule. Remove them so the gate re-protects them:\n"
        + "\n".join(f"  {f}" for f in stale)
    )


def test_allowlist_entries_exist() -> None:
    """Every allowlisted path must point at a real file (catches renames/typos)."""
    missing = sorted(f for f in ALLOWLIST if not (SRC_ROOT / f).is_file())
    assert not missing, "ALLOWLIST references nonexistent files:\n" + "\n".join(
        f"  {f}" for f in missing
    )


def test_required_narrow_set_matches_exception_taxonomy() -> None:
    """The required narrow set is exactly the swallowable transport subtree.

    Pins the derivation against the live exception tree: every required name
    must subclass ``RPCError`` (otherwise the broad clause could never swallow
    it and requiring it would be noise), and ``NetworkError`` — present in the
    sibling re-raise tuple for ``idempotent_create``'s probe contract — must
    NOT subclass ``RPCError``, which is exactly why the gate tolerates it
    without requiring it. If the taxonomy ever moves (e.g. ``NetworkError``
    re-parented under ``RPCError``), this fails and the required set must be
    re-derived.
    """
    broad = getattr(nlm_exceptions, BROAD_TYPE)
    for name in sorted(REQUIRED_NARROW_RERAISE):
        narrow = getattr(nlm_exceptions, name)
        assert issubclass(narrow, broad), (
            f"{name} no longer subclasses {BROAD_TYPE}; it cannot be swallowed "
            "by a broad catch — re-derive REQUIRED_NARROW_RERAISE."
        )
    assert not issubclass(nlm_exceptions.NetworkError, broad), (
        "NetworkError now subclasses RPCError — it CAN be swallowed by a broad "
        "catch, so add it to REQUIRED_NARROW_RERAISE."
    )

    # PARTITION pin: the live RPCError subtree must equal required ∪ excluded,
    # so a brand-new RPCError subclass forces an explicit decision here
    # instead of silently bypassing the required set.
    def _subtree(cls: type) -> set[str]:
        names: set[str] = set()
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("notebooklm"):
                names.add(sub.__name__)
                names.update(_subtree(sub))
        return names

    live = _subtree(broad)
    partitioned = REQUIRED_NARROW_RERAISE | set(EXCLUDED_FROM_REQUIRED)
    unaccounted = sorted(live - partitioned)
    assert not unaccounted, (
        f"New RPCError subclass(es) {unaccounted} are not partitioned: decide "
        "whether each is a transient transport fault callers must catch (add "
        "to REQUIRED_NARROW_RERAISE — the gate will then demand it in every "
        "narrow re-raise clause) or not (add to EXCLUDED_FROM_REQUIRED with a "
        "one-line reason)."
    )
    retired = sorted(partitioned - live)
    assert not retired, (
        f"Partition entries {retired} no longer exist in the RPCError subtree "
        "— remove them from REQUIRED_NARROW_RERAISE / EXCLUDED_FROM_REQUIRED."
    )


# ---------------------------------------------------------------------------
# Detector self-checks (flag / ignore, both directions)
# ---------------------------------------------------------------------------


def test_detector_flags_unpreceded_broad_wrap() -> None:
    """The exact ``add_text`` bug shape is flagged: bare broad catch, wrap-and-raise."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    result = rpc_call(method, params)",
                "except RPCError as e:",
                "    raise SourceAddError(title, cause=e) from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == [(4, "SourceAddError")]


def test_detector_flags_tuple_catch_containing_broad_rpc_error() -> None:
    """``RPCError`` hidden in a catch tuple is still the broad swallow."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except (ValueError, RPCError) as e:",
                "    raise DomainError('failed') from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == [(4, "DomainError")]


def test_detector_flags_incomplete_narrow_set() -> None:
    """A preceding re-raise of only SOME narrow types does not satisfy the rule."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except (RateLimitError, ServerError):",  # AuthError missing
                "    raise",
                "except RPCError as e:",
                "    raise SourceAddError(title) from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == [(6, "SourceAddError")]


def test_detector_flags_narrow_clause_that_wraps_instead_of_reraising() -> None:
    """A narrow clause that wraps (not bare ``raise``) is itself the swallow."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except (AuthError, RateLimitError, ServerError) as e:",
                "    raise SourceAddError(title) from e",  # wraps, not re-raise
                "except RPCError as e:",
                "    raise SourceAddError(title) from e",
            ]
        )
    )
    # Both clauses offend: the broad clause is unpreceded by a true re-raise,
    # and the narrow clause itself wrap-and-raises while catching RPCError
    # subclasses — but the detector targets clauses naming RPCError, so the
    # broad clause is the flagged site.
    assert _catch_ordering_offenders(tree) == [(6, "SourceAddError")]


def test_detector_flags_narrow_clause_after_broad_wrap() -> None:
    """Clause order matters: a narrow re-raise AFTER the broad wrap is dead code."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except RPCError as e:",
                "    raise SourceAddError(title) from e",
                "except (AuthError, RateLimitError, ServerError, NetworkError):",
                "    raise",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == [(4, "SourceAddError")]


def test_detector_accepts_sibling_catch_ordering() -> None:
    """The sanctioned add_url/add_drive/upload pattern passes, extras tolerated.

    The sibling tuple includes ``NetworkError`` beyond the required three —
    extra narrow names must not break the match.
    """
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    result = rpc_call(method, params)",
                "except (AuthError, RateLimitError, ServerError, NetworkError):",
                "    raise",
                "except RPCError as e:",
                "    raise SourceAddError(title, cause=e) from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_detector_accepts_split_narrow_clauses() -> None:
    """Coverage accumulates: narrow types split over several bare-re-raise
    clauses (e.g. a dedicated ``except AuthError`` for logging symmetry)
    satisfy the rule as long as the union covers the required set before the
    broad clause."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except AuthError:",
                "    raise",
                "except (RateLimitError, ServerError):",
                "    raise",
                "except RPCError as e:",
                "    raise SourceAddError(title) from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_detector_ignores_bare_reraise_and_swallow_bodies() -> None:
    """Broad clauses that re-raise the original or swallow-and-continue are out of scope.

    These are the live non-violation shapes the gate must tolerate: the
    ``_rpc_executor`` refresh-and-retry catch (work, then bare ``raise``), the
    ``_notebooks.create`` quota probe (helper call + bare ``raise``), and the
    ``_artifact/listing`` / ``_research`` partial-availability swallows (log
    and continue, no raise) — ADR-0019 Rule-3/Scope territory, not this gate's.
    """
    tree = ast.parse(
        "\n".join(
            [
                "try:",  # executor shape: instrumentation then bare re-raise
                "    decode()",
                "except RPCError as exc:",
                "    log(exc)",
                "    raise",
                "try:",  # notebooks shape: may-raise helper + bare re-raise
                "    create()",
                "except RPCError as exc:",
                "    raise_quota_error_if_detected(exc)",
                "    raise",
                "try:",  # listing/research shape: swallow and continue
                "    rows = fetch()",
                "except (RPCError, HTTPError) as e:",
                "    logger.warning('failed: %s', e)",
                "    rows = None",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_detector_ignores_rewrap_into_rpc_error_itself() -> None:
    """Re-raising AS ``RPCError`` is not a different class — callers still catch it."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    decode()",
                "except RPCError as e:",
                "    raise RPCError(f'decode failed: {e}') from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_detector_ignores_narrow_only_catch() -> None:
    """A clause catching only narrow types has no broad swallow to order around."""
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except RateLimitError as e:",
                "    raise BackoffExhaustedError() from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_detector_ignores_raise_inside_nested_function_def() -> None:
    """A wrap-raise inside a function *defined* in the handler body executes later.

    It is not the handler's own wrap-and-raise, so it must not be flagged;
    a nested-def callback that wraps is a different (deferred) control flow.
    """
    tree = ast.parse(
        "\n".join(
            [
                "try:",
                "    work()",
                "except RPCError as e:",
                "    def _later():",
                "        raise SourceAddError('deferred')",
                "    schedule(_later)",
                "    raise",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == []


def test_gate_catches_a_planted_offender_in_a_fresh_module() -> None:
    """Simulates the gate's real job: a NEW module re-introducing the bug is rejected."""
    tree = ast.parse(
        "\n".join(
            [
                "async def add_thing(notebook_id, payload):",
                "    try:",
                "        return await rpc.rpc_call(RPCMethod.ADD_SOURCE, payload)",
                "    except RPCError as e:",
                "        raise SourceAddError('thing', cause=e) from e",
            ]
        )
    )
    assert _catch_ordering_offenders(tree) == [(5, "SourceAddError")]
