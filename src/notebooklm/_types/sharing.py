"""Private sharing type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import quote

from .._env import get_base_url
from ..rpc import RPCMethod, safe_index
from ..rpc.types import ShareAccess, SharePermission, ShareViewLevel

logger = logging.getLogger(__name__)

# The RPC that produces every sharing payload parsed in this module. Passed to
# ``safe_index`` so a shape-drift ``UnknownRPCMethodError`` points at the right
# method. Every ``safe_index`` call below sits AFTER a length/isinstance/truthy
# guard that already proves the slot is present, so the read stays byte-for-byte
# as soft as the legacy ``data[i]`` it replaced — ``safe_index`` never raises on
# any input the old guarded code accepted; it centralises the descent and only
# fires if the guard's invariant is somehow violated (genuine drift).
_SHARE_METHOD_ID = RPCMethod.GET_SHARE_STATUS.value

#: ``(field_name, type_name)`` pairs already reported by
#: :meth:`ShareStatus._warn_if_malformed`, so a polled notebook does not re-emit
#: the same drift line on every decode.
#:
#: Keyed on the value's *type* rather than the value so the set is **bounded by
#: construction** — two field names times the handful of JSON-decodable types.
#: Keying on the value (or its ``repr``) would grow without limit in any
#: long-lived process, which is a memory leak on a decode path that runs on
#: every share-status read.
_warned_malformed_share_slots: set[tuple[str, str]] = set()

#: Cap on the rendered drift value used as a warn-once key and logged. Long
#: enough to identify any plausible scalar verbatim, short enough that a
#: pathological payload cannot retain an unbounded string.
_MAX_DRIFT_REPR_LEN: int = 120


@dataclass
class SharedUser:
    """A user the notebook is shared with."""

    email: str
    permission: SharePermission
    display_name: str | None = None
    avatar_url: str | None = None

    @classmethod
    def from_api_response(cls, data: list[Any]) -> SharedUser:
        """Parse from GET_SHARE_STATUS user entry.

        Entry format: [email, permission, [], [name, avatar]]
        """
        # ``data[0]`` is the user email. An absent / ``None`` slot keeps the
        # historical silent ``""``-degrade (this factory parses entries out of
        # the whole shared-user list, so raising would abort sibling entries).
        # A *present-but-malformed* slot (non-str, non-None) also degrades to
        # ``""`` for the same reason, but now logs a WARNING instead of
        # silently fabricating an empty email (#1485 absence-vs-malformed
        # policy).
        email = ""
        if data:
            # ``data`` is non-empty here, so slot 0 is present: ``safe_index``
            # can never raise — it stays the soft read it replaces.
            raw_email = safe_index(
                data, 0, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
            )
            if isinstance(raw_email, str):
                email = raw_email
            elif raw_email is not None:
                logger.warning(
                    "Share user email slot malformed — fabricating empty email "
                    "(expected str at entry[0], got %s; entry=%s)",
                    type(raw_email).__name__,
                    reprlib.repr(data),
                )
        # ``len(data) > 1`` proves slot 1 is present before descending.
        perm_value = (
            safe_index(data, 1, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response")
            if len(data) > 1
            else 3
        )
        try:
            permission = SharePermission(perm_value)
        except (TypeError, ValueError):
            permission = SharePermission.VIEWER

        display_name = None
        avatar_url = None
        # ``len(data) > 3`` proves slot 3 is present; ``isinstance(..., list)``
        # is checked on the same descended value so the original guard shape is
        # preserved.
        user_info_block = (
            safe_index(data, 3, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response")
            if len(data) > 3
            else None
        )
        if isinstance(user_info_block, list):
            user_info = user_info_block
            # ``if user_info`` / ``len(user_info) > 1`` guard each slot below.
            display_name = (
                safe_index(
                    user_info, 0, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
                )
                if user_info
                else None
            )
            avatar_url = (
                safe_index(
                    user_info, 1, method_id=_SHARE_METHOD_ID, source="SharedUser.from_api_response"
                )
                if len(user_info) > 1
                else None
            )

        return cls(
            email=email,
            permission=permission,
            display_name=display_name,
            avatar_url=avatar_url,
        )


@dataclass
class ShareStatus:
    """Current sharing configuration for a notebook.

    Wire shape (``GET_SHARE_STATUS`` → mobile ``GetProjectDetailsResponse``),
    live-observed identically on 10/10 notebooks in a 2026-08 sweep::

        [
          [[email, permission, [], [name, avatar]], ...],  # [0] shared users
          null | [is_publicly_readable, is_discoverable],  # [1] publicSettings
          1000,                                            # [2] maxIndividualsShareLimit
          true,                                            # [3] isPublicSharingAllowed
          null,                                            # [4] unread, always null live
          null,                                            # [5] unread, always null live
          [3, true, true],                                 # [6] tag 7 — UNNAMED
          false,                                           # [7] tag 8 — UNNAMED
        ]

    Slots ``[6]`` / ``[7]`` (proto tags 7 and 8) are populated on every live row
    but are **deliberately not surfaced**: ``GetProjectDetailsResponse`` in the
    recovered mobile schema declares only tags 2, 3 and 4, so nothing names
    them. Guessing a name here is precisely the defect class
    ``tests/_guardrails/_wire_contract.py`` exists to prevent, so they are
    recorded there in ``UNREAD_SHARE_STATUS_SLOTS`` rather than exposed under an
    invented name. That record is enforced, not merely documentary:
    ``test_unread_share_status_slots_stay_undecoded`` fails if any constant in
    this module starts reading slot 6 or 7 (#2130).
    """

    #: ``[0]`` — the shared-user rows. Not declared in the mobile
    #: ``GetProjectDetailsResponse`` (a web-only slot), so it is pinned rather
    #: than mapped in the wire contract.
    _USERS_POS: ClassVar[int] = 0
    #: ``[1]`` — ``publicSettings`` (``ProjectPublicSettings``, tag 2).
    _PUBLIC_BLOCK_POS: ClassVar[int] = 1
    #: ``[1][0]`` — ``ProjectPublicSettings.isPubliclyReadable`` (tag 1).
    _IS_PUBLIC_INNER_POS: ClassVar[int] = 0
    #: ``[2]`` — ``maxIndividualsShareLimit`` (tag 3).
    _MAX_SHARE_LIMIT_POS: ClassVar[int] = 2
    #: ``[3]`` — ``isPublicSharingAllowed`` (tag 4).
    _PUBLIC_SHARING_ALLOWED_POS: ClassVar[int] = 3

    notebook_id: str
    is_public: bool
    access: ShareAccess
    view_level: ShareViewLevel
    shared_users: list[SharedUser] = field(default_factory=list)
    share_url: str | None = None
    #: ``maxIndividualsShareLimit`` — the per-notebook collaborator cap the
    #: backend enforces (live: ``1000`` on every notebook observed). ``None``
    #: when the response omits the slot or carries a non-integer there, i.e.
    #: "the backend stated no cap", never a fabricated default: a wrong number
    #: here would be worse than no number, because a bulk-share caller would
    #: budget against it (#2130).
    max_individuals_share_limit: int | None = None
    #: ``isPublicSharingAllowed`` — the tenant/policy gate on making this
    #: notebook public (live: ``True`` on every notebook observed).
    #:
    #: **Tri-state on purpose.** ``None`` means the response made no claim; it
    #: is NOT collapsed into ``False``, because "the backend did not say" and
    #: "the backend said no" have opposite consequences for a caller deciding
    #: whether to attempt a public share. Callers must test
    #: ``is_public_sharing_allowed is False`` for the deny case rather than
    #: ``not is_public_sharing_allowed``, which also catches the unknown — or
    #: read :attr:`is_public_sharing_denied`, which encodes that distinction.
    is_public_sharing_allowed: bool | None = None

    @property
    def is_public_sharing_denied(self) -> bool:
        """Whether the backend **explicitly** refused public sharing.

        ``True`` only for a literal ``False`` on the wire. ``False`` therefore
        means "no denial was reported" — for an explicit allow and for silence
        alike — **not** "public sharing is confirmed available".

        This exists because the safe reading of the tri-state is not the
        idiomatic one: ``not status.is_public_sharing_allowed`` is ``True`` for
        the *unknown* case too, so the obvious spelling reports a denial the
        backend never made. Reaching for this property instead of re-deriving
        the comparison is what keeps that bug out of each new caller — the same
        role :attr:`notebooklm.Source.is_drive_degraded` plays for the Drive
        tri-state.
        """
        return self.is_public_sharing_allowed is False

    @classmethod
    def from_api_response(cls, data: list[Any], notebook_id: str) -> ShareStatus:
        """Parse from GET_SHARE_STATUS response.

        Response format: ``[user_entries, public_block_or_null,
        max_individuals_share_limit, is_public_sharing_allowed, ...]``, where
        ``user_entries`` is a list of ``[email, permission, [], [name, avatar]]``
        rows. See the class docstring for the full observed shape, including the
        two populated-but-unnamed trailing slots.
        """
        # Parse users from [0]. ``if data`` proves slot 0 is present before the
        # ``safe_index`` descent, so the read stays as soft as the legacy
        # ``data[0]`` it replaces.
        users = []
        user_entries = (
            safe_index(
                data,
                cls._USERS_POS,
                method_id=_SHARE_METHOD_ID,
                source="ShareStatus.from_api_response",
            )
            if data
            else None
        )
        if isinstance(user_entries, list):
            for user_data in user_entries:
                if isinstance(user_data, list):
                    users.append(SharedUser.from_api_response(user_data))

        # Parse is_public from [1]. Bind the ``[is_public]`` block to a local so
        # the flag read is a single-level index rather than a chained
        # ``data[1][0]`` descent; an absent/empty block legitimately means
        # "not public".
        is_public = False
        # ``len(data) > 1`` proves slot 1 is present before the descent; the
        # ``isinstance(..., list)`` check runs on the descended value so the
        # original "absent/empty block means not-public" contract is preserved.
        public_slot = (
            safe_index(
                data,
                cls._PUBLIC_BLOCK_POS,
                method_id=_SHARE_METHOD_ID,
                source="ShareStatus.from_api_response",
            )
            if len(data) > cls._PUBLIC_BLOCK_POS
            else None
        )
        public_block = public_slot if isinstance(public_slot, list) else None
        if public_block:
            # ``if public_block`` proves it is a non-empty list, so slot 0 exists.
            is_public = bool(
                safe_index(
                    public_block,
                    cls._IS_PUBLIC_INNER_POS,
                    method_id=_SHARE_METHOD_ID,
                    source="ShareStatus.from_api_response",
                )
            )

        # ``maxIndividualsShareLimit`` (tag 3) and ``isPublicSharingAllowed``
        # (tag 4). Both fail closed to ``None`` — "the backend made no claim" —
        # rather than to a fabricated 0/False, so an absent slot can never be
        # mistaken for a real cap of zero or a real policy denial.
        limit_slot = cls._scalar_at(data, cls._MAX_SHARE_LIMIT_POS)
        # ``bool`` is an ``int`` subclass, so a JSON ``true`` here would silently
        # decode as a cap of 1; that is drift, not a limit, and must not parse.
        max_individuals_share_limit: int | None = None
        if isinstance(limit_slot, int) and not isinstance(limit_slot, bool):
            max_individuals_share_limit = limit_slot
        else:
            cls._warn_if_malformed(limit_slot, "maxIndividualsShareLimit", "int")

        allowed_slot = cls._scalar_at(data, cls._PUBLIC_SHARING_ALLOWED_POS)
        # Strictly ``bool``: a truthy non-bool (e.g. a drifted int or string) is
        # not a policy answer, and coercing one would invent a gate verdict.
        is_public_sharing_allowed: bool | None = None
        if isinstance(allowed_slot, bool):
            is_public_sharing_allowed = allowed_slot
        else:
            cls._warn_if_malformed(allowed_slot, "isPublicSharingAllowed", "bool")

        access = ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED

        # view_level not in GET_SHARE_STATUS response - default to FULL_NOTEBOOK
        view_level = ShareViewLevel.FULL_NOTEBOOK

        # Construct share URL if public. Percent-encode the id with ``safe=""``
        # so reserved characters cannot escape the path position and rewrite
        # the URL into another endpoint (mirrors ``_sharing_manager.build_share_url``).
        share_url = (
            f"{get_base_url()}/notebook/{quote(notebook_id, safe='')}" if is_public else None
        )

        return cls(
            notebook_id=notebook_id,
            is_public=is_public,
            access=access,
            view_level=view_level,
            shared_users=users,
            share_url=share_url,
            max_individuals_share_limit=max_individuals_share_limit,
            is_public_sharing_allowed=is_public_sharing_allowed,
        )

    @staticmethod
    def _warn_if_malformed(value: Any, field_name: str, expected: str) -> None:
        """Log once-per-value when a slot is PRESENT but carries the wrong type.

        Absence and drift both decode to ``None``, which is the one place this
        tri-state is lossy: "the backend stated no cap" and "the backend stated
        something we could not read" become indistinguishable to a caller. That
        is an acceptable public contract — an ``int | None`` should not grow an
        ``UNKNOWN`` sentinel — but it must not also be *invisible*, because this
        decode is the only place the drift could ever be observed and shape
        drift is this client's #1 breakage class.

        So the value is dropped either way, and a present-but-wrong-typed slot
        additionally warns. Mirrors the ``#1485`` absence-vs-malformed policy
        already applied to the email slot in
        :meth:`SharedUser.from_api_response`, and the warn-once keying used by
        ``SourceRow.drive_status``. ``None`` is real absence and never warns.
        """
        if value is None:
            return
        # Keyed on the *type*, not the value. Keying on the value would let a
        # long-lived process (the REST server, an MCP session) accumulate one
        # cache entry per distinct malformed payload forever — an unbounded set
        # on a hot decode path. ``field_name`` ranges over two constants and
        # ``type(...).__name__`` over the handful of JSON-decodable types, so
        # this set is bounded by construction: no cap constant, no eviction
        # policy, nothing to tune.
        #
        # The trade-off is deliberate: two differently-valued drifts of the same
        # type warn once rather than twice. That loses nothing worth having —
        # the useful signal is "this slot started arriving as a str", not the
        # census of every str seen — and the first warning still logs a concrete
        # example.
        key = (field_name, type(value).__name__)
        if key in _warned_malformed_share_slots:
            return
        _warned_malformed_share_slots.add(key)
        rendered = repr(value)[:_MAX_DRIFT_REPR_LEN]
        logger.warning(
            "GET_SHARE_STATUS %s slot malformed — reporting 'no claim' (expected %s, got %s: %s)",
            field_name,
            expected,
            type(value).__name__,
            rendered,
        )

    @classmethod
    def _scalar_at(cls, data: list[Any], pos: int) -> Any:
        """The raw value at ``data[pos]``, or ``None`` when the slot is absent.

        The length guard runs BEFORE the :func:`safe_index` descent, matching the
        rest of this parser: short responses are a normal shape here (the pinned
        ``GET_SHARE_STATUS`` golden capture is only three elements long), so a
        missing trailing slot is absence, not drift, and must not **raise** —
        :func:`safe_index` treats an out-of-range index as drift and raises
        ``UnknownRPCMethodError``, so reaching it for a legitimately short
        response would turn a normal shape into an error.
        """
        if len(data) <= pos:
            return None
        return safe_index(
            data, pos, method_id=_SHARE_METHOD_ID, source="ShareStatus.from_api_response"
        )
