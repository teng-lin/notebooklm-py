"""Backend-neutral notebook operations API."""

import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote

from ._env import get_base_url
from ._idempotency import idempotent_create, unresolved_commit_error
from ._idempotency import mark_unconfirmed as _unconfirmed
from ._notebook_metadata import NotebookMetadataService, NotebookSourceLister
from ._runtime.call_supervisor import OperationLease
from .exceptions import (
    AuthError,
    NetworkError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from .types import (
    NextStepSuggestion,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PromptSuggestion,
)

logger = logging.getLogger(__name__)


ShareUrlBuilder = Callable[[str, str | None], str]
_CopyFailureChain = Literal["explicit", "suppress"]


def build_share_url(base_url: str, notebook_id: str, artifact_id: str | None = None) -> str:
    """Build the legacy NotebookLM notebook or artifact share URL.

    Both IDs are percent-encoded with ``safe=""`` so reserved characters
    (``/``, ``?``, ``&``, ``#``) and whitespace cannot escape the path /
    query position and rewrite the URL into another endpoint.
    """
    notebook_url = f"{base_url}/notebook/{quote(notebook_id, safe='')}"
    if artifact_id:
        return f"{notebook_url}?artifactId={quote(artifact_id, safe='')}"
    return notebook_url


def _build_default_share_url(notebook_id: str, artifact_id: str | None = None) -> str:
    """Build a share URL from the base URL resolved at call time."""
    return build_share_url(get_base_url(), notebook_id, artifact_id)


def _describe_notebooks(notebooks: list[Notebook]) -> str:
    """Render matched notebooks as ``id (title)`` for an ambiguity message.

    Mirrors ``_web/sources/add.py::_describe_sources``. The ambiguity raises tell the
    caller to go and check their notebook list; naming the exact rows saves them
    diffing that list by eye against a title that, by definition, is not unique.
    """
    return ", ".join(f"{notebook.id} ({notebook.title!r})" for notebook in notebooks)


class NotebooksAPI(ABC):
    """Backend-neutral operations on NotebookLM notebooks.

    The public namespace class owns transport-independent orchestration. Concrete
    backends implement each one-call operation and the create/copy send hooks.
    """

    _create_method_id: str
    _copy_method_id: str
    _copy_failure_chain: _CopyFailureChain

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(
        self,
        sources_api: NotebookSourceLister,
        *,
        metadata_service: NotebookMetadataService | None = None,
        share_url_builder: ShareUrlBuilder = _build_default_share_url,
    ) -> None:
        """Initialize transport-neutral notebook state.

        Args:
            sources_api: Source lister for cross-API metadata composition.
            metadata_service: Optional explicit metadata service for tests or advanced wiring.
            share_url_builder: Synchronous notebook/artifact share URL builder.
        """
        self._sources = sources_api
        self._metadata_service = metadata_service or NotebookMetadataService(
            # Keep notebook lookup late-bound so tests and advanced callers that
            # replace ``api.get`` after construction still affect get_metadata().
            get_notebook=lambda notebook_id: self.get(notebook_id),
            source_lister=self._sources,
        )
        self._share_url_builder = share_url_builder
        # CREATE_NOTEBOOK/COPY_PROJECT may volunteer a chat-session id that
        # ChatAPI consumes once before falling back to a backend read. Producers
        # and any eviction/cleanup policy stay backend-owned.
        self._created_chat_session_ids: dict[str, str] = {}

    def _take_created_chat_session_id(self, notebook_id: str) -> str | None:
        """Consume a create/copy response's volunteered chat-session id once."""
        return self._created_chat_session_ids.pop(notebook_id, None)

    @abstractmethod
    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        """Return all source IDs in a notebook."""

    @abstractmethod
    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> builtins.list[PromptSuggestion]:
        """Return AI-suggested prompts for a notebook."""

    @abstractmethod
    async def suggest_next_steps(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
    ) -> builtins.list[NextStepSuggestion]:
        """Return grounded follow-up questions for a notebook (``NextStepSuggestions``).

        The standalone form of the ``next_steps`` block a chat answer carries.
        ``source_ids=None`` scopes to every source in the notebook.
        """

    @abstractmethod
    async def list(self) -> builtins.list[Notebook]:
        """List notebooks in backend-defined recent-first order."""

    async def create(self, title: str) -> Notebook:
        """Create a new notebook.

        Args:
            title: The title for the new notebook.

        Returns:
            The created Notebook object.

        Idempotency:
            Wraps the underlying create operation in a probe-then-retry loop. On
            a transient transport failure (5xx / 429 / network), the wrapper lists
            notebooks and checks whether a new notebook with the requested title
            appeared since the call started. If exactly one match is found, that
            notebook is returned without re-issuing the create. If zero matches,
            the create is retried. If more than one matches, the wrapper raises an
            :class:`RPCError` because the situation is ambiguous.
        """
        async with self._operation_scope("notebooks.create"):
            return await self._create_with_probe(title)

    async def _create_with_probe(self, title: str) -> Notebook:
        logger.debug("Creating notebook: %s", title)

        # Capture the baseline notebook IDs *before* the create so the
        # probe can distinguish a notebook that landed during this
        # call from a pre-existing notebook with the same title.
        # Titles are NOT unique in NotebookLM, so an unfiltered match
        # could hand back a notebook that predates the call — and every
        # subsequent ``sources.add_*`` / ``chat.ask`` in that session
        # would then target the wrong notebook.
        #
        # ``None`` is the "baseline unavailable" sentinel, matching the
        # three sibling PROBE_THEN_CREATE paths (``add_url``,
        # ``add_drive``, ``register_file_source``). The probe below then
        # refuses to guess and raises on any match instead. This
        # deliberately trades a possible duplicate create — loud, visible
        # in the notebook list, diagnosable — for the silent wrong-identity
        # outcome the previous empty-set fallback produced (#2232).
        baseline_ids: set[str] | None
        # Retained so the ambiguity raise below can name what went wrong: the
        # caller sees "baseline unavailable" long after this line ran, and
        # without the cause there is nothing in the process that can explain it.
        baseline_error: Exception | None = None
        try:
            baseline_ids = {nb.id for nb in await self.list()}
        except Exception as exc:
            # WARNING, not DEBUG (#2220 parity with the source paths, #2204):
            # the ``notebooklm`` logger defaults to WARNING, so a DEBUG record
            # here is discarded before any handler sees it and the call silently
            # runs with its idempotency probe disabled.
            #
            # Swallowing is still right at *baseline* time — nothing has been
            # written yet, so degrading is safe and failing here would break
            # creates that would otherwise have succeeded. The probe below runs
            # after a create that may already have committed and therefore has
            # no such freedom; that asymmetry is the whole shape of #2220.
            baseline_error = exc
            logger.warning(
                "create: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a notebook this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                type(exc).__name__,
                exc_info=True,
            )
            baseline_ids = None

        async def _create() -> Notebook:
            return await self._send_create(title)

        async def _probe() -> Notebook | None:
            # Transport- and auth-level errors during the probe MUST
            # propagate: the original create may have committed
            # server-side and we have no way to confirm. Silently
            # returning None would let ``idempotent_create`` re-issue the
            # create on the next attempt and duplicate the notebook.
            # Surfacing the transport error keeps the caller in control —
            # they can decide whether to re-probe later (e.g. once
            # connectivity recovers) before retrying the create.
            #
            # Other exception types (decoding errors, unexpected RPC
            # failures, programming bugs) propagate too, as of #2220. They
            # signal that the probe path itself is broken — which is exactly
            # when its protection matters most, because "broken probe" is
            # indistinguishable from "the create did not land". The old
            # best-effort contract returned ``None`` there and let the create
            # be re-issued on no evidence.
            try:
                current = await self.list()
            except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
                # Transport- and auth-level probe failures must propagate.
                # Silently returning None here lets ``idempotent_create``
                # re-issue the create on top of a broken probe, which is
                # exactly the duplicate-resource bug we are guarding against.
                logger.warning(
                    "create: probe list() failed with transport/auth error; "
                    "propagating so the caller can avoid a duplicate-resource retry"
                )
                # Mark it UNCONFIRMED before it goes (#2220 review): the create
                # may already have committed and this probe could not say, which
                # is the same predicament as the decode branch below. Without the
                # marker a ServerError/RateLimitError here classifies as the
                # *retriable* SERVER/RATE_LIMITED with the hint "retry after a
                # short delay" — and the caller retries the ADD, not the probe.
                # The underlying type is left intact, so "re-authenticate" /
                # "connectivity" remain readable in the message.
                _unconfirmed(exc)
                raise
            except Exception as exc:
                # Propagate, do not retry (#2220). ``notebooks.create`` is the
                # fourth instance of the probe-then-create pattern the issue
                # names three of; leaving it swallowing would split the very
                # uniformity that argument rests on. ``RPCError`` matches what
                # the ambiguity branch below already raises, so no call site's
                # ``except`` clause changes meaning.
                logger.warning(
                    "create: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(exc).__name__,
                    exc_info=True,
                )
                raise unresolved_commit_error(
                    self._create_method_id,
                    "the notebook create",
                    RPCError(
                        # Action first — the MCP/REST surfaces truncate messages at
                        # 300 characters, which cut the closing instruction off.
                        "UNRESOLVED — do not blindly retry; check your notebook list "
                        f"first. Cannot confirm notebook with title {title!r}: the "
                        "create failed at the transport level and may or may not have "
                        "committed, and the idempotency probe that would settle it "
                        f"failed too ({type(exc).__name__}). No FURTHER attempt was made, "
                        "because retrying on an unanswered probe is how duplicates "
                        "happen — but an earlier attempt in this call may also have "
                        "committed.",
                        method_id=self._create_method_id,
                    ),
                    preserve_exception=True,
                ) from exc
            matches = [nb for nb in current if nb.title == title]
            if baseline_ids is not None:
                matches = [nb for nb in matches if nb.id not in baseline_ids]
            elif matches:
                # Without a baseline a match may predate this create — see the
                # ``baseline_ids`` comment for the failure mode this guards.
                # Both halves of the ambiguity are worth stating: the match may
                # predate the create, or it may BE the create, in which case it
                # landed and the caller will otherwise never learn its id.
                #
                # Deliberately NOT ``raise ... from baseline_error``. Setting
                # ``__cause__`` (by ``from`` or by hand) makes the traceback
                # print the cause *instead of* ``__context__`` — and here
                # ``__context__`` is the create's transport failure, the half
                # ``idempotent_create`` promises stays visible ("the traceback
                # shows both halves", ``_idempotency.py``). The baseline failure
                # is named by type in the message instead, which is all the two
                # sibling paths without a ``cause=`` field surface either.
                raise _unconfirmed(
                    RPCError(
                        # Action first — the MCP/REST surfaces truncate messages
                        # at 300 characters, and a realistic title plus one
                        # ``id (title)`` row runs past that, cutting the closing
                        # instruction off. Same reasoning as the probe-failure
                        # raise above.
                        f"Cannot disambiguate notebook with title {title!r} — check your "
                        "notebook list before retrying: the pre-create baseline snapshot "
                        f"failed ({type(baseline_error).__name__}), so "
                        f"{_describe_notebooks(matches)} may either predate this create "
                        "or be the notebook it just created.",
                        method_id=self._create_method_id,
                    )
                )
            if len(matches) == 1:
                # ``matches`` is a list of typed ``Notebook`` objects (NOT a raw
                # RPC payload) — tuple unpacking reads the single match
                # without the ``name[int]`` shape that the positional-decode gate
                # (rightly) flags only for genuine payload descents.
                (match,) = matches  # exactly one (len==1 guard); unpack avoids name[int]
                return match
            if len(matches) > 1:
                # Ambiguous: more than one new notebook with this title
                # appeared during the call. We cannot safely pick one;
                # surface the situation so the caller can resolve it.
                raise _unconfirmed(
                    RPCError(
                        f"Cannot disambiguate notebook with title {title!r}: "
                        f"probe found {len(matches)} new notebooks with this title "
                        "after a transport failure. Resolve manually before retrying.",
                        method_id=self._create_method_id,
                    )
                )
            return None

        result = await idempotent_create(
            _create,
            _probe,
            label=f"notebooks.create[{title!r}]",
        )
        return result.value

    @abstractmethod
    async def _send_create(self, title: str) -> Notebook:
        """Send one backend create operation and decode the notebook."""

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        """Copy a notebook, including its sources and Studio artifacts.

        ``CopyProject`` has no caller-provided idempotency token. Internal
        transport retries are disabled so a lost response cannot create a
        second copy. If the call fails after the server commits, callers must
        disambiguate the intended copy from their notebook list.
        """
        if not notebook_id:
            raise ValidationError("notebook_id must not be empty")
        if not title or not title.strip():
            raise ValidationError("title must not be empty")

        try:
            return await self._send_copy(notebook_id, title)
        except (NetworkError, RateLimitError, ServerError) as exc:
            rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
            failure = unresolved_commit_error(
                self._copy_method_id,
                "CopyProject",
                RPCError(
                    "UNRESOLVED — CopyProject may have committed before its response was "
                    "lost. Do not blindly retry; list notebooks and resolve copies "
                    "manually first.",
                    method_id=self._copy_method_id,
                    rpc_code=rpc_code,
                ),
                preserve_exception=True,
            )
            if self._copy_failure_chain == "explicit":
                raise failure from exc
            raise failure from None

    @abstractmethod
    async def _send_copy(self, notebook_id: str, title: str) -> Notebook:
        """Send one backend copy operation and decode the new notebook."""

    @abstractmethod
    async def get(self, notebook_id: str) -> Notebook:
        """Get notebook details or raise ``NotebookNotFoundError``."""

    async def get_or_none(self, notebook_id: str) -> Notebook | None:
        """Get notebook details, returning ``None`` when it does not exist."""
        try:
            return await self.get(notebook_id)
        except NotebookNotFoundError:
            return None

    @abstractmethod
    async def delete(self, notebook_id: str) -> None:
        """Delete a notebook."""

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook and return the refreshed notebook."""
        return await self.update(notebook_id, title=new_title)

    async def set_emoji(self, notebook_id: str, emoji: str) -> Notebook:
        """Set a notebook's display emoji and return the refreshed notebook."""
        return await self.update(notebook_id, emoji=emoji)

    @abstractmethod
    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        """Update a notebook's title and/or emoji in one mutation."""

    @abstractmethod
    async def get_summary(self, notebook_id: str) -> str:
        """Get raw summary text for a notebook."""

    @abstractmethod
    async def get_description(self, notebook_id: str) -> NotebookDescription:
        """Get an AI-generated summary and suggested topics for a notebook."""

    @abstractmethod
    async def remove_from_recent(self, notebook_id: str) -> None:
        """Remove a notebook from the recently viewed list."""

    @abstractmethod
    async def get_raw(self, notebook_id: str) -> Any:
        """Get undecoded notebook data from the backend."""

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Get a share URL without toggling server-side sharing."""
        return self._share_url_builder(notebook_id, artifact_id)

    async def get_metadata(self, notebook_id: str) -> NotebookMetadata:
        """Get notebook details composed with simplified source metadata."""
        return await self._metadata_service.get_metadata(notebook_id)


__all__ = ["NotebooksAPI"]
