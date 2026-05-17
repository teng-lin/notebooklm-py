"""Best-effort shell-completion providers for live NotebookLM IDs."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from click.shell_completion import CompletionItem

MAX_COMPLETION_ITEMS = 50

_AuthLoader = Callable[[Any], Any]
_AsyncRunner = Callable[[Awaitable[Any]], Any]
_ClientFactory = Callable[[Any], Any]
_NotebookResolver = Callable[[Any], str | None]
_CurrentNotebookLoader = Callable[[], str | None]


def _notebook_id_from_context(ctx: Any) -> str | None:
    """Return a parsed notebook id from a Click context chain, if present."""
    cur = ctx
    while cur is not None:
        try:
            params = getattr(cur, "params", None)
            notebook_id = params.get("notebook_id") if params else None
            if isinstance(notebook_id, str) and notebook_id:
                return notebook_id
            cur = getattr(cur, "parent", None)
        except Exception:
            return None
    return None


def _close_awaitable(awaitable: Awaitable[Any]) -> None:
    """Close an unconsumed coroutine when an injected runner fails immediately."""
    close = getattr(awaitable, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class CompletionProvider:
    """Load completion rows while preserving shell-safe failure behavior.

    All external seams are resolved lazily so existing tests and callers can
    patch ``notebooklm.cli.helpers`` and ``notebooklm.client`` symbols at call
    time. Every completion method catches broad exceptions and returns ``[]``;
    shell completion must never print diagnostics into the user's terminal.
    """

    def __init__(
        self,
        *,
        auth_loader: _AuthLoader | None = None,
        async_runner: _AsyncRunner | None = None,
        client_factory: _ClientFactory | None = None,
        notebook_resolver: _NotebookResolver | None = None,
        current_notebook_loader: _CurrentNotebookLoader | None = None,
        row_limit: int = MAX_COMPLETION_ITEMS,
    ) -> None:
        self._auth_loader = auth_loader
        self._async_runner = async_runner
        self._client_factory = client_factory
        self._notebook_resolver = notebook_resolver
        self._current_notebook_loader = current_notebook_loader
        self._row_limit = row_limit

    def complete_notebooks(self, ctx: Any, incomplete: str) -> list[CompletionItem]:
        """Complete notebook ids, filtered by prefix and capped for shells."""
        try:
            auth = self._load_auth(ctx)

            async def _list() -> Any:
                async with self._make_client(auth) as client:
                    return await client.notebooks.list()

            notebooks = self._run(_list())
            items: list[CompletionItem] = []
            for notebook in notebooks:
                notebook_id = getattr(notebook, "id", "")
                if isinstance(notebook_id, str) and notebook_id.startswith(incomplete):
                    title = getattr(notebook, "title", "") or ""
                    items.append(CompletionItem(notebook_id, help=title))
                    if len(items) >= self._row_limit:
                        break
            return items
        except Exception:
            return []

    def complete_sources(self, ctx: Any, incomplete: str) -> list[CompletionItem]:
        """Complete source ids for the resolved notebook."""
        try:
            notebook_id = self.resolve_notebook(ctx)
            if not notebook_id:
                return []
            auth = self._load_auth(ctx)

            async def _list() -> Any:
                async with self._make_client(auth) as client:
                    return await client.sources.list(notebook_id)

            sources = self._run(_list())
            items: list[CompletionItem] = []
            for source in sources:
                source_id = getattr(source, "id", None) or getattr(source, "source_id", "")
                if source_id and source_id.startswith(incomplete):
                    title = getattr(source, "title", "") or ""
                    items.append(CompletionItem(source_id, help=title))
                    if len(items) >= self._row_limit:
                        break
            return items
        except Exception:
            return []

    def complete_artifacts(self, ctx: Any, incomplete: str) -> list[CompletionItem]:
        """Complete artifact ids for the resolved notebook."""
        try:
            notebook_id = self.resolve_notebook(ctx)
            if not notebook_id:
                return []
            auth = self._load_auth(ctx)

            async def _list() -> Any:
                async with self._make_client(auth) as client:
                    return await client.artifacts.list(notebook_id)

            artifacts = self._run(_list())
            items: list[CompletionItem] = []
            for artifact in artifacts:
                artifact_id = getattr(artifact, "id", None) or getattr(artifact, "artifact_id", "")
                if artifact_id and artifact_id.startswith(incomplete):
                    title = getattr(artifact, "title", "") or getattr(artifact, "name", "") or ""
                    items.append(CompletionItem(artifact_id, help=title))
                    if len(items) >= self._row_limit:
                        break
            return items
        except Exception:
            return []

    def resolve_notebook(self, ctx: Any) -> str | None:
        """Resolve the notebook id usable for sub-resource completion."""
        try:
            if self._notebook_resolver is not None:
                return self._notebook_resolver(ctx)
            return self._resolve_notebook_from_context(ctx)
        except Exception:
            return None

    def _resolve_notebook_from_context(self, ctx: Any) -> str | None:
        notebook_id = _notebook_id_from_context(ctx)
        if notebook_id:
            return notebook_id

        env_val = os.environ.get("NOTEBOOKLM_NOTEBOOK", "").strip()
        if env_val:
            return env_val

        return self._load_current_notebook()

    def _load_auth(self, ctx: Any) -> Any:
        if self._auth_loader is not None:
            return self._auth_loader(ctx)
        from . import helpers

        return helpers.get_auth_tokens(ctx)

    def _load_current_notebook(self) -> str | None:
        try:
            if self._current_notebook_loader is not None:
                return self._current_notebook_loader()
            from . import helpers

            return helpers.get_current_notebook()
        except Exception:
            return None

    def _make_client(self, auth: Any) -> Any:
        if self._client_factory is not None:
            return self._client_factory(auth)
        from ..client import NotebookLMClient

        return NotebookLMClient(auth)

    def _run(self, awaitable: Awaitable[Any]) -> Any:
        try:
            if self._async_runner is not None:
                return self._async_runner(awaitable)
            from . import helpers

            return helpers.run_async(awaitable)
        except Exception:
            _close_awaitable(awaitable)
            raise


def complete_notebooks(ctx: Any, incomplete: str) -> list[CompletionItem]:
    """Complete notebook ids using the default provider."""
    return CompletionProvider().complete_notebooks(ctx, incomplete)


def resolve_notebook(ctx: Any) -> str | None:
    """Resolve the notebook id using the default provider."""
    return CompletionProvider().resolve_notebook(ctx)


def complete_sources(
    ctx: Any,
    incomplete: str,
    *,
    notebook_resolver: _NotebookResolver | None = None,
) -> list[CompletionItem]:
    """Complete source ids using the default provider."""
    return CompletionProvider(notebook_resolver=notebook_resolver).complete_sources(ctx, incomplete)


def complete_artifacts(
    ctx: Any,
    incomplete: str,
    *,
    notebook_resolver: _NotebookResolver | None = None,
) -> list[CompletionItem]:
    """Complete artifact ids using the default provider."""
    return CompletionProvider(notebook_resolver=notebook_resolver).complete_artifacts(
        ctx,
        incomplete,
    )
