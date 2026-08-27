"""Private sharing type implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import ShareAccess, SharePermission, ShareViewLevel


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
        from .._web.rows.sharing import decode_shared_user

        return decode_shared_user(cls, data)


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
        from .._web.rows.sharing import decode_share_status

        return decode_share_status(cls, data, notebook_id)
