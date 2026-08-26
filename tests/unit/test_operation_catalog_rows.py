"""P9.2: binding rows are execution-authority sites in the operation catalog.

A module-level ``CodecBinding``/``CustomBinding`` row declares its natives through
``NativeCallSpec``; the AST walker reads that literal declaration exactly as it reads a
``_rpc_call(RPCMethod.X, …)`` site, so a codec row never has zero authorities and the
policy ledger can be audited against what a row can actually dispatch.
"""

from __future__ import annotations

import ast
from pathlib import Path

from notebooklm._semantic.operations import Operation
from notebooklm.rpc import RPCMethod
from scripts import _operation_catalog_ast as catalog_ast
from scripts import audit_operation_catalog as catalog

_ROW_MODULE = """
from notebooklm._semantic.binding import BindingTable, CodecBinding, CustomBinding, NativeCallSpec, RpcNative
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    LABEL_UPDATE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    RESEARCH_START_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
)
from notebooklm.rpc import RPCMethod

SETTINGS_GET = CodecBinding(
    definition=SETTINGS_GET_DEF,
    encode=encode_settings,
    decode=decode_settings,
    native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS),
)
_RESEARCH_SPEC = NativeCallSpec.keyed(
    _pick_research,
    RpcNative(RPCMethod.START_FAST_RESEARCH),
    RpcNative(RPCMethod.START_DEEP_RESEARCH, "deep"),
)
RESEARCH_START = bind_codec(RESEARCH_START_DEF, encode_r, decode_r, native=_RESEARCH_SPEC)
LABEL_UPDATE = CustomBinding(
    definition=LABEL_UPDATE_DEF,
    handler=_label_update,
    native=(
        NativeCallSpec.constant(RPCMethod.LIST_LABELS, key="list"),
        NativeCallSpec.keyed(
            _variant,
            RpcNative(RPCMethod.UPDATE_LABEL, variant="add_sources"),
            RpcNative(RPCMethod.UPDATE_LABEL, "remove_sources"),
            RpcNative(method=RPCMethod.UPDATE_LABEL, variant=None),
            key="mutate",
        ),
    ),
    justification="mutate-then-readback stays adapter-owned until hoisted",
    category="protocol",
)
TABLE = BindingTable(
    {
        Operation.SETTINGS_GET_LIMITS: CodecBinding(
            definition=SETTINGS_GET_LIMITS_DEF,
            encode=encode_limits,
            decode=decode_limits,
            native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS, "limits"),
        ),
    }
)
"""

_UNRESOLVED_MODULE = """
DYNAMIC_METHOD = CodecBinding(
    definition=NOTE_LIST_DEF, encode=e, decode=d, native=NativeCallSpec.constant(pick())
)
DYNAMIC_VARIANT = CodecBinding(
    definition=NOTE_GET_DEF,
    encode=e,
    decode=d,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS, chosen),
)
NO_DEFINITION = CodecBinding(
    encode=e, decode=d, native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS)
)
UNKNOWN_DEFINITION = CodecBinding(
    definition=NOT_A_REAL_DEF, encode=e, decode=d, native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS)
)
_DYNAMIC_SPEC = NativeCallSpec.keyed(_pick)
BY_NAME = CodecBinding(definition=SETTINGS_GET_DEF, encode=e, decode=d, native=_DYNAMIC_SPEC)
MISMATCHED_KEY = {Operation.NOTE_LIST: CodecBinding(definition=NOTE_GET_DEF, encode=e, decode=d, native=NativeCallSpec.constant(RPCMethod.GET_NOTES_AND_MIND_MAPS))}
"""


def _collect(source: str) -> catalog_ast._ReferenceCollector:
    collector = catalog_ast._ReferenceCollector("synthetic.py", set())
    collector.visit(ast.parse(source))
    return collector


def test_walker_derives_natives_from_constant_keyed_custom_and_table_rows() -> None:
    collector = _collect(_ROW_MODULE)

    rows = {row.site: row for row in collector.binding_rows}
    assert set(rows) == {
        "SETTINGS_GET",
        "RESEARCH_START",
        "LABEL_UPDATE",
        "TABLE.SETTINGS_GET_LIMITS",
    }
    assert rows["SETTINGS_GET"].operation is Operation.SETTINGS_GET
    assert rows["SETTINGS_GET"].natives == (("GET_USER_SETTINGS", None),)
    # A keyed spec bound to a module-level name resolves through that name; each declared
    # choice is one native, and ``RpcNative(method)`` means variant ``None``.
    assert rows["RESEARCH_START"].operation is Operation.RESEARCH_START
    assert rows["RESEARCH_START"].natives == (
        ("START_FAST_RESEARCH", None),
        ("START_DEEP_RESEARCH", "deep"),
    )
    # Custom rows declare a tuple of specs; keyword and positional variants both resolve.
    assert rows["LABEL_UPDATE"].natives == (
        ("LIST_LABELS", None),
        ("UPDATE_LABEL", "add_sources"),
        ("UPDATE_LABEL", "remove_sources"),
        ("UPDATE_LABEL", None),
    )
    # Rows nested in a table literal take the table name plus their operation member.
    assert rows["TABLE.SETTINGS_GET_LIMITS"].operation is Operation.SETTINGS_GET_LIMITS
    assert rows["TABLE.SETTINGS_GET_LIMITS"].natives == (("GET_USER_SETTINGS", "limits"),)
    assert all(not row.unresolved for row in rows.values())
    assert collector.unresolved_rpc_calls == []
    # Rows are not ``rpc_call`` sites; the two inventories stay distinct.
    assert collector.rpc_calls == []


def test_walker_reports_unresolvable_rows_like_dynamic_dispatches() -> None:
    collector = _collect(_UNRESOLVED_MODULE)

    assert collector.unresolved_rpc_calls == [
        ("DYNAMIC_METHOD", "method"),
        ("DYNAMIC_VARIANT", "operation_variant"),
        ("NO_DEFINITION", "definition"),
        ("UNKNOWN_DEFINITION", "definition"),
        ("BY_NAME", "method"),
        ("MISMATCHED_KEY.NOTE_LIST", "definition"),
    ]
    rows = {row.site: row for row in collector.binding_rows}
    assert all(rows[site].unresolved for site in rows)
    # Partial resolution is never silently accepted as an authority.
    assert rows["DYNAMIC_METHOD"].natives == ()
    assert rows["NO_DEFINITION"].operation is None
    # A table key that disagrees with the row's definition is flagged, and the key wins the
    # site name so the report points at the offending entry.
    assert rows["MISMATCHED_KEY.NOTE_LIST"].operation is Operation.NOTE_GET


def test_rows_inside_functions_or_classes_are_not_sites() -> None:
    collector = _collect(
        """
def build():
    return CodecBinding(definition=SETTINGS_GET_DEF, native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS))

class Holder:
    ROW = CodecBinding(definition=SETTINGS_GET_DEF, native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS))
"""
    )
    assert collector.binding_rows == []
    assert collector.unresolved_rpc_calls == []


def _write_row_package(root: Path) -> None:
    package = root / "_web" / "bindings"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "settings.py").write_text(_ROW_MODULE, encoding="utf-8")


def test_row_sites_join_native_execution_sites_and_binding_sites(tmp_path: Path) -> None:
    _write_row_package(tmp_path)

    sites = catalog_ast.collect_native_execution_sites(tmp_path)
    assert sites[(RPCMethod.GET_USER_SETTINGS, None)] == ["_web/bindings/settings.py:SETTINGS_GET"]
    assert sites[(RPCMethod.GET_USER_SETTINGS, "limits")] == [
        "_web/bindings/settings.py:TABLE.SETTINGS_GET_LIMITS"
    ]
    assert sites[(RPCMethod.START_DEEP_RESEARCH, "deep")] == [
        "_web/bindings/settings.py:RESEARCH_START"
    ]
    assert sites[(RPCMethod.UPDATE_LABEL, "add_sources")] == [
        "_web/bindings/settings.py:LABEL_UPDATE"
    ]
    assert catalog_ast.collect_binding_sites(tmp_path) == {
        "_web/bindings/settings.py:SETTINGS_GET",
        "_web/bindings/settings.py:RESEARCH_START",
        "_web/bindings/settings.py:LABEL_UPDATE",
        "_web/bindings/settings.py:TABLE.SETTINGS_GET_LIMITS",
    }


def test_derive_row_authorities_allocates_each_native_to_its_row(tmp_path: Path) -> None:
    _write_row_package(tmp_path)

    derived = catalog_ast.derive_row_authorities(tmp_path)

    assert derived[(Operation.SETTINGS_GET, (RPCMethod.GET_USER_SETTINGS, None))] == (
        "_web/bindings/settings.py:SETTINGS_GET",
    )
    assert derived[(Operation.RESEARCH_START, (RPCMethod.START_FAST_RESEARCH, None))] == (
        "_web/bindings/settings.py:RESEARCH_START",
    )
    assert derived[(Operation.LABEL_UPDATE, (RPCMethod.UPDATE_LABEL, "remove_sources"))] == (
        "_web/bindings/settings.py:LABEL_UPDATE",
    )
    # SETTINGS_GET (1) + TABLE.SETTINGS_GET_LIMITS (1) + RESEARCH_START (2) + LABEL_UPDATE (4).
    assert len(derived) == 8
    # Deterministic ordering: by operation value, then native text.
    assert list(derived)[0][0] is Operation.LABEL_UPDATE


def test_row_audit_compares_declared_natives_with_the_policy_ledger() -> None:
    row = catalog_ast.BindingRowSite

    assert (
        catalog_ast.audit_row_bindings(
            [row("x.py:SETTINGS_GET", Operation.SETTINGS_GET, (("GET_USER_SETTINGS", None),))]
        )
        == []
    )
    # RESEARCH_START's ledger row is (FAST, None) + (DEEP, None): a row declaring a "deep"
    # variant the ledger does not know is drift, not a new authority.
    errors = catalog_ast.audit_row_bindings(
        [
            row(
                "x.py:RESEARCH_START",
                Operation.RESEARCH_START,
                (("START_FAST_RESEARCH", None), ("START_DEEP_RESEARCH", "deep")),
            )
        ]
    )
    assert errors == [
        "research.start binding row x.py:RESEARCH_START declares natives "
        "['START_DEEP_RESEARCH:deep', 'START_FAST_RESEARCH:<default>'] but the policy ledger "
        "expects ['START_DEEP_RESEARCH:<default>', 'START_FAST_RESEARCH:<default>']"
    ]
    assert catalog_ast.audit_row_bindings(
        [row("x.py:NO_DEF", None, (("GET_USER_SETTINGS", None),))]
    ) == ["binding row x.py:NO_DEF names no resolvable operation definition"]
    assert catalog_ast.audit_row_bindings(
        [
            row("x.py:A", Operation.SETTINGS_GET, (("GET_USER_SETTINGS", None),)),
            row("y.py:B", Operation.SETTINGS_GET, (("GET_USER_SETTINGS", None),)),
        ]
    ) == ["settings.get has more than one binding row: x.py:A, y.py:B"]
    # An operation outside the active web ledger cannot have a web row.
    from scripts._web_policy_intent import WEB_CALL_POLICY_BINDINGS

    inactive = next(op for op in Operation if op not in WEB_CALL_POLICY_BINDINGS)
    assert catalog_ast.audit_row_bindings(
        [row("x.py:INACTIVE", inactive, (("GET_USER_SETTINGS", None),))]
    ) == [f"binding row x.py:INACTIVE for {inactive.value} has no web call-policy ledger entry"]
    # Unresolved rows are reported by the dispatch audit, not double-counted here.
    assert (
        catalog_ast.audit_row_bindings(
            [row("x.py:PARTIAL", Operation.SETTINGS_GET, (), unresolved=True)]
        )
        == []
    )


def test_production_rows_are_derived_and_the_audit_is_wired() -> None:
    """Every production row is a resolved authority site that agrees with the ledger."""
    from notebooklm._web.bindings import WEB_BINDING_ROWS

    rows = catalog_ast.collect_binding_rows()
    assert {row.operation for row in rows} == set(WEB_BINDING_ROWS)
    assert all(not row.unresolved for row in rows)
    assert all(row.site.startswith("_web/bindings/") for row in rows)
    derived = catalog_ast.derive_row_authorities()
    # Every row but the streamed leaf allocates at least one native; a
    # ``StreamNative`` is not a wire method, so ``chat.stream_answer`` allocates
    # none and is resolved rather than unresolved.
    assert {operation for operation, _native in derived} == set(WEB_BINDING_ROWS) - {
        Operation.CHAT_STREAM_ANSWER
    }
    assert catalog_ast.audit_row_bindings() == []
    assert catalog.audit_row_bindings is catalog_ast.audit_row_bindings
    assert catalog.collect_binding_rows is catalog_ast.collect_binding_rows
    assert catalog.derive_row_authorities is catalog_ast.derive_row_authorities
