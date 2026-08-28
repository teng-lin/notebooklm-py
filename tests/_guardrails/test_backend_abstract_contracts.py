"""Pin the exact abstract surface of each backend-neutral namespace base.

A shared workflow is allowed at most one wire hook. Namespace split commits
add an entry here in the same change that makes the public API class abstract,
so a new abstract read or hook is always an explicit review-visible diff.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.repo_lint


@dataclass(frozen=True)
class _AbstractContract:
    module: str
    class_name: str
    abstract_methods: frozenset[str]
    wire_hooks: frozenset[str]


# A4-A9 append one contract per namespace split.
BASE_ABSTRACT_CONTRACTS: tuple[_AbstractContract, ...] = (
    _AbstractContract(
        module="notebooklm._artifacts",
        class_name="ArtifactsAPI",
        abstract_methods=frozenset(
            {
                "_list_studio",
                "_send_create_artifact",
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
        abstract_methods=frozenset(
            {
                "_send_create",
                "delete",
                "get",
                "get_description",
                "get_raw",
                "get_source_ids",
                "get_summary",
                "list",
                "remove_from_recent",
                "suggest_prompts",
                "update",
            }
        ),
        wire_hooks=frozenset({"_send_create"}),
    ),
    _AbstractContract(
        module="notebooklm._sources",
        class_name="SourcesAPI",
        abstract_methods=frozenset(
            {
                "add_drive",
                "add_drive_file",
                "add_file",
                "add_text",
                "add_url",
                "check_freshness",
                "delete",
                "get_fulltext",
                "get_guide",
                "list",
                "refresh",
                "rename",
            }
        ),
        wire_hooks=frozenset(),
    ),
)

_WIRE_HOOK_PREFIXES = ("_send_",)
_WIRE_HOOK_NAMES = frozenset({"_stream_answer"})


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
