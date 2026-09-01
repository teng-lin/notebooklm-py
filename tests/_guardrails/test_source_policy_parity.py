"""Guardrail: the MCP and REST source adapters share ONE source-policy definition.

The batch/wait caps and the fatal-vs-isolate classifier live in the transport-neutral
``_app`` core (``_app.source_batch`` / ``_app.source_wait``). This gate forbids either
adapter from re-declaring the cap constants locally (which is how they drifted before —
the MCP copy swallowed fatal errors and skipped the caps) and pins that both consult
the same shared ``batch_item_is_fatal``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "notebooklm"

_POLICY_NAMES = frozenset(
    {
        "MAX_BATCH_URLS",
        "MAX_WAIT_TIMEOUT",
        "MAX_WAIT_SOURCE_IDS",
    }
)

# The adapter modules that must IMPORT the policy, never re-declare it.
_ADAPTERS = [
    _SRC / "server" / "routes" / "sources.py",
    _SRC / "mcp" / "tools" / "sources.py",
    _SRC / "mcp" / "tools" / "_waitagg.py",
]

# The _app modules that are the sole definers.
_DEFINERS = [
    _SRC / "_app" / "source_batch.py",
    _SRC / "_app" / "source_wait.py",
]


def _module_level_assigned_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module level only — imports/aliases are ImportFrom, not Assign
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_names(path: Path) -> set[str]:
    """Names an ``ImportFrom`` binds into the module namespace (respecting ``as``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _local_def_names(path: Path) -> set[str]:
    """Function/assignment names DEFINED locally in the module (a fork, not an import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names | _module_level_assigned_names(path)


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=lambda p: str(p.relative_to(_SRC)))
def test_adapters_do_not_redeclare_the_cap_policy(adapter: Path) -> None:
    """An adapter re-`MAX_* = ...` assignment (drift risk) fails here; imports are fine."""
    redeclared = _module_level_assigned_names(adapter) & _POLICY_NAMES
    assert not redeclared, (
        f"{adapter.relative_to(_SRC)} re-declares source-policy caps {sorted(redeclared)}; "
        "import them from _app.source_batch / _app.source_wait instead so MCP and REST "
        "can't drift."
    )


def test_app_defines_every_cap_exactly_once() -> None:
    """Each cap is a module-level assignment in exactly one _app definer module."""
    definers: dict[str, list[str]] = {name: [] for name in _POLICY_NAMES}
    for path in _DEFINERS:
        for name in _module_level_assigned_names(path) & _POLICY_NAMES:
            definers[name].append(path.name)
    for name, where in definers.items():
        assert len(where) == 1, f"{name} must be defined once in _app; found in {where}"


# The batch adapters that must IMPORT the fatal classifier, never fork it.
_BATCH_ADAPTERS = [
    _SRC / "server" / "routes" / "sources.py",
    _SRC / "mcp" / "tools" / "sources.py",
]


@pytest.mark.parametrize("adapter", _BATCH_ADAPTERS, ids=lambda p: str(p.relative_to(_SRC)))
def test_batch_adapters_import_the_fatal_classifier_and_dont_fork_it(adapter: Path) -> None:
    """Dependency-free (AST) no-fork guard: each batch adapter must IMPORT
    ``batch_item_is_fatal`` from ``_app.source_batch`` and must NOT define its own.

    This runs even when the ``fastmcp`` / ``fastapi`` extras are absent (the common
    ``--extra dev`` contributor install), unlike the object-identity check below —
    so a future MCP-local re-implementation of the classifier can't slip in behind a
    skipped guard.
    """
    assert "batch_item_is_fatal" in _imported_names(adapter), (
        f"{adapter.relative_to(_SRC)} must import batch_item_is_fatal from "
        "_app.source_batch (the single fatal-vs-isolate definition)."
    )
    assert "batch_item_is_fatal" not in _local_def_names(adapter), (
        f"{adapter.relative_to(_SRC)} defines its own batch_item_is_fatal — that is "
        "the fork this consolidation exists to prevent. Import it from _app.source_batch."
    )


def test_both_adapters_share_the_same_fatal_classifier() -> None:
    """Both adapters bind the exact same ``batch_item_is_fatal`` object (no fork).

    Stronger than the AST guard above (proves object identity, not just an import by
    name), but requires the transport extras — hence the skip. The AST guard is the
    always-on backstop for extra-less installs.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("fastmcp")
    from notebooklm._app.source_batch import batch_item_is_fatal as canonical
    from notebooklm.mcp.tools import sources as mcp_sources
    from notebooklm.server.routes import sources as rest_sources

    assert mcp_sources.batch_item_is_fatal is canonical
    assert rest_sources.batch_item_is_fatal is canonical


# ===========================================================================
# File-extension policy: ONE declaration behind every consumer (#2202)
# ===========================================================================
#
# Before #2202 the path-shape heuristic (``_app.source_add``), the Drive
# download+upload router (``_web.sources.drive_import``) and the upload HTML reject
# gate (``_web.sources._upload_decode``) each hand-maintained their own extension
# list. They drifted: ``.pptx`` gained a decode entry in #2191 while the Drive
# router still refused it and a mistyped ``deck.pptx`` still got no warning, and
# ``.xhtml`` was rejectable by the uploader while not counting as file-shaped.
# All three now derive from ``_types.sources``, and these gates pin that so the
# next supported file type cannot land in only one of them.


def test_path_shaped_set_is_the_union_of_the_three_declarations() -> None:
    """The path heuristic is exactly "uploadable OR HTML OR file-shaped-only" — derived, not copied."""
    from notebooklm._app.source_add import _PATH_SHAPED_EXTENSIONS
    from notebooklm._types.sources import (
        _FILE_SHAPED_ONLY_EXTENSIONS,
        _HTML_FILE_EXTENSIONS,
        _UPLOAD_FILE_EXTENSIONS,
    )

    assert _PATH_SHAPED_EXTENSIONS == (
        _UPLOAD_FILE_EXTENSIONS | _HTML_FILE_EXTENSIONS | _FILE_SHAPED_ONLY_EXTENSIONS
    )


def test_file_shaped_only_extensions_never_reach_the_drive_network_gate() -> None:
    """The honesty gate: an unverified extension is file-shaped but NOT upload-routed.

    The two sets have asymmetric failure modes — a wrong entry in the path
    heuristic costs a spurious warning, a wrong entry in the Drive router turns a
    fast client-side refusal into a full download plus a murky server-side
    failure. So an extension without wire evidence (``.ppt``) must stay out of
    the upload set, and out of everything derived from it.
    """
    from notebooklm._types.sources import (
        _FILE_SHAPED_ONLY_EXTENSIONS,
        _PATH_SHAPED_FILE_EXTENSIONS,
        _UPLOAD_FILE_EXTENSIONS,
    )
    from notebooklm._web.sources.drive_import import _UPLOAD_SUPPORTED_EXTS

    assert _FILE_SHAPED_ONLY_EXTENSIONS
    # Recognised as a filename...
    assert _FILE_SHAPED_ONLY_EXTENSIONS <= _PATH_SHAPED_FILE_EXTENSIONS
    # ...but never claimed as uploadable, on either spelling.
    assert not (_FILE_SHAPED_ONLY_EXTENSIONS & _UPLOAD_FILE_EXTENSIONS)
    bare = {ext.lstrip(".") for ext in _FILE_SHAPED_ONLY_EXTENSIONS}
    assert not (bare & _UPLOAD_SUPPORTED_EXTS)


def test_drive_router_extensions_are_the_same_declaration_dot_stripped() -> None:
    """``drive_import`` compares bare suffixes, but against the SAME source of truth."""
    from notebooklm._types.sources import _HTML_FILE_EXTENSIONS, _UPLOAD_FILE_EXTENSIONS
    from notebooklm._web.sources.drive_import import _HTML_EXTS, _UPLOAD_SUPPORTED_EXTS

    assert {ext.lstrip(".") for ext in _UPLOAD_FILE_EXTENSIONS} == _UPLOAD_SUPPORTED_EXTS
    assert {ext.lstrip(".") for ext in _HTML_FILE_EXTENSIONS} == _HTML_EXTS


def test_upload_html_reject_gate_binds_the_same_html_set() -> None:
    """Object identity, not just equality: the upload gate must not fork the HTML set."""
    from notebooklm._types.sources import _HTML_FILE_EXTENSIONS
    from notebooklm._web.sources._upload_decode import _HTML_UPLOAD_SUFFIXES

    assert _HTML_UPLOAD_SUFFIXES is _HTML_FILE_EXTENSIONS


def test_extension_sets_are_pairwise_disjoint_and_dotted() -> None:
    """Each extension lands in exactly one of the three sets, and is always dotted.

    ``looks_like_path`` compares against ``Path(...).suffix``, which is dotted and
    lowercase-compared — an undotted entry would silently never match.
    """
    from notebooklm._types.sources import (
        _FILE_SHAPED_ONLY_EXTENSIONS,
        _HTML_FILE_EXTENSIONS,
        _UPLOAD_FILE_EXTENSIONS,
    )

    sets = (_UPLOAD_FILE_EXTENSIONS, _HTML_FILE_EXTENSIONS, _FILE_SHAPED_ONLY_EXTENSIONS)
    assert len(
        _UPLOAD_FILE_EXTENSIONS | _HTML_FILE_EXTENSIONS | _FILE_SHAPED_ONLY_EXTENSIONS
    ) == sum(len(one) for one in sets)
    for ext in _UPLOAD_FILE_EXTENSIONS | _HTML_FILE_EXTENSIONS | _FILE_SHAPED_ONLY_EXTENSIONS:
        assert ext.startswith("."), f"{ext!r} must be spelled with a leading dot"
        assert ext == ext.lower(), f"{ext!r} must be lowercase"


def test_accepted_extension_sets_are_pinned_by_content() -> None:
    """Pin the ACTUAL members, not just the derivation.

    The three parity gates above prove nobody re-forked the lists, but they
    compare the declaration to itself — deleting ``.pdf`` would break the Drive
    route and every one of them would still pass. This is the content half:
    changing what the client claims NotebookLM accepts has to be deliberate, and
    (for anything added) backed by a probe.
    """
    from notebooklm._types.sources import (
        _FILE_SHAPED_ONLY_EXTENSIONS,
        _HTML_FILE_EXTENSIONS,
        _UPLOAD_FILE_EXTENSIONS,
    )

    # Every member has been live-probed to READY on the Web upload endpoint.
    # ``.doc``/``.odt``/``.rtf``/``.tsv`` were removed on 2026-08-31: a real
    # file of each is refused at the resumable ``start`` with HTTP 400, and
    # identically so with the content type forced to ``text/plain``, so the
    # endpoint refuses them by extension rather than by MIME. Adding one back
    # needs a probe, not a hunch.
    assert {
        ".csv",
        ".docx",
        ".epub",
        ".markdown",
        ".md",
        ".pdf",
        ".pptx",
        ".txt",
    } == _UPLOAD_FILE_EXTENSIONS
    assert {".htm", ".html", ".xht", ".xhtml"} == _HTML_FILE_EXTENSIONS
    assert {".doc", ".odt", ".ppt", ".rtf", ".tsv"} == _FILE_SHAPED_ONLY_EXTENSIONS


def test_powerpoint_is_spelled_on_both_the_decode_and_input_sides() -> None:
    """#2137/#2191 gave PowerPoint a decode code; #2202 gives it its input spellings.

    Split by evidence: ``.pptx`` was live-probed onto the wire, ``.ppt`` was not.
    """
    from notebooklm._types.sources import (
        _FILE_SHAPED_ONLY_EXTENSIONS,
        _PATH_SHAPED_FILE_EXTENSIONS,
        _SOURCE_TYPE_CODE_MAP,
        _UPLOAD_FILE_EXTENSIONS,
        SourceType,
    )

    assert SourceType.POWERPOINT in _SOURCE_TYPE_CODE_MAP.values()
    assert ".pptx" in _UPLOAD_FILE_EXTENSIONS
    assert ".ppt" in _FILE_SHAPED_ONLY_EXTENSIONS
    assert {".ppt", ".pptx"} <= _PATH_SHAPED_FILE_EXTENSIONS


def test_source_type_codes_match_the_recovered_android_enum() -> None:
    """Pin the decode map against ``OriginalSourceContentType`` from the app binary.

    The two are the same server-side numbering. Checking them against each
    other is what exposed code ``14`` being labelled ``GOOGLE_SPREADSHEET``
    when the enum calls it ``DRIVE`` -- a mislabel that had already forced two
    workarounds (a Drive-PDF MIME override, and an Android-side 7->14 remap).

    The code set is read from the **protobuf descriptor**, never transcribed. A
    hand-copied fixture silently stopped at 18 on the first attempt and would
    have certified parity while omitting 19 and 20. Only the name pairing is
    written out, because the two vocabularies genuinely differ
    (``SOURCE_CONTENT_TYPE_WORD`` <-> ``DOCX``); a value with no pairing fails
    here rather than defaulting to anything.
    """
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
        read_pb2,
    )
    from notebooklm._types.sources import _SOURCE_TYPE_CODE_MAP, SourceType

    recovered = {
        value.number: value.name for value in read_pb2.OriginalSourceContentType.DESCRIPTOR.values
    }
    equivalent = {
        "SOURCE_CONTENT_TYPE_UNKNOWN": SourceType.UNKNOWN,
        "SOURCE_CONTENT_TYPE_GOOGLE_DOC": SourceType.GOOGLE_DOCS,
        "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES": SourceType.GOOGLE_SLIDES,
        "SOURCE_CONTENT_TYPE_PDF": SourceType.PDF,
        "SOURCE_CONTENT_TYPE_TEXT": SourceType.PASTED_TEXT,
        "SOURCE_CONTENT_TYPE_URL": SourceType.WEB_PAGE,
        "SOURCE_CONTENT_TYPE_POWERPOINT": SourceType.POWERPOINT,
        "SOURCE_CONTENT_TYPE_GOOGLE_SHEET": SourceType.GOOGLE_SPREADSHEET,
        "SOURCE_CONTENT_TYPE_MARKDOWN": SourceType.MARKDOWN,
        "SOURCE_CONTENT_TYPE_YOUTUBE_VIDEO": SourceType.YOUTUBE,
        "SOURCE_CONTENT_TYPE_AUDIO": SourceType.MEDIA,
        "SOURCE_CONTENT_TYPE_WORD": SourceType.DOCX,
        "SOURCE_CONTENT_TYPE_EXCEL": SourceType.EXCEL,
        "SOURCE_CONTENT_TYPE_IMAGE": SourceType.IMAGE,
        "SOURCE_CONTENT_TYPE_DRIVE": SourceType.GOOGLE_DRIVE,
        "SOURCE_CONTENT_TYPE_GMAIL": SourceType.GMAIL,
        "SOURCE_CONTENT_TYPE_CSV": SourceType.CSV,
        "SOURCE_CONTENT_TYPE_EPUB": SourceType.EPUB,
        "SOURCE_CONTENT_TYPE_GEMINI_CHAT": SourceType.GEMINI_CHAT,
        "SOURCE_CONTENT_TYPE_AI_MODE_CHAT": SourceType.AI_MODE_CHAT,
        "SOURCE_CONTENT_TYPE_EXPERT_INTELLIGENCE": SourceType.EXPERT_INTELLIGENCE,
    }

    assert set(recovered.values()) <= set(equivalent), (
        "recovered enum value(s) with no public member: "
        f"{sorted(set(recovered.values()) - set(equivalent))}. Add the member and "
        "map the code, or record why it stays unmapped."
    )
    assert set(_SOURCE_TYPE_CODE_MAP) == set(recovered), (
        "decode map and recovered enum disagree on which codes exist: "
        f"{sorted(set(_SOURCE_TYPE_CODE_MAP) ^ set(recovered))}"
    )
    assert {code: equivalent[name] for code, name in recovered.items()} == _SOURCE_TYPE_CODE_MAP


def test_source_type_code_map_is_written_in_numeric_order() -> None:
    """Keeps the contiguity of the map auditable by reading it."""
    from notebooklm._types.sources import _SOURCE_TYPE_CODE_MAP

    codes = list(_SOURCE_TYPE_CODE_MAP)
    assert codes == sorted(codes)
    assert codes == list(range(len(codes))), (
        f"expected contiguous codes from 0; got gaps at "
        f"{sorted(set(range(max(codes) + 1)) - set(codes))}"
    )


def test_android_name_map_agrees_with_the_public_decode_map() -> None:
    """The Android codec translates by name; the codes must still line up.

    It used to remap ``GOOGLE_SHEET`` 7 -> 14 to compensate for the mislabel
    above. With that gone the translation is the identity, and this keeps it so.
    """
    from notebooklm._android.codecs.sources import _SOURCE_TYPE_CODE_BY_NAME
    from notebooklm._types.sources import _SOURCE_TYPE_CODE_MAP

    codes = sorted(_SOURCE_TYPE_CODE_BY_NAME.values())
    assert len(codes) == len(set(codes)), "two Android names share one code"
    assert set(codes) <= set(_SOURCE_TYPE_CODE_MAP), (
        "Android emits codes the public map cannot decode: "
        f"{sorted(set(codes) - set(_SOURCE_TYPE_CODE_MAP))}"
    )
