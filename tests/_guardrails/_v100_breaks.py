"""Auditable runway registry for the planned v1.0 public-contract removals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Runway:
    """A live warning or compatibility marker in one production module."""

    module: str
    symbol: str | None = None
    notice: str | None = None

    def __post_init__(self) -> None:
        if (self.symbol is None) == (self.notice is None):
            raise ValueError("Runway must set exactly one of symbol or notice")

    @property
    def needle(self) -> str:
        value = self.symbol if self.symbol is not None else self.notice
        assert value is not None
        return value


@dataclass(frozen=True)
class DocsRunway:
    """A docs-only deprecation whose first table cell is the verified marker."""

    anchor: str


@dataclass(frozen=True)
class BreakingChange:
    """One unique v1 removal with a live runway or explicit exemption."""

    summary: str
    runway: Runway | DocsRunway | None = None
    exemption: str | None = None


def _spec(key: str, module: str) -> Runway:
    return Runway(module=module, symbol=f'warn_registered_deprecation("{key}")')


V100_BREAKING_CHANGES: Mapping[str, BreakingChange] = MappingProxyType(
    {
        "client_legacy_constructor_options": BreakingChange(
            "Remove flat NotebookLMClient tuning keywords",
            Runway(
                module="client.py",
                symbol='warn_registered_deprecation("client_legacy_constructor_options",',
            ),
        ),
        "client_legacy_from_storage_options": BreakingChange(
            "Remove flat NotebookLMClient.from_storage tuning keywords",
            Runway(
                module="client.py",
                symbol='warn_registered_deprecation("client_legacy_from_storage_options",',
            ),
        ),
        "client_rpc_call_web": BreakingChange(
            "Remove NotebookLMClient.rpc_call Web compatibility entrypoint",
            _spec("client_rpc_call_web", "client.py"),
        ),
        "client_rpc_call_android": BreakingChange(
            "Remove NotebookLMClient.rpc_call Android-to-Web compatibility entrypoint",
            _spec("client_rpc_call_android", "client.py"),
        ),
        "artifact_poll_follower_options": BreakingChange(
            "Make artifact polling options per waiter",
            Runway(
                module="_artifact/polling.py",
                symbol='warn_registered_deprecation("artifact_poll_follower_options",',
            ),
        ),
        "artifact_poll_follower_callback": BreakingChange(
            "Deliver every observed artifact status to follower callbacks",
            _spec("artifact_poll_follower_callback", "_artifact/polling.py"),
        ),
        "`NotebookLMClient.rpc_call(...)`::Remove Android LazyWebSidecar": BreakingChange(
            "Remove the Android LazyWebSidecar compatibility graph",
            Runway(module="_client_compat.py", symbol="class LazyWebSidecar"),
        ),
        "auth_tokens_from_storage": BreakingChange(
            "Remove AuthTokens.from_storage",
            _spec("auth_tokens_from_storage", "_auth/tokens.py"),
        ),
        "auth_tokens_sync_storage_construction": BreakingChange(
            "Remove synchronous AuthTokens storage construction",
            _spec("auth_tokens_sync_storage_construction", "_auth/tokens.py"),
        ),
        "auth_tokens_flat_cookies": BreakingChange(
            "Remove AuthTokens.flat_cookies",
            _spec("auth_tokens_flat_cookies", "_auth/tokens.py"),
        ),
        "auth_tokens_replace_cookie_jar": BreakingChange(
            "Remove AuthTokens.replace_cookie_jar",
            _spec("auth_tokens_replace_cookie_jar", "_auth/tokens.py"),
        ),
        "`AuthTokens.cookies` / `AuthTokens.cookie_jar`::Remove AuthTokens.cookies": BreakingChange(
            "Remove AuthTokens.cookies",
            DocsRunway("`AuthTokens.cookies` / `AuthTokens.cookie_jar`"),
        ),
        "`AuthTokens.cookies` / `AuthTokens.cookie_jar`::Remove AuthTokens.cookie_jar": BreakingChange(
            "Remove AuthTokens.cookie_jar",
            DocsRunway("`AuthTokens.cookies` / `AuthTokens.cookie_jar`"),
        ),
        "`AuthTokens.cookies` / `AuthTokens.cookie_jar`::Change AuthTokens class shape": BreakingChange(
            "Change the AuthTokens dataclass constructor shape",
            DocsRunway("`AuthTokens.cookies` / `AuthTokens.cookie_jar`"),
        ),
        "`AuthTokens.cookie_snapshot`::Remove AuthTokens.cookie_snapshot": BreakingChange(
            "Remove AuthTokens.cookie_snapshot",
            DocsRunway("`AuthTokens.cookie_snapshot`"),
        ),
        "`AuthTokens.jar`::Remove AuthTokens.jar": BreakingChange(
            "Remove AuthTokens.jar",
            DocsRunway("`AuthTokens.jar`"),
        ),
        "`AuthTokens.cookie_header`::Remove AuthTokens.cookie_header": BreakingChange(
            "Remove AuthTokens.cookie_header",
            DocsRunway("`AuthTokens.cookie_header`"),
        ),
        "`AuthTokens.cookie_header_for(url)`::Remove AuthTokens.cookie_header_for": BreakingChange(
            "Remove AuthTokens.cookie_header_for",
            DocsRunway("`AuthTokens.cookie_header_for(url)`"),
        ),
        "artifact_from_api_response": BreakingChange(
            "Remove Artifact.from_api_response",
            _spec("artifact_from_api_response", "_types/artifacts.py"),
        ),
        "artifact_from_mind_map": BreakingChange(
            "Remove Artifact.from_mind_map",
            _spec("artifact_from_mind_map", "_types/artifacts.py"),
        ),
        "collection_from_api_response": BreakingChange(
            "Remove Collection.from_api_response",
            _spec("collection_from_api_response", "_types/collections.py"),
        ),
        "label_from_api_response": BreakingChange(
            "Remove Label.from_api_response",
            _spec("label_from_api_response", "_types/labels.py"),
        ),
        "notebook_from_api_response": BreakingChange(
            "Remove Notebook.from_api_response",
            _spec("notebook_from_api_response", "_types/notebooks.py"),
        ),
        "share_status_from_api_response": BreakingChange(
            "Remove ShareStatus.from_api_response",
            _spec("share_status_from_api_response", "_types/sharing.py"),
        ),
        "shared_user_from_api_response": BreakingChange(
            "Remove SharedUser.from_api_response",
            _spec("shared_user_from_api_response", "_types/sharing.py"),
        ),
        "source_from_api_response": BreakingChange(
            "Remove Source.from_api_response",
            _spec("source_from_api_response", "_types/sources.py"),
        ),
        "source_from_row": BreakingChange(
            "Remove Source.from_row",
            _spec("source_from_row", "_types/sources.py"),
        ),
        "mcp_confirmed_name_references": BreakingChange(
            "Reject names and partial ids on confirmed MCP mutations",
            _spec("mcp_confirmed_name_references", "mcp/_confirm.py"),
        ),
        "Awaiting `NotebookLMClient.from_storage(...)`::Remove awaitable factory path": BreakingChange(
            "Remove the awaitable NotebookLMClient.from_storage path",
            Runway(
                module="client.py",
                notice="Awaiting NotebookLMClient.from_storage(...) is deprecated",
            ),
        ),
        "Pre-profiles home-root layout::Remove home-root credential fallback": BreakingChange(
            "Remove pre-profiles home-root credential fallback",
            Runway(module="paths.py", notice="pre-profiles home-root layout"),
        ),
        "`ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)::Remove answer_start_char": BreakingChange(
            "Remove ChatReference.answer_start_char",
            DocsRunway("`ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)"),
        ),
        "`ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)::Remove answer_end_char": BreakingChange(
            "Remove ChatReference.answer_end_char",
            DocsRunway("`ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)"),
        ),
        "`Notebook.modified_at` (dataclass field)::Remove Notebook.modified_at": BreakingChange(
            "Remove Notebook.modified_at",
            DocsRunway("`Notebook.modified_at` (dataclass field)"),
        ),
        "`NotebookMetadata.modified_at` (property)::Remove NotebookMetadata.modified_at": BreakingChange(
            "Remove NotebookMetadata.modified_at",
            Runway(
                module="_types/notebooks.py",
                notice="NotebookMetadata.modified_at is deprecated",
            ),
        ),
    }
)


__all__ = [
    "BreakingChange",
    "DocsRunway",
    "Runway",
    "V100_BREAKING_CHANGES",
]
