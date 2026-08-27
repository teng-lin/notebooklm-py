"""Private notebook type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .._deprecation import warn_deprecated
from .._row_adapters.notebooks import ProjectRow
from ..exceptions import UnknownRPCMethodError
from ..rpc import RPCMethod, safe_index
from .chat import ChatSettings
from .common import _datetime_from_timestamp
from .enums import ChatGoal, ChatResponseLength, SharePermission, share_permission_to_str
from .sources import SourceType

logger = logging.getLogger(__name__)

# ``Notebook.from_api_response`` decodes rows from BOTH ``LIST_NOTEBOOKS`` (each
# row in the list envelope) and ``GET_NOTEBOOK`` (the single ``nb_info`` row).
# The positional descents below route through ``safe_index`` purely for the
# shared schema-drift telemetry seam; every descent is *length-guarded first*
# so ``safe_index`` is only ever invoked on a slot the guard already proved
# present — it therefore cannot raise here, preserving the historical
# "short / malformed rows soft-degrade to a default" contract (the same
# length-guard-then-``safe_index`` style ``NoteRow`` uses). ``LIST_NOTEBOOKS``
# is used as the representative ``method_id`` for diagnostics since the list
# path is the primary producer; a drift diagnostic would still point at the
# notebook-row family.
_NOTEBOOK_METHOD_ID = RPCMethod.LIST_NOTEBOOKS.value


@dataclass
class SourceSummary:
    """Simplified source information for metadata export."""

    kind: SourceType
    title: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.kind.value,
            "title": self.title,
            "url": self.url,
        }


def _extract_notebook_sources_count(data: list[Any]) -> int:
    """Extract the embedded source count from a notebook API payload."""
    sources = (
        safe_index(data, 1, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.sources_count")
        if len(data) > 1
        else None
    )
    return len(sources) if isinstance(sources, list) else 0


#: Wire codes the ``userRole`` slot may legitimately carry, as plain ints —
#: comparing ints to ints rather than relying on ``SharePermission`` hashing as
#: its value, so the check cannot quietly start rejecting everything if the enum
#: ever stops mixing in ``int``. ``SharePermission`` also owns ``_REMOVE = 4``
#: (proto ``NOT_SHARED``), but that is a write-only sentinel for the
#: share-mutation call — a notebook row never reports it (a revoked account gets
#: ``PERMISSION_DENIED``, not role 4), so it is excluded here rather than
#: surfaced as a nonsensical role.
_NOTEBOOK_ROLES = frozenset(
    {SharePermission.OWNER.value, SharePermission.EDITOR.value, SharePermission.VIEWER.value}
)


def _role_from_wire(raw_role: Any, row: list[Any]) -> SharePermission | None:
    """Map a raw ``userRole`` slot to :class:`SharePermission`, or ``None``.

    ``None`` means "not stated by this row" — an absent slot, or a value this
    client does not recognize. Callers treat that as unknown rather than
    guessing a level. An unmapped *present* value logs a WARNING because it is
    the signature of protocol drift (#1485 absence-vs-malformed policy).
    """
    if raw_role is None:
        return None
    # ``bool`` is an ``int`` subclass, so ``SharePermission(True)`` would
    # silently yield ``OWNER``. Reject booleans explicitly: they are the shape
    # of the neighbouring has-sharing slot, i.e. exactly the drift we would
    # most want to notice.
    if isinstance(raw_role, int) and not isinstance(raw_role, bool):
        if raw_role in _NOTEBOOK_ROLES:
            return SharePermission(raw_role)
    logger.warning(
        "Notebook row userRole slot unmapped — reporting unknown role "
        "(expected 1/2/3 at data[5][0], got %r; row=%s)",
        raw_role,
        reprlib.repr(row),
    )
    return None


@dataclass(frozen=True)
class PremiumFeatureInfo:
    """Tier-dependent notebook capabilities returned with a ``Project``.

    Each member is tri-state. ``True``/``False`` is the server's explicit
    capability verdict; ``None`` means the response did not carry a usable
    boolean for that slot. In particular, callers should test
    ``can_view_analytics is True`` rather than treating absence as denial.
    """

    can_edit_advanced_settings: bool | None = None
    can_edit_guidebook_config: bool | None = None
    can_view_analytics: bool | None = None


@dataclass(frozen=True)
class ChatSession:
    """A chat session volunteered by the notebook ``Project`` response."""

    id: str


@dataclass
class Notebook:
    """Represents a NotebookLM notebook."""

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    #: ``True`` when :attr:`role` is :attr:`~notebooklm.types.SharePermission.OWNER`.
    #: Kept as a convenience derivation of :attr:`role`; it can no longer
    #: distinguish an editor from a viewer, so prefer :attr:`role` (#2125).
    is_owner: bool = True
    #: **Deprecated alias for :attr:`last_viewed_at`** (#2126) — the name is a
    #: lie the wire never told: the slot is ``lastViewedTime``, not a
    #: modification time. Kept in lock-step with :attr:`last_viewed_at` by
    #: :meth:`__post_init__` and :meth:`__setattr__`, and scheduled for removal
    #: in v1.0. See ``docs/deprecations.md``.
    #:
    #: This is a *docs-only* deprecation because ``modified_at`` is a dataclass
    #: **field**: a runtime ``DeprecationWarning`` on field access would also
    #: fire from ``repr()``, ``__eq__``, ``dataclasses.replace()`` and the
    #: MCP/REST ``to_jsonable`` serializer, flooding callers who never typed the
    #: old name. That is the same reasoning ``docs/deprecations.md`` records for
    #: ``AuthTokens.cookies`` / ``cookie_jar``. The sibling property
    #: :attr:`NotebookMetadata.modified_at` *is* a property, so it does warn.
    #:
    #: ``modified_at`` / ``role`` / ``last_viewed_at`` are appended at the END
    #: of the field list so positional construction stays unaffected (additive,
    #: default ``None``).
    modified_at: datetime | None = None
    #: The calling account's permission level on this notebook, decoded from
    #: ``ProjectMetadata.userRole``. ``None`` when the row omits the slot or
    #: carries an unmapped code.
    role: SharePermission | None = None
    #: When *this account* last opened the notebook — ``ProjectMetadata``
    #: ``lastViewedTime`` (tag 6, ``meta[5]``), tz-aware UTC.
    #:
    #: **This is not a modification time.** It does not move when a collaborator
    #: edits the notebook, and it *does* move when nobody edits anything — the
    #: backend writes it on every ``GET_NOTEBOOK``. It is also the sort key
    #: behind the NotebookLM web UI's "Recent" ordering
    #: (``ListRecentlyViewedProjects``), so every :meth:`NotebooksAPI.get` this
    #: client issues — including the ones it makes internally, and every
    #: source-readiness poll iteration — advances it and reshuffles that
    #: ordering. :meth:`NotebooksAPI.list` is a plain read of that ordering and
    #: does *not* bump it (probed: pinned across 15s of repeated
    #: ``LIST_NOTEBOOKS``). ``docs/python-api.md`` carries the full inventory of
    #: internal call paths that bump;
    #: :meth:`NotebooksAPI.remove_from_recent` is the only way to undo it.
    last_viewed_at: datetime | None = None
    #: Display emoji carried by ``Project.emoji``. ``None`` when the response
    #: omits it; an empty string is preserved as an explicit no-emoji value.
    emoji: str | None = None
    #: Tier-dependent feature availability volunteered by the backend.
    premium_features: PremiumFeatureInfo | None = None
    #: Chat sessions returned by ``CREATE_NOTEBOOK``. ``GET_NOTEBOOK`` omits
    #: them, so this is normally empty outside the create result.
    chat_sessions: list[ChatSession] = field(default_factory=list)
    #: Current chat persona/configuration decoded from ``Project`` tag 8.
    #: ``None`` only when the row was too short or malformed to make a claim.
    chat_settings: ChatSettings | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        """Maintain the two derived-field invariants on every assignment.

        Two fields on this dataclass are derivations of another field, and both
        must stay *fields* rather than becoming properties: the MCP/REST
        serializer emits ``dataclasses.fields`` only, so a property would
        silently vanish from every adapter's response — a breaking wire change.
        The invariants are therefore maintained on assignment instead.

        1. ``is_owner`` mirrors ``role is SharePermission.OWNER`` (#2125).
        2. ``modified_at`` mirrors ``last_viewed_at`` (#2126). ``modified_at``
           is the deprecated alias — the wire slot is ``lastViewedTime``, never
           a modification time — kept through v1.0 so existing callers keep
           their keyword, their attribute reads, and their serialized JSON key.

        Hooking ``__setattr__`` rather than ``__post_init__`` matters because
        this dataclass is mutated in place after construction (see the timestamp
        backfill in ``_app.notebooks._backfill_create_timestamps``, which writes
        ``last_viewed_at``); a construction-only hook would let both derived
        fields go stale the moment anyone assigned the field they derive from.

        A contradictory ``is_owner`` is *corrected*, not rejected. Raising is not
        actually available here: ``is_owner`` has a plain ``True`` default, so
        the ordinary ``Notebook(id=..., title=..., role=VIEWER)`` call is
        indistinguishable from an explicit ``is_owner=True``, and rejecting the
        contradiction would reject the most natural construction in the codebase.

        When ``role`` is ``None`` (the row stated no level) the caller's
        ``is_owner`` is left untouched, preserving the historical
        optimistic-``True`` soft-degrade.

        The timestamp pair mirrors in *both* directions so a legacy caller who
        writes ``nb.modified_at = X`` after construction still round-trips —
        otherwise ``to_jsonable`` (which emits both fields) and the CLI's
        ``notebook_viewed_keys`` (which reads only the canonical one) would give
        two different answers for the same object.

        Both directions are guarded on ``value is not None``, and for a
        mechanical reason: the generated ``__init__`` assigns fields in
        declaration order, and ``modified_at`` comes first (it has to, so
        positional construction keeps working). An unguarded mirror would let
        ``__init__``'s later ``last_viewed_at=None`` default wipe out a legacy
        ``Notebook(..., modified_at=X)`` argument. Restoring the canonical field
        from that legacy argument is handled once in :meth:`__post_init__`.

        The residual gap is assigning ``None`` after construction
        (``nb.last_viewed_at = None`` leaves ``modified_at`` stale). Clearing a
        decoded timestamp is not something this codebase or any plausible caller
        does, and closing it would require an "``__init__`` finished" flag whose
        cost outlives the alias it protects.
        """
        super().__setattr__(name, value)
        if name == "role" and value is not None:
            super().__setattr__("is_owner", value is SharePermission.OWNER)
        elif name == "last_viewed_at" and value is not None:
            super().__setattr__("modified_at", value)
        elif name == "modified_at" and value is not None:
            super().__setattr__("last_viewed_at", value)

    def __post_init__(self) -> None:
        """Reconcile the ``modified_at`` / ``last_viewed_at`` pair once, at birth.

        :meth:`__setattr__` keeps ``modified_at`` following ``last_viewed_at``
        thereafter; this handles the one direction it deliberately cannot, the
        legacy ``Notebook(..., modified_at=X)`` keyword, which ``__init__``
        assigns *before* ``last_viewed_at`` and whose value the canonical field's
        ``None`` default therefore overwrites. ``last_viewed_at`` is
        authoritative: when both names are supplied and disagree, it wins.

        Every constructed ``Notebook`` therefore leaves this method with the two
        names in agreement. One consequence worth naming:
        ``dataclasses.replace(nb, modified_at=X)`` on a notebook that already has
        a ``last_viewed_at`` is a no-op — pass ``last_viewed_at=X`` instead.
        """
        if self.last_viewed_at is None:
            self.last_viewed_at = self.modified_at
        self.modified_at = self.last_viewed_at

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore from a pickle, re-establishing the timestamp-alias invariant.

        Unpickling bypasses ``__init__``, ``__post_init__`` *and*
        ``__setattr__``: the default protocol writes ``__dict__`` directly. So a
        pickle written before #2126 restores with ``modified_at`` populated and
        no ``last_viewed_at`` key at all.

        That does **not** raise ``AttributeError``, which is the tempting
        assumption — ``last_viewed_at`` is a dataclass field with a ``None``
        class-level default, so the lookup falls through to the class and
        quietly yields ``None``. The silent outcome is the worse one: the object
        reports a populated ``modified_at`` next to a ``None``
        ``last_viewed_at``, precisely the "the two names disagree" state the
        alias runway promises cannot happen, and precisely the shape of bug this
        whole change exists to remove. Seed the canonical field so the promise
        holds for unpickled objects too.

        ``chat_sessions`` is different from the other additive fields: its
        ``default_factory`` creates no class-level fallback, so a pickle from
        before #2133 would raise ``AttributeError`` on access unless we seed an
        empty list here. ``role`` needs no equivalent: an old pickle restores
        it as ``None`` (unknown), and an unknown role deliberately leaves the
        pickled ``is_owner`` untouched — already the documented soft-degrade
        (#2125).
        """
        self.__dict__.update(state)
        self.__dict__.setdefault("chat_sessions", [])
        if state.get("last_viewed_at") is None and state.get("modified_at") is not None:
            self.__dict__["last_viewed_at"] = state["modified_at"]

    @classmethod
    def from_api_response(
        cls,
        data: list[Any],
        *,
        include_chat_settings: bool = False,
    ) -> Notebook:
        """Parse a notebook row from an API response.

        ``LIST_NOTEBOOKS`` does not project chat configuration: its slot 7 is
        ``null`` even when the notebook is configured. Only callers mapping a
        ``GET_NOTEBOOK`` row should set ``include_chat_settings=True``; there an
        explicit ``null`` truthfully means the default configuration.
        """
        project = ProjectRow(data)
        title_slot = (
            safe_index(data, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.title")
            if len(data) > 0
            else None
        )
        raw_title = title_slot if isinstance(title_slot, str) else ""
        title = raw_title.replace("thought\n", "").strip()
        sources_count = _extract_notebook_sources_count(data)
        # ``data[2]`` is the notebook id. A short row / ``None`` slot keeps
        # the historical silent ``""``-degrade — this factory parses rows out
        # of whole-list responses, so raising would abort sibling rows. A
        # *present-but-malformed* slot (non-str, non-None) still degrades to
        # ``""`` for the same reason, but now logs a WARNING: a silently
        # fabricated empty id is otherwise indistinguishable from a real row
        # (#1485 absence-vs-malformed policy).
        notebook_id = ""
        if len(data) > 2:
            raw_id = safe_index(data, 2, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.id")
            if isinstance(raw_id, str):
                notebook_id = raw_id
            elif raw_id is not None:
                logger.warning(
                    "Notebook row id slot malformed — fabricating empty id "
                    "(expected str at data[2], got %s; row=%s)",
                    type(raw_id).__name__,
                    reprlib.repr(data),
                )

        # ``data[5]`` is the metadata block; bind it once so the timestamp and
        # role descents read a single named local instead of re-chaining
        # ``data[5][...]`` (the legitimately-absent block defaults below). The
        # slot read goes through ``safe_index`` (length-guarded first, so it
        # cannot raise) and the result is only retained when it is a list.
        meta_slot = (
            safe_index(data, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.metadata")
            if len(data) > 5
            else None
        )
        meta = meta_slot if isinstance(meta_slot, list) else None

        # ``meta[8]`` (``data[5][8][0]``, proto tag 9) is the CREATION instant:
        # pinned across create / share / rename / read and byte-identical over
        # the mobile gRPC surface (#2126 audit), so it is the one timestamp on
        # this row that means what its name says.
        created_at = None
        if meta is not None and len(meta) > 8:
            created_ts = safe_index(
                meta, 8, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
            )
            if isinstance(created_ts, list) and len(created_ts) > 0:
                created_at = _datetime_from_timestamp(
                    safe_index(
                        created_ts, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
                    )
                )

        # ``meta[5]`` (``data[5][5][0]``, proto tag 6) is ``lastViewedTime`` —
        # NOT a modification time. It was surfaced as ``modified_at`` until
        # #2126, on the belief that it tracked edits; it tracks *this account's
        # reads*.
        #
        # Why the original probe got it wrong is worth recording, because the
        # obvious re-run reproduces the error: that probe edited the notebook and
        # then READ IT BACK to observe the slot, so every "edit" was confounded
        # with a read. Isolating the read is what settles it — the #2126 audit
        # (``docs/notes/web-rpc-vs-mobile-grpc-audit-2026-08-07.md`` §1.7) saw
        # three consecutive reads with no mutation of any kind advance the slot
        # (1786105463 -> 1786105467 -> 1786105471), and a single bare
        # ``GET_NOTEBOOK`` move the notebook to index 0 of
        # ``ListRecentlyViewedProjects``.
        #
        # Decoding it is free and read-only — the recency write happens
        # server-side on the ``GET_NOTEBOOK`` we already issued — so the fix is
        # the honest name plus the warning in the field docs, not a narrower
        # decode. See ``Notebook.last_viewed_at``.
        last_viewed_at = None
        if meta is not None and len(meta) > 5:
            viewed_ts = safe_index(
                meta, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.last_viewed_at"
            )
            if isinstance(viewed_ts, list) and len(viewed_ts) > 0:
                last_viewed_at = _datetime_from_timestamp(
                    safe_index(
                        viewed_ts,
                        0,
                        method_id=_NOTEBOOK_METHOD_ID,
                        source="Notebook.last_viewed_at",
                    )
                )

        # ``meta[0]`` (``data[5][0]``) is ``ProjectMetadata.userRole`` — the
        # CALLING account's permission level on this notebook (1 OWNER /
        # 2 WRITER / 3 READER, value-identical to ``SharePermission``).
        #
        # This used to be read from ``meta[1]`` instead, on the belief that the
        # slot was an owner flag. A two-account live probe (#2125) showed
        # ``meta[1]`` actually tracks "this notebook has ANY sharing at all":
        # it flipped ``False -> True`` the moment a collaborator was added to a
        # notebook the account owned, and back on revoke. So the old expression
        # evaluated to ``not (shared with anyone)`` and reported ``is_owner=False``
        # for every notebook the user owned *and had shared*. ``meta[0]`` stayed
        # pinned at ``1`` for the owner across every stage of that probe, and is
        # present on 100% of ``LIST_NOTEBOOKS`` *and* ``GET_NOTEBOOK`` rows, so
        # the correct read costs no extra RPC.
        role = None
        if meta is not None and len(meta) > 0:
            raw_role = safe_index(meta, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.role")
            role = _role_from_wire(raw_role, data)

        premium_features = None
        premium_flags = project.premium_feature_flags
        if premium_flags is not None:
            premium_features = PremiumFeatureInfo(*premium_flags)

        chat_settings = None
        if include_chat_settings:
            # Reuse the strict GET_NOTEBOOK decoder so the persona/config wire
            # position has one owner. The general Notebook factory is softer
            # than ChatAPI.get_settings because it also maps whole listings:
            # one malformed optional config must not discard sibling notebooks.
            from .._row_adapters.chat import unwrap_chat_settings

            try:
                settings_row = unwrap_chat_settings(data, source="Notebook.chat_settings")
                chat_settings = ChatSettings(
                    goal=ChatGoal(settings_row.goal_code),
                    response_length=ChatResponseLength(settings_row.response_length_code),
                    custom_prompt=settings_row.custom_prompt,
                )
            except (UnknownRPCMethodError, ValueError):
                logger.warning(
                    "Notebook row chat-settings slot could not be decoded — reporting unknown "
                    "settings (row=%s)",
                    reprlib.repr(data),
                )

        # ``is_owner`` is derived from ``role`` in ``__post_init__``. An unknown
        # / absent role leaves the field's default of ``True``: the
        # overwhelmingly common case is the caller's own notebook, and a short
        # or malformed row must soft-degrade rather than mislabel every entry.
        return cls(
            id=notebook_id,
            title=title,
            created_at=created_at,
            sources_count=sources_count,
            role=role,
            last_viewed_at=last_viewed_at,
            emoji=project.emoji,
            premium_features=premium_features,
            chat_sessions=[ChatSession(id=session_id) for session_id in project.chat_session_ids],
            chat_settings=chat_settings,
        )


@dataclass
class SuggestedTopic:
    """A suggested topic/question for the notebook."""

    question: str
    prompt: str


@dataclass(frozen=True)
class PromptSuggestion:
    """An AI-suggested question/prompt to ask a notebook.

    Returned by :meth:`NotebooksAPI.suggest_prompts` (the ``otmP3b`` /
    ``GeneratePromptSuggestions`` RPC). Each suggestion pairs a short,
    human-readable ``title`` with a ready-to-send multi-line ``prompt`` that can
    be passed straight to :meth:`ChatAPI.ask`.

    Attributes:
        title: Short label for the suggestion (e.g. ``"Professional Briefing"``).
        prompt: The full multi-line instruction string to ask the notebook.
    """

    title: str
    prompt: str


@dataclass
class NotebookDescription:
    """AI-generated description and suggested topics for a notebook."""

    summary: str
    suggested_topics: list[SuggestedTopic] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> NotebookDescription:
        """Parse from get_notebook_description() response."""
        topics = [
            SuggestedTopic(question=t.get("question", ""), prompt=t.get("prompt", ""))
            for t in data.get("suggested_topics", [])
        ]
        return cls(
            summary=data.get("summary", ""),
            suggested_topics=topics,
        )


@dataclass
class NotebookMetadata:
    """Combined notebook metadata with sources list."""

    notebook: Notebook
    sources: list[SourceSummary] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Get notebook ID."""
        return self.notebook.id

    @property
    def title(self) -> str:
        """Get notebook title."""
        return self.notebook.title

    @property
    def created_at(self) -> datetime | None:
        """Get creation timestamp."""
        return self.notebook.created_at

    @property
    def last_viewed_at(self) -> datetime | None:
        """When this account last opened the notebook (``lastViewedTime``).

        Not a modification time, and not read-only: the backend rewrites this
        slot on every read, and it is the sort key behind the web UI's "Recent"
        ordering. See :attr:`Notebook.last_viewed_at` for the full contract.
        """
        return self.notebook.last_viewed_at

    @property
    def modified_at(self) -> datetime | None:
        """Deprecated alias for :attr:`last_viewed_at` (#2126).

        The wire slot is ``lastViewedTime``, never a modification time. Unlike
        the same-named *field* on :class:`Notebook` — where a warning would leak
        through ``repr``/``__eq__``/serialization — this is a property, so it can
        warn at exactly the boundary ADR-0018 asks for: a caller who typed the
        old name.
        """
        warn_deprecated(
            "NotebookMetadata.modified_at is deprecated because the underlying wire "
            "field is lastViewedTime, not a modification time: it advances when this "
            "account merely reads the notebook and does not move when a collaborator "
            "edits it. Use NotebookMetadata.last_viewed_at.",
            removal="1.0",
        )
        return self.notebook.last_viewed_at

    @property
    def is_owner(self) -> bool:
        """Get owner status (``role is SharePermission.OWNER``)."""
        return self.notebook.is_owner

    @property
    def role(self) -> SharePermission | None:
        """Get the calling account's permission level on the notebook."""
        return self.notebook.role

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization.

        Emits ``last_viewed_at`` *and* the legacy ``modified_at`` key carrying
        the same value, so no consumer of the old key breaks during the v1.0
        runway. Both read :attr:`last_viewed_at`, never the deprecated property,
        so serializing never emits a ``DeprecationWarning``.
        """
        last_viewed = self.last_viewed_at.isoformat() if self.last_viewed_at else None
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_viewed_at": last_viewed,
            "modified_at": last_viewed,
            "is_owner": self.is_owner,
            "role": share_permission_to_str(self.role) if self.role is not None else None,
            "sources": [s.to_dict() for s in self.sources],
        }
