"""Pin the exact abstract surface of each backend-neutral namespace base.

A shared workflow is allowed at most one wire hook. Namespace split commits
add an entry here in the same change that makes the public API class abstract,
so a new abstract read or hook is always an explicit review-visible diff.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint


@dataclass(frozen=True)
class _AbstractContract:
    module: str
    class_name: str
    implementation_module: str
    implementation_class_name: str
    abstract_methods: frozenset[str]
    wire_hooks: frozenset[str]


# A4-A9 append one contract per namespace split.
BASE_ABSTRACT_CONTRACTS: tuple[_AbstractContract, ...] = (
    _AbstractContract(
        module="notebooklm._research",
        class_name="BaseResearchAPI",
        implementation_module="notebooklm._web.research",
        implementation_class_name="WebResearchAPI",
        abstract_methods=frozenset({"start", "discover", "poll", "cancel", "import_sources"}),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._artifacts",
        class_name="ArtifactsAPI",
        implementation_module="notebooklm._web.artifacts",
        implementation_class_name="WebArtifactsAPI",
        abstract_methods=frozenset(
            {
                "_list_studio",
                "_send_create_artifact",
                "copy",
                "delete",
                "download_audio",
                "download_data_table",
                "download_flashcards",
                "download_infographic",
                "download_mind_map",
                "download_quiz",
                "download_report",
                "download_slide_deck",
                "download_video",
                "export",
                "export_data_table",
                "export_report",
                "generate_mind_map",
                "get_customization_choices",
                "get_prompt",
                "list",
                "rename",
                "retry_failed",
                "revise_slide",
                "suggest_reports",
            }
        ),
        wire_hooks=frozenset({"_send_create_artifact"}),
    ),
    _AbstractContract(
        module="notebooklm._notebooks",
        class_name="NotebooksAPI",
        implementation_module="notebooklm._web.notebooks",
        implementation_class_name="WebNotebooksAPI",
        abstract_methods=frozenset(
            {
                "_send_create",
                "copy",
                "delete",
                "get",
                "get_description",
                "get_raw",
                "get_source_ids",
                "get_summary",
                "list",
                "remove_from_recent",
                "suggest_next_steps",
                "suggest_prompts",
                "update",
            }
        ),
        wire_hooks=frozenset({"_send_create"}),
    ),
    _AbstractContract(
        module="notebooklm._sources",
        class_name="SourcesAPI",
        implementation_module="notebooklm._web.sources",
        implementation_class_name="WebSourcesAPI",
        abstract_methods=frozenset(
            {
                "add_drive",
                "add_drive_file",
                "add_file",
                "add_play_book",
                "add_text",
                "add_url",
                "add_urls_async",
                "append_text",
                "check_freshness",
                "copy",
                "delete",
                "get_fulltext",
                "get_guide",
                "list",
                "list_play_books",
                "refresh",
                "rename",
                "search",
            }
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._chat",
        class_name="ChatAPI",
        implementation_module="notebooklm._web.chat",
        implementation_class_name="WebChatAPI",
        abstract_methods=frozenset(
            {
                "_list_turn_roles",
                "_cancel_generation",
                "_get_session_status",
                "_send_delete_conversation",
                "_send_note",
                "_stream_answer",
                "configure",
                "get_conversation_id",
                "get_conversation_turns",
                "get_history",
                "get_settings",
            }
        ),
        wire_hooks=frozenset(
            {
                "_cancel_generation",
                "_get_session_status",
                "_send_delete_conversation",
                "_send_note",
                "_stream_answer",
            }
        ),
    ),
    _AbstractContract(
        module="notebooklm._collections",
        class_name="CollectionsAPI",
        implementation_module="notebooklm._web.collections",
        implementation_class_name="WebCollectionsAPI",
        abstract_methods=frozenset(
            {
                "add_notebooks",
                "create",
                "delete",
                "get",
                "get_or_none",
                "list",
                "notebooks",
                "remove_notebooks",
                "rename",
            }
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._labels",
        class_name="LabelsAPI",
        implementation_module="notebooklm._web.labels",
        implementation_class_name="WebLabelsAPI",
        abstract_methods=frozenset(
            {
                "add_sources",
                "create",
                "delete",
                "generate",
                "get",
                "get_or_none",
                "list",
                "remove_sources",
                "rename",
                "set_emoji",
                "sources",
                "update",
            }
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._notes",
        class_name="NotesAPI",
        implementation_module="notebooklm._web.notes",
        implementation_class_name="WebNotesAPI",
        abstract_methods=frozenset(
            {
                "create",
                "delete",
                "delete_mind_map",
                "get",
                "get_or_none",
                "list",
                "list_mind_maps",
                "update",
            }
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._sharing",
        class_name="SharingAPI",
        implementation_module="notebooklm._web.sharing",
        implementation_class_name="WebSharingAPI",
        abstract_methods=frozenset(
            {"get_status", "remove_user", "set_public", "set_users", "set_view_level"}
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._settings",
        class_name="SettingsAPI",
        implementation_module="notebooklm._web.settings",
        implementation_class_name="WebSettingsAPI",
        abstract_methods=frozenset(
            {
                "get_account_limits",
                "get_output_language",
                "get_user_settings",
                "set_output_language",
            }
        ),
        wire_hooks=frozenset(),
    ),
    _AbstractContract(
        module="notebooklm._mind_maps_api",
        class_name="MindMapsAPI",
        implementation_module="notebooklm._web.mind_maps",
        implementation_class_name="WebMindMapsAPI",
        abstract_methods=frozenset(
            {
                "_send_rename_note_backed",
                "generate",
                "get_tree",
                "list_note_backed",
            }
        ),
        wire_hooks=frozenset({"_send_rename_note_backed"}),
    ),
)

_WIRE_HOOK_PREFIXES = ("_send_",)
_WIRE_HOOK_NAMES = frozenset({"_cancel_generation", "_get_session_status", "_stream_answer"})

_ARTIFACT_DOCSTRING_SHA256 = {
    ("ArtifactsAPI", "class"): "a46bd93059bf56db9586a741a0df1aca8b49a30cf74007a74e27997166ebb482",
    (
        "ArtifactsAPI",
        "__init__",
    ): "9ec76bfff5af89ad60160608597d1662260664f30aff665871151fb16a7852d0",
    (
        "WebArtifactsAPI",
        "class",
    ): "a46bd93059bf56db9586a741a0df1aca8b49a30cf74007a74e27997166ebb482",
    (
        "WebArtifactsAPI",
        "__init__",
    ): "d1b96af651ebc15337c480fd5d9cdb4efb6948dc326f2e6ecc08406982b2e701",
}


def test_backend_base_abstract_methods_and_wire_hooks_match_manifest() -> None:
    for contract in BASE_ABSTRACT_CONTRACTS:
        module = importlib.import_module(contract.module)
        base = getattr(module, contract.class_name)
        actual = frozenset(base.__abstractmethods__)
        actual_wire_hooks = frozenset(
            name
            for name in actual
            if name.startswith(_WIRE_HOOK_PREFIXES) or name in _WIRE_HOOK_NAMES
        )

        assert actual == contract.abstract_methods, (
            f"{contract.class_name} abstract surface changed: "
            f"expected {sorted(contract.abstract_methods)}, got {sorted(actual)}"
        )
        assert actual_wire_hooks == contract.wire_hooks, (
            f"{contract.class_name} wire hooks changed: "
            f"expected {sorted(contract.wire_hooks)}, got {sorted(actual_wire_hooks)}"
        )


def test_artifact_workflow_ownership_and_docstrings_are_preserved() -> None:
    """Moving workflows onto the neutral base must not shrink runtime help text."""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._web.artifacts import WebArtifactsAPI

    inherited_workflows = {
        "generate_audio",
        "generate_cinematic_video",
        "generate_data_table",
        "generate_flashcards",
        "generate_infographic",
        "generate_quiz",
        "generate_report",
        "generate_slide_deck",
        "generate_study_guide",
        "generate_video",
        "get",
        "get_or_none",
        "list_audio",
        "list_data_tables",
        "list_flashcards",
        "list_infographics",
        "list_quizzes",
        "list_reports",
        "list_slide_decks",
        "list_video",
        "poll_status",
        "wait_for_completion",
    }
    web_overrides = ArtifactsAPI.__abstractmethods__ - {
        "_list_studio",
        "_send_create_artifact",
    }

    for name in inherited_workflows:
        base_method = getattr(ArtifactsAPI, name)
        assert getattr(WebArtifactsAPI, name) is base_method
        assert base_method.__doc__, f"ArtifactsAPI.{name} lost its public docstring"

    for name in web_overrides:
        base_doc = getattr(ArtifactsAPI, name).__doc__
        web_doc = getattr(WebArtifactsAPI, name).__doc__
        assert base_doc
        assert web_doc == base_doc, f"WebArtifactsAPI.{name} docstring drifted"


def test_artifact_class_constructor_docstrings_and_web_signature_are_pinned() -> None:
    """B0 pins the one-supervisor constructor boundary and runtime help."""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._web.artifacts import WebArtifactsAPI

    owners = {"ArtifactsAPI": ArtifactsAPI, "WebArtifactsAPI": WebArtifactsAPI}
    for (owner_name, member_name), expected in _ARTIFACT_DOCSTRING_SHA256.items():
        owner = owners[owner_name]
        target = owner if member_name == "class" else getattr(owner, member_name)
        doc = inspect.getdoc(target)
        assert doc is not None
        assert hashlib.sha256(doc.encode()).hexdigest() == expected

    base_parameters = inspect.signature(ArtifactsAPI).parameters
    assert tuple(base_parameters) == ("supervisor", "notebooks", "asset_downloads")
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in base_parameters.values()
    )
    assert all(
        parameter.default is inspect.Parameter.empty for parameter in base_parameters.values()
    )

    web_parameters = inspect.signature(WebArtifactsAPI).parameters
    assert tuple(web_parameters) == (
        "rpc",
        "supervisor",
        "notebooks",
        "mind_maps",
        "note_service",
        "storage_path",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in web_parameters.values()
    )


def test_logger_names_survive_web_module_moves() -> None:
    """Existing log filters keep observing events after ownership moves."""
    expected = {
        "notebooklm._chat": "notebooklm._chat.api",
        "notebooklm._web.artifacts": "notebooklm._artifacts",
        "notebooklm._web.artifact.downloads": "notebooklm._artifact.downloads",
        "notebooklm._web.artifact.generation": "notebooklm._artifact.generation",
        "notebooklm._web.artifact.listing": "notebooklm._artifact.listing",
        "notebooklm._web.artifact.table": "notebooklm._artifacts",
        "notebooklm._web.chat": "notebooklm._chat.api",
        "notebooklm._web.collections": "notebooklm._collections",
        "notebooklm._web.labels": "notebooklm._labels",
        "notebooklm._web.rows.documents": "notebooklm._row_adapters.documents",
        "notebooklm._web.rows.notebooks": "notebooklm._types.notebooks",
        "notebooklm._web.rows.research": "notebooklm._row_adapters.research",
        "notebooklm._web.rows.research_task": "notebooklm._research_task_parser",
        "notebooklm._web.rows.sharing": "notebooklm._types.sharing",
        "notebooklm._web.rows.sources": "notebooklm._row_adapters.sources",
    }
    actual = {module: importlib.import_module(module).logger.name for module in expected}
    assert actual == expected


def test_chat_shared_workflows_call_only_their_single_wire_hook() -> None:
    """Pin the one protected-adapter hook used by each shared Chat workflow."""
    path = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_chat.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    chat = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatAPI"
    )
    expected = {
        "ask": {"_stream_answer"},
        "delete_conversation": {"_send_delete_conversation"},
        "save_answer_as_note": {"_send_note"},
    }
    for method_name, expected_hooks in expected.items():
        method = next(
            node
            for node in chat.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        hooks = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (node.func.attr.startswith("_send_") or node.func.attr == "_stream_answer")
        }
        assert hooks == expected_hooks


def test_backend_implementations_preserve_abstract_method_signatures() -> None:
    """Concrete backends cannot widen, reorder, or reannotate public contracts."""
    for contract in BASE_ABSTRACT_CONTRACTS:
        base = getattr(importlib.import_module(contract.module), contract.class_name)
        implementation = getattr(
            importlib.import_module(contract.implementation_module),
            contract.implementation_class_name,
        )
        for method_name in contract.abstract_methods:
            base_method = getattr(base, method_name)
            implementation_method = getattr(implementation, method_name)
            assert inspect.signature(implementation_method, eval_str=True) == inspect.signature(
                base_method, eval_str=True
            ), (
                f"{contract.implementation_class_name}.{method_name} does not match "
                f"{contract.class_name}.{method_name}"
            )
            assert inspect.iscoroutinefunction(
                implementation_method
            ) == inspect.iscoroutinefunction(base_method)


def test_web_sharing_inherits_neutral_concrete_workflows() -> None:
    """The two shared upsert projections must not fork in the web backend."""
    from notebooklm._sharing import SharingAPI
    from notebooklm._web.sharing import WebSharingAPI

    for method_name in ("add_user", "update_user"):
        assert method_name not in WebSharingAPI.__dict__
        assert getattr(WebSharingAPI, method_name) is getattr(SharingAPI, method_name)
