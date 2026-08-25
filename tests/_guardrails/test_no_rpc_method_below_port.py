"""Shrink-only ratchet: who may *name* ``RPCMethod`` below the port.

P9 of the semantic-backend plan
(``docs/plan/2026-08-13-semantic-backend-refactor.md``, "P9 -- Decompose the
web backend into transport, codec, and binding table") ends with this
acceptance criterion:

    Below the port, ``RPCMethod`` is named only by ``WebRequest``,
    ``WebTransport``, ``NativeCallSpec`` values and the policy ledger; no codec
    function, handler or row body names it.

Today that is not yet true. The P9.3 codec rows already thread the method id
*from the spec* -- ``_web/bindings/research.py`` decodes with
``method_id=_select_start(value).method.value`` -- but most ``_web/codec/*.py``
decoders still spell ``RPCMethod.X.value`` for their ``method_id`` diagnostics,
and the handler modules that P9.4 deletes still pass ``RPCMethod.X`` to
``_rpc_call``. Each such site is a second, hand-maintained copy of the native
binding the row already declares, and it is the copy a second backend could
not share.

This gate makes the population **shrink-only**:

* **Sanctioned modules** (:data:`SANCTIONED_MODULES`) are never scanned: the
  transport pair (``transport.py``, ``chat_transport.py``), runtime/deadline
  ledgers, registry, and policy ledger. They are *where* ``RPCMethod`` belongs
  below the port.

* **Binding rows** (``_web/bindings/*.py``) may name ``RPCMethod`` only as the
  *value of a native spec*: an argument of ``NativeCallSpec.constant(...)``,
  ``NativeCallSpec.keyed(...)`` or ``NativeChoice(...)`` -- wherever that call
  sits, including a module-level ``_X = NativeChoice(RPCMethod.Y)`` -- or as
  the type parameter ``NativeChoice[RPCMethod]`` of such a value. Any other
  mention in a row module (a ``.value`` spelled into a decoder call, a bare
  comparison) is a site.

* **Everything else** under ``src/notebooklm/_web/`` is a site, pinned in
  :data:`ALLOWLIST` as ``"<path relative to src/notebooklm>:<qualname>"``
  where ``qualname`` is the dotted chain of enclosing ``def``/``class`` names
  (``<module>`` at module level; a function's signature -- defaults and
  annotations -- counts as that function's scope). The gate asserts the
  measured population is **exactly** the literal: a new site fails, and a
  drained site fails until its entry is deleted, so the list can only shrink
  and the reclaimed ground can never be re-accreted.

A site is an :class:`ast.Name` load of ``RPCMethod``; import statements are
not sites (ruff F401 removes the import when the last site goes), and prose
mentions in docstrings and comments are not sites. ``UnknownRPCMethodError``
is a different name and is never matched.

The :data:`ALLOWLIST` is the **P9.4 codec method-id threading** burndown: each
entry drains when its codec function takes ``method_id`` from the row (as the
research rows already do), or when P9.4 deletes the handler module that owns
it. Do not add entries -- a new site means a codec or row that should take the
method id from its ``NativeCallSpec`` value instead. When the literal is
empty, the P9 acceptance criterion above holds and this file becomes a flat
ban.

Modelled after the AST allowlist gates in ``tests/_guardrails/`` (notably
``test_no_raw_positional_rpc_indexing.py`` for the scope idiom and
``test_module_size_ratchet.py`` / ``test_string_patch_ratchet.py`` for the
shrink-only mechanics).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"

WEB_ROOT = "_web"
BINDINGS_PREFIX = "_web/bindings/"
TARGET = "RPCMethod"

# Where ``RPCMethod`` belongs below the port: the transport pair, the runtime
# and its deadline seams, the registry and the policy ledger. Never scanned.
SANCTIONED_MODULES: frozenset[str] = frozenset(
    {
        "_web/chat_transport.py",
        "_web/deadlines.py",
        "_web/policy.py",
        "_web/registry.py",
        "_web/runtime.py",
        "_web/transport.py",
    }
)

# The native-spec forms a binding row may name ``RPCMethod`` inside: callee
# spellings whose arguments are spec values, and the subscript whose slice is
# the spec's type parameter.
SPEC_CALLEES: frozenset[str] = frozenset(
    {"NativeCallSpec.constant", "NativeCallSpec.keyed", "NativeChoice"}
)
SPEC_SUBSCRIPTS: frozenset[str] = frozenset({"NativeChoice"})

# P9.4 codec method-id threading burndown. Measured, not estimated; exact and
# sorted; may only shrink. Regenerate with::
#
#     uv run python -c "from tests._guardrails.test_no_rpc_method_below_port \
#         import measured_sites; print(*measured_sites(), sep='\n')"
ALLOWLIST: tuple[str, ...] = (
    "_web/backend.py:WebRpcBackend._rpc_call",
    "_web/backend.py:WebRpcBackend.public_rpc_call",
    "_web/bindings/_invoker_caller.py:InvokerRpcCaller.__init__",
    "_web/bindings/_invoker_caller.py:InvokerRpcCaller.rpc_call",
    "_web/bindings/mind_maps.py:<module>",
    "_web/bindings/mind_maps.py:_mind_map_generate_interactive",
    "_web/bindings/notebooks.py:_map_allocate_quota_rejection",
    "_web/codec/artifact_formatters.py:_extract_data_table_rows",
    "_web/codec/artifact_formatters.py:_parse_data_table",
    "_web/codec/artifacts.py:decode_interactive_content",
    "_web/codec/artifacts.py:decode_studio_rows",
    "_web/codec/chat.py:decode_get_settings_result",
    "_web/codec/chat_stream.py:_extract_next_turn_content",
    "_web/codec/generation.py:decode_generation_kickoff",
    "_web/codec/labels.py:decode_label_generate_result",
    "_web/codec/labels.py:decode_label_set_list_result",
    "_web/codec/mind_maps.py:decode_artifact_mind_map_leaf",
    "_web/codec/mind_maps.py:decode_created_interactive_id",
    "_web/codec/mind_maps.py:decode_generated_tree",
    "_web/codec/mind_maps.py:extract_interactive_tree_leaf",
    "_web/codec/notebooks.py:<module>",
    "_web/codec/notebooks.py:_decode_summary",
    "_web/codec/notebooks.py:_decode_topics",
    "_web/codec/notebooks.py:decode_notebook_description",
    "_web/codec/notebooks.py:decode_notebook_get",
    "_web/codec/notebooks.py:decode_notebook_list_result",
    "_web/codec/notebooks.py:decode_notebook_source_ids_silent",
    "_web/codec/notes.py:_decode_note_rows",
    "_web/codec/notes.py:_is_note_row_like",
    "_web/codec/notes.py:_normalize_note_row",
    "_web/codec/notes.py:decode_created_note",
    "_web/codec/settings.py:decode_get_user_settings",
    "_web/codec/settings.py:decode_set_output_language",
    "_web/codec/sharing.py:<module>",
    "_web/codec/source_ids.py:_decode",
    "_web/codec/sources.py:decode_add_source_records",
    "_web/codec/sources.py:decode_renamed_source",
    "_web/codec/sources.py:decode_source_record",
    "_web/codec/sources.py:decode_source_snapshot",
    "_web/codec/sources.py:rename_target_missing",
    "_web/codec/studio_documents.py:decode_artifact_retry",
    "_web/codec/studio_documents.py:decode_artifact_revise_slide",
    "_web/codec/studio_documents.py:decode_generation_status",
    "_web/codec/suggestions.py:decode_prompt_suggestions",
    "_web/codec/suggestions.py:decode_report_suggestions",
)


def _callee_name(node: ast.expr) -> str | None:
    """``Name`` / one-level ``Attribute`` spelling of a callee or subscript value."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _spec_form_targets(tree: ast.AST) -> set[int]:
    """Identities of ``RPCMethod`` names that sit inside a sanctioned spec form."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        roots: list[ast.expr]
        if isinstance(node, ast.Call) and _callee_name(node.func) in SPEC_CALLEES:
            roots = [*node.args, *(keyword.value for keyword in node.keywords)]
        elif isinstance(node, ast.Subscript) and _callee_name(node.value) in SPEC_SUBSCRIPTS:
            roots = [node.slice]
        else:
            continue
        for root in roots:
            allowed.update(
                id(inner)
                for inner in ast.walk(root)
                if isinstance(inner, ast.Name) and inner.id == TARGET
            )
    return allowed


def rpc_method_sites(tree: ast.AST, *, rel: str, allow_spec_forms: bool) -> set[str]:
    """``"<rel>:<qualname>"`` for every scope in *tree* that names ``RPCMethod``.

    ``qualname`` is the dotted chain of enclosing ``def``/``class`` names, or
    ``<module>``. A function's decorators, defaults and annotations are visited
    under that function's scope.
    """
    exempt = _spec_form_targets(tree) if allow_spec_forms else set()
    sites: set[str] = set()

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if isinstance(node, ast.Name) and node.id == TARGET and id(node) not in exempt:
            sites.add(f"{rel}:{'.'.join(scope) or '<module>'}")
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return sites


def scan(src_root: Path) -> set[str]:
    """All ``RPCMethod`` sites under ``<src_root>/_web/`` outside the sanctioned set."""
    sites: set[str] = set()
    for path in sorted((src_root / WEB_ROOT).rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel in SANCTIONED_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites |= rpc_method_sites(tree, rel=rel, allow_spec_forms=rel.startswith(BINDINGS_PREFIX))
    return sites


def measured_sites() -> tuple[str, ...]:
    """The current population, in :data:`ALLOWLIST` order (for regeneration)."""
    return tuple(sorted(scan(SRC_ROOT)))


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_rpc_method_sites_below_the_port_are_exactly_the_allowlist() -> None:
    """The measured population equals the pinned literal: no growth, no stale entries."""
    actual = measured_sites()
    expected = set(ALLOWLIST)
    new = sorted(set(actual) - expected)
    drained = sorted(expected - set(actual))
    assert actual == ALLOWLIST, (
        "RPCMethod population below the port drifted from ALLOWLIST in "
        f"{Path(__file__).name}.\n"
        + (
            "NEW sites (a codec function, handler or row body now names RPCMethod; "
            "take the method id from the row's NativeCallSpec value instead):\n  "
            + "\n  ".join(new)
            + "\n"
            if new
            else ""
        )
        + (
            "DRAINED sites (delete these entries from ALLOWLIST so the ground "
            "cannot be re-accreted):\n  " + "\n  ".join(drained) + "\n"
            if drained
            else ""
        )
    )


def test_allowlist_is_sorted_and_unique() -> None:
    assert tuple(sorted(set(ALLOWLIST))) == ALLOWLIST


def test_allowlist_entries_are_well_formed_and_exist() -> None:
    """Every entry is ``<_web path>:<qualname>`` for a file that still exists."""
    for entry in ALLOWLIST:
        rel, sep, qualname = entry.partition(":")
        assert sep and qualname, f"malformed ALLOWLIST entry {entry!r}"
        assert rel.startswith(f"{WEB_ROOT}/"), f"{entry!r} is not below the port"
        assert rel not in SANCTIONED_MODULES, f"{entry!r} names a sanctioned module"
        assert (SRC_ROOT / rel).is_file(), f"{entry!r}: {rel} no longer exists"


def test_sanctioned_modules_exist() -> None:
    """A rename or delete of a sanctioned module must update the set."""
    missing = sorted(rel for rel in SANCTIONED_MODULES if not (SRC_ROOT / rel).is_file())
    assert not missing, f"SANCTIONED_MODULES names missing modules: {missing}"


# --------------------------------------------------------------------------
# Detector self-tests
# --------------------------------------------------------------------------

_SPEC_ROW_MODULE = '''\
"""A binding row module that names RPCMethod only through spec values."""

from ..._binding import CodecBinding, NativeCallSpec, NativeChoice
from ...rpc import RPCMethod

_FAST = NativeChoice(RPCMethod.START_FAST_RESEARCH)
_DEEP = NativeChoice(RPCMethod.START_DEEP_RESEARCH)


def _select(value: object) -> NativeChoice[RPCMethod]:
    return _FAST if value else _DEEP


def _decode(value: object, result: object) -> object:
    # Threads the id from the spec: no RPCMethod name here.
    return (result, _select(value).method.value)


START = CodecBinding(native=NativeCallSpec.keyed(_select, _FAST, _DEEP), decode=_decode)
POLL = CodecBinding(native=NativeCallSpec.constant(RPCMethod.POLL_RESEARCH))
NOTE = CodecBinding(native=NativeCallSpec.constant(RPCMethod.CREATE_NOTE, "plain"))
'''

_LEAKY_ROW_MODULE = """\
from ..._binding import CodecBinding, NativeCallSpec
from ...rpc import RPCMethod


def _decode(result: object) -> object:
    return (result, RPCMethod.POLL_RESEARCH.value)


POLL = CodecBinding(native=NativeCallSpec.constant(RPCMethod.POLL_RESEARCH), decode=_decode)
"""

_DECODER_MODULE = '''\
"""A codec module that spells the method id itself."""

from ...rpc import RPCMethod, safe_index

_METHOD_ID = RPCMethod.LIST_NOTEBOOKS.value


def decode_rows(payload: list[object]) -> object:
    # ``UnknownRPCMethodError`` is a different name and never matches.
    return safe_index(payload, 0, method_id=RPCMethod.GET_NOTEBOOK.value, source="x")


class _Helper:
    def method_id(self, method: RPCMethod) -> str:
        return method.value


def clean(payload: list[object], *, method_id: str) -> object:
    """Names RPCMethod only in prose: RPCMethod.CREATE_NOTE."""
    return safe_index(payload, 0, method_id=method_id, source="y")
'''

_SANCTIONED_MODULE = """\
from ..rpc import RPCMethod

def call(method: RPCMethod) -> str:
    return method.value
"""


def _plant(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_detector_allows_spec_forms_in_rows_and_flags_decoders(tmp_path: Path) -> None:
    _plant(tmp_path, "_web/bindings/research.py", _SPEC_ROW_MODULE)
    _plant(tmp_path, "_web/bindings/leaky.py", _LEAKY_ROW_MODULE)
    _plant(tmp_path, "_web/codec/notebooks.py", _DECODER_MODULE)
    _plant(tmp_path, "_web/transport.py", _SANCTIONED_MODULE)

    assert scan(tmp_path) == {
        "_web/bindings/leaky.py:_decode",
        "_web/codec/notebooks.py:<module>",
        "_web/codec/notebooks.py:decode_rows",
        "_web/codec/notebooks.py:_Helper.method_id",
    }


def test_detector_does_not_exempt_spec_forms_outside_row_modules(tmp_path: Path) -> None:
    """The spec-form exemption is scoped to ``_web/bindings/``; a codec wrapping one is a site."""
    _plant(
        tmp_path,
        "_web/codec/wrapped.py",
        "from ..._binding import NativeCallSpec\nfrom ...rpc import RPCMethod\n"
        "SPEC = NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK)\n",
    )
    assert scan(tmp_path) == {"_web/codec/wrapped.py:<module>"}


def test_gate_catches_a_planted_offender_in_a_fresh_module(tmp_path: Path) -> None:
    """End-to-end: a fresh module naming ``RPCMethod.X.value`` fails the allowlist comparison."""
    _plant(tmp_path, "_web/bindings/settings.py", _SPEC_ROW_MODULE)
    assert tuple(sorted(scan(tmp_path))) == ()

    _plant(
        tmp_path,
        "_web/codec/fresh.py",
        "from ...rpc import RPCMethod\n\ndef decode(x):\n    return RPCMethod.SUMMARIZE.value\n",
    )
    assert tuple(sorted(scan(tmp_path))) == ("_web/codec/fresh.py:decode",) != ALLOWLIST
