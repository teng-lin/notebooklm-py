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
        abstract_methods=frozenset({"_send_import", "start", "discover", "poll", "cancel"}),
        wire_hooks=frozenset({"_send_import"}),
    ),
    _AbstractContract(
        module="notebooklm._artifacts",
        class_name="ArtifactsAPI",
        implementation_module="notebooklm._web.artifacts",
        implementation_class_name="WebArtifactsAPI",
        abstract_methods=frozenset(
            {
                "_list_studio",
                "_read_customization_choices",
                "_send_copy",
                "_send_create_artifact",
                "_send_export",
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
                "generate_mind_map",
                "get_prompt",
                "list",
                "rename",
                "retry_failed",
                "revise_slide",
                "suggest_reports",
            }
        ),
        wire_hooks=frozenset(
            {
                "_read_customization_choices",
                "_send_copy",
                "_send_create_artifact",
                "_send_export",
            }
        ),
    ),
    _AbstractContract(
        module="notebooklm._notebooks",
        class_name="NotebooksAPI",
        implementation_module="notebooklm._web.notebooks",
        implementation_class_name="WebNotebooksAPI",
        abstract_methods=frozenset(
            {
                "_send_copy",
                "_send_create",
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
        wire_hooks=frozenset({"_send_copy", "_send_create"}),
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
                "add_play_book",
                "add_text",
                "add_url",
                "check_freshness",
                "delete",
                "get_fulltext",
                "get_guide",
                "list",
                "list_play_books",
                "refresh",
                "rename",
                "search",
                "_send_add_urls_async",
                "_send_append_text",
                "_send_copy",
                "_send_upload",
            }
        ),
        wire_hooks=frozenset(
            {"_send_add_urls_async", "_send_append_text", "_send_copy", "_send_upload"}
        ),
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
                "_read_settings",
                "_send_configure",
                "_send_delete_conversation",
                "_send_note",
                "_stream_answer",
                "get_conversation_id",
                "get_conversation_turns",
                "get_history",
            }
        ),
        wire_hooks=frozenset(
            {
                "_cancel_generation",
                "_get_session_status",
                "_list_turn_roles",
                "_read_settings",
                "_send_configure",
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
                "_send_mutate_member",
                "_send_update",
                "create",
                "list",
            }
        ),
        wire_hooks=frozenset({"_send_mutate_member", "_send_update"}),
    ),
    _AbstractContract(
        module="notebooklm._labels",
        class_name="LabelsAPI",
        implementation_module="notebooklm._web.labels",
        implementation_class_name="WebLabelsAPI",
        abstract_methods=frozenset(
            {
                "create",
                "generate",
                "list",
                "_send_mutate_member",
                "_send_update",
            }
        ),
        wire_hooks=frozenset({"_send_mutate_member", "_send_update"}),
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
                "_list_studio_mind_map_rows",
                "_read_interactive_tree",
                "_send_rename_note_backed",
                "_start_interactive_mind_map",
                "list_note_backed",
            }
        ),
        wire_hooks=frozenset(
            {
                "_list_studio_mind_map_rows",
                "_read_interactive_tree",
                "_send_rename_note_backed",
                "_start_interactive_mind_map",
            }
        ),
    ),
)

_ANDROID_IMPLEMENTATIONS = {
    "ArtifactsAPI": ("notebooklm._android.artifacts", "AndroidArtifactsAPI"),
    "BaseResearchAPI": ("notebooklm._android.research", "AndroidResearchAPI"),
    "ChatAPI": ("notebooklm._android.chat", "AndroidChatAPI"),
    "CollectionsAPI": ("notebooklm._android.collections", "AndroidCollectionsAPI"),
    "LabelsAPI": ("notebooklm._android.labels", "AndroidLabelsAPI"),
    "MindMapsAPI": ("notebooklm._android.mind_maps", "AndroidMindMapsAPI"),
    "NotebooksAPI": ("notebooklm._android.notebooks", "AndroidNotebooksAPI"),
    "NotesAPI": ("notebooklm._android.notes", "AndroidNotesAPI"),
    "SettingsAPI": ("notebooklm._android.settings", "AndroidSettingsAPI"),
    "SharingAPI": ("notebooklm._android.sharing", "AndroidSharingAPI"),
    "SourcesAPI": ("notebooklm._android.sources", "AndroidSourcesAPI"),
}

_ANDROID_INHERITED_WORKFLOWS = {
    "ArtifactsAPI": frozenset(
        {
            "copy",
            "export",
            "export_data_table",
            "export_report",
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
            "get_customization_choices",
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
    ),
    "BaseResearchAPI": frozenset(
        {
            "_import_sources_with_verification",
            "_import_sources_with_verification_in_scope",
            "_wait_for_completion",
            "_wait_for_completion_in_scope",
            "import_sources",
            "import_sources_with_verification",
            "wait_for_completion",
        }
    ),
    "ChatAPI": frozenset(
        {
            "_count_prior_server_turns",
            "ask",
            "cancel",
            "configure",
            "delete_conversation",
            "get_settings",
            "save_answer_as_note",
            "session_status",
            "set_mode",
        }
    ),
    "CollectionsAPI": frozenset(
        {
            "_mutate_members",
            "add_notebooks",
            "delete",
            "get",
            "get_or_none",
            "notebooks",
            "remove_notebooks",
            "rename",
        }
    ),
    "LabelsAPI": frozenset(
        {
            "_mutate_members",
            "add_sources",
            "delete",
            "get",
            "get_or_none",
            "remove_sources",
            "rename",
            "set_emoji",
            "sources",
            "update",
        }
    ),
    "MindMapsAPI": frozenset(
        {
            "_delete_in_scope",
            "_detect_kind",
            "_rename_in_scope",
            "delete",
            "get",
            "get_or_none",
            "get_tree",
            "generate",
            "list",
            "rename",
        }
    ),
    "NotebooksAPI": frozenset(
        {
            "_create_with_probe",
            "copy",
            "create",
            "get_metadata",
            "get_or_none",
            "rename",
            "set_emoji",
        }
    ),
    "NotesAPI": frozenset(),
    "SettingsAPI": frozenset(),
    "SharingAPI": frozenset({"add_user", "update_user"}),
    "SourcesAPI": frozenset(
        {
            "delete_many",
            "add_file",
            "add_urls_async",
            "append_text",
            "copy",
            "get",
            "get_or_none",
            "wait_all_until_ready",
            "wait_for_sources",
            "wait_until_ready",
            "wait_until_registered",
        }
    ),
}

_TEMPLATE_HOOKS = frozenset({"_operation_scope"})
_WEB_SCOPE_OVERRIDES = frozenset({"SourcesAPI"})

_WIRE_HOOK_PREFIXES = ("_send_",)
_WIRE_HOOK_NAMES = frozenset(
    {
        "_cancel_generation",
        "_get_session_status",
        "_list_turn_roles",
        "_read_customization_choices",
        "_read_settings",
        "_list_studio_mind_map_rows",
        "_read_interactive_tree",
        "_start_interactive_mind_map",
        "_stream_answer",
    }
)

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


def test_namespace_bases_and_backends_preserve_the_scope_template_hook() -> None:
    """Keep lifecycle policy separate from each workflow's one wire hook."""
    assert (
        {contract.class_name for contract in BASE_ABSTRACT_CONTRACTS}
        == set(_ANDROID_IMPLEMENTATIONS)
        == set(_ANDROID_INHERITED_WORKFLOWS)
    )

    for contract in BASE_ABSTRACT_CONTRACTS:
        base = getattr(importlib.import_module(contract.module), contract.class_name)
        web = getattr(
            importlib.import_module(contract.implementation_module),
            contract.implementation_class_name,
        )
        android_module, android_name = _ANDROID_IMPLEMENTATIONS[contract.class_name]
        android = getattr(importlib.import_module(android_module), android_name)

        actual_hooks = frozenset(name for name in _TEMPLATE_HOOKS if name in base.__dict__)
        assert actual_hooks == _TEMPLATE_HOOKS
        assert not getattr(base._operation_scope, "__isabstractmethod__", False)
        if contract.class_name in _WEB_SCOPE_OVERRIDES:
            assert web._operation_scope is not base._operation_scope
        else:
            assert web._operation_scope is base._operation_scope
        assert android._operation_scope is not base._operation_scope


def test_android_backends_inherit_manifested_neutral_workflow_bodies() -> None:
    """A scope-only Android override may not fork a neutral workflow body."""
    for contract in BASE_ABSTRACT_CONTRACTS:
        base = getattr(importlib.import_module(contract.module), contract.class_name)
        android_module, android_name = _ANDROID_IMPLEMENTATIONS[contract.class_name]
        android = getattr(importlib.import_module(android_module), android_name)

        for method_name in _ANDROID_INHERITED_WORKFLOWS[contract.class_name]:
            assert method_name not in android.__dict__
            assert getattr(android, method_name) is getattr(base, method_name)


def test_artifact_workflow_ownership_and_docstrings_are_preserved() -> None:
    """Moving workflows onto the neutral base must not shrink runtime help text."""
    from notebooklm._artifacts import ArtifactsAPI
    from notebooklm._web.artifacts import WebArtifactsAPI

    inherited_workflows = {
        "copy",
        "export",
        "export_data_table",
        "export_report",
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
        "get_customization_choices",
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
        "_send_copy",
        "_send_create_artifact",
        "_send_export",
        "_read_customization_choices",
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

    assert WebArtifactsAPI.get_customization_choices is ArtifactsAPI.get_customization_choices


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


def test_research_neutral_helpers_are_inherited_without_a_web_import_cycle() -> None:
    """Research sharing stays neutral and keeps compatibility in the Web leaf."""
    from notebooklm._android.research import AndroidResearchAPI
    from notebooklm._research import BaseResearchAPI
    from notebooklm._web.research import ResearchAPI, WebResearchAPI

    assert ResearchAPI is WebResearchAPI

    for name in ("_normalize_url", "_select_polled_tasks", "_public_poll_result"):
        assert name not in WebResearchAPI.__dict__
        assert inspect.getattr_static(WebResearchAPI, name) is inspect.getattr_static(
            BaseResearchAPI, name
        )
    assert "_web_extract_report_urls" not in WebResearchAPI.__dict__
    assert "_web_select_cited_sources" not in WebResearchAPI.__dict__
    assert "import_sources" not in WebResearchAPI.__dict__
    assert WebResearchAPI.import_sources is BaseResearchAPI.import_sources
    assert (
        WebResearchAPI._import_sources_with_verification
        is not BaseResearchAPI._import_sources_with_verification
    )
    assert (
        AndroidResearchAPI._import_sources_with_verification
        is BaseResearchAPI._import_sources_with_verification
    )

    root = Path(__file__).resolve().parents[2]
    neutral_path = root / "src" / "notebooklm" / "_research_import.py"
    assert neutral_path.is_file()
    assert not (root / "src" / "notebooklm" / "_web" / "research_import.py").exists()
    tree = ast.parse(neutral_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and (node.module in {"_web", "_android"} or node.module.startswith(("_web.", "_android.")))
        for node in ast.walk(tree)
    )
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))
    assert not any(
        (
            isinstance(node, ast.Import)
            and any(alias.name in {"asyncio", "logging"} for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.split(".", maxsplit=1)[0] in {"asyncio", "logging"}
        )
        for node in ast.walk(tree)
    )


def test_research_import_calls_only_its_single_wire_hook() -> None:
    """Classification stays in the neutral body above one backend send boundary."""
    root = Path(__file__).resolve().parents[2]
    path = root / "src" / "notebooklm" / "_research.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    research = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BaseResearchAPI"
    )
    method = next(
        node
        for node in research.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "import_sources"
    )
    hooks = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("_send_")
    }
    assert hooks == {"_send_import"}


def test_chat_shared_workflows_call_only_their_single_wire_hook() -> None:
    """Pin the one protected-adapter hook used by each shared Chat workflow."""
    path = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_chat.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    chat = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatAPI"
    )
    expected = {
        "_count_prior_server_turns": {"_list_turn_roles"},
        "ask": {"_stream_answer"},
        "configure": {"_send_configure"},
        "delete_conversation": {"_send_delete_conversation"},
        "get_settings": {"_read_settings"},
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
            and (
                node.func.attr.startswith("_send_")
                or node.func.attr in {"_list_turn_roles", "_read_settings", "_stream_answer"}
            )
        }
        assert hooks == expected_hooks


def test_artifact_shared_workflows_call_only_their_single_wire_hook() -> None:
    """Pin the protected adapter hook used by each shared artifact workflow."""
    path = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_artifacts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    artifacts = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ArtifactsAPI"
    )
    expected = {
        "copy": {"_send_copy"},
        "export": {"_send_export"},
    }
    for method_name, expected_hooks in expected.items():
        method = next(
            node
            for node in artifacts.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        )
        hooks = {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("_send_")
        }
        assert hooks == expected_hooks


def test_copy_workflows_share_the_neutral_mapping_reconciler() -> None:
    """Sources and artifacts keep one owner for post-decode copy policy."""
    root = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
    for filename, class_name in (("_sources.py", "SourcesAPI"), ("_artifacts.py", "ArtifactsAPI")):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        owner = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        method = next(
            node
            for node in owner.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "copy"
        )
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "reconcile_copy_mapping"
        ]
        assert len(calls) == 1


def test_notebook_copy_calls_only_its_single_wire_hook() -> None:
    """Pin notebook copy orchestration above one backend send boundary."""
    from notebooklm._android.notebooks import AndroidNotebooksAPI
    from notebooklm._notebooks import NotebooksAPI
    from notebooklm._web.notebooks import WebNotebooksAPI

    path = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_notebooks.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    notebooks = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NotebooksAPI"
    )
    method = next(
        node
        for node in notebooks.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "copy"
    )
    hooks = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("_send_")
    }
    assert hooks == {"_send_copy"}
    for backend in (WebNotebooksAPI, AndroidNotebooksAPI):
        assert "copy" not in backend.__dict__
        assert backend.copy is NotebooksAPI.copy
    assert WebNotebooksAPI._copy_failure_chain == "explicit"
    assert AndroidNotebooksAPI._copy_failure_chain == "suppress"


def test_created_chat_session_hint_storage_and_consumer_have_one_base_owner() -> None:
    from notebooklm._android.notebooks import AndroidNotebooksAPI
    from notebooklm._notebooks import NotebooksAPI
    from notebooklm._web.notebooks import WebNotebooksAPI

    assert "_take_created_chat_session_id" in NotebooksAPI.__dict__
    assert "_take_created_chat_session_id" not in WebNotebooksAPI.__dict__
    assert "_take_created_chat_session_id" not in AndroidNotebooksAPI.__dict__

    for implementation in (WebNotebooksAPI, AndroidNotebooksAPI):
        source = inspect.getsource(implementation.__init__)
        assert "_created_chat_session_ids" not in source


def test_chat_workflows_and_typed_reads_have_their_intended_owners() -> None:
    """Keep public policy shared and transport-specific reads in backend hooks."""
    from notebooklm._android.chat import AndroidChatAPI
    from notebooklm._chat import ChatAPI
    from notebooklm._web.chat import WebChatAPI

    for method_name in ("configure", "get_settings", "_count_prior_server_turns"):
        assert method_name not in WebChatAPI.__dict__
        assert method_name not in AndroidChatAPI.__dict__
        assert getattr(WebChatAPI, method_name) is getattr(ChatAPI, method_name)
        assert getattr(AndroidChatAPI, method_name) is getattr(ChatAPI, method_name)

    for method_name in ("_send_configure", "_read_settings", "_list_turn_roles"):
        assert method_name in ChatAPI.__abstractmethods__
        base_method = getattr(ChatAPI, method_name)
        assert getattr(WebChatAPI, method_name) is not base_method
        assert getattr(AndroidChatAPI, method_name) is not base_method


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
