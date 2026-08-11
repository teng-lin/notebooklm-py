"""Transport-neutral profile-management business logic.

This is the Click-free core of ``cli/profile_cmd.py``: it owns the ``profile
list`` data-gathering (profile name → active/authenticated/account rows), the
``profile delete`` active/default guard decision, and the ``config.json``
default-profile retarget mutators used by ``switch`` / ``rename``. Every
transport adapter (the Click CLI today, the FastMCP server / future HTTP surface
tomorrow) drives this core and renders the typed rows / applies the decision
with its own surface + exit-code policy + confirmation prompts.

Boundary-imposed seams:

* **CLI path helpers stay injected; public account operations are late-bound.**
  :func:`gather_profile_list` retains its four-collaborator compatibility API,
  while the coarse profile-account operations resolve public auth facade
  functions inside each call. The CLI reads path helpers off its own module at
  invocation time so historical consumer patches keep landing.
* **The locked config writer stays in the adapter.** ``profile switch`` /
  ``rename`` persist through the CLI's ``_atomic_write_config`` (a patched-in
  test surface); this core only supplies the pure mutator closures it runs under
  the lock, never the I/O.
* **Rich rendering, ``click.confirm`` prompts, and exit policy stay in the CLI.**
  This core returns typed values / pure decisions only.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

#: Returns the list of known profile names.
ListProfilesFn = Callable[[], list[str]]
#: Returns the active (effective) profile name.
ResolveProfileFn = Callable[[], str]
#: Resolves a profile's ``storage_state.json`` path.
StoragePathFn = Callable[..., Path]
#: Reads a storage file's persisted account metadata (``{}`` when absent).
ReadAccountMetadataFn = Callable[[Path], dict[str, Any]]


class StoragePathForProfile(Protocol):
    """Resolve one profile's canonical storage path."""

    def __call__(self, profile: str | None = None) -> Path: ...


ProfileRepairStatus: TypeAlias = Literal["WRITTEN", "AMBIGUOUS", "ERROR"]


@dataclass(frozen=True)
class ProfileEntry:
    """One row of ``profile list`` — a profile's name + status.

    Mirrors the historical ``{name, active, authenticated, account}`` dict the
    CLI ``--json`` envelope emits, so the adapter rebuilds that envelope
    byte-for-byte from the typed fields.
    """

    name: str
    active: bool
    authenticated: bool
    account: str | None


@dataclass(frozen=True)
class ProfileAccountObservation:
    """Immutable projection of one profile's account metadata."""

    display_email: str | None
    nonempty_email: str | None
    metadata_valid: bool


@dataclass(frozen=True)
class ProfileTargetRequest:
    """Inputs for allocating a profile for one discovered account."""

    base_name: str
    account_email: str
    existing_profiles: frozenset[str]
    unavailable: frozenset[str]
    claimed: frozenset[str]
    update: bool


@dataclass(frozen=True)
class ProfileRepairRequest:
    """Inputs for Playwright account-metadata repair."""

    storage_path: Path
    page_html: str | None = None


@dataclass(frozen=True)
class ProfileRepairOutcome:
    """Transport-neutral account-metadata repair result."""

    status: ProfileRepairStatus
    email: str | None = None
    detail: str | None = None


def _read_profile_account_metadata(storage_path: Path) -> dict[str, Any]:
    """Resolve the public metadata reader at operation call time."""
    try:
        from ..auth import read_account_metadata

        try:
            return read_account_metadata(storage_path)
        finally:
            del read_account_metadata
    finally:
        del storage_path


def _repair_playwright_account(request: ProfileRepairRequest):
    """Build the public repair coroutine without adding an await traceback frame."""
    try:
        from ..auth import repair_account_metadata_from_playwright_storage

        try:
            return repair_account_metadata_from_playwright_storage(
                request.storage_path,
                page_html=request.page_html,
            )
        finally:
            del repair_account_metadata_from_playwright_storage
    finally:
        del request


def gather_profile_list(
    *,
    list_profiles: ListProfilesFn,
    resolve_profile: ResolveProfileFn,
    get_storage_path: StoragePathFn,
    read_account_metadata: ReadAccountMetadataFn,
) -> tuple[list[ProfileEntry], str]:
    """Gather the ``profile list`` rows + the active profile name.

    Returns ``(entries, active)``. ``entries`` is empty when no profiles exist
    (the adapter renders the "run login" hint). The collaborators are injected so
    this core never reaches into ``notebooklm.paths`` / ``notebooklm.auth`` and
    the CLI's ``patch.object`` seams keep landing.
    """
    profiles = list_profiles()
    active = resolve_profile()

    entries: list[ProfileEntry] = []
    for name in profiles:
        storage = get_storage_path(profile=name)
        account_metadata = read_account_metadata(storage)
        account_email = account_metadata.get("email")
        entries.append(
            ProfileEntry(
                name=name,
                active=name == active,
                authenticated=storage.exists(),
                account=account_email if isinstance(account_email, str) else None,
            )
        )
    return entries, active


def gather_profile_accounts(
    *,
    list_profiles: ListProfilesFn,
    resolve_profile: ResolveProfileFn,
    get_storage_path: StoragePathForProfile,
) -> tuple[list[ProfileEntry], str]:
    """Gather profile rows through the call-time public metadata adapter."""
    return gather_profile_list(
        list_profiles=list_profiles,
        resolve_profile=resolve_profile,
        get_storage_path=get_storage_path,
        read_account_metadata=_read_profile_account_metadata,
    )


def observe_profile_account(storage_path: Path) -> ProfileAccountObservation:
    """Read once and project the three historical account-metadata rules."""
    metadata: dict[str, Any] | None = None
    try:
        metadata = _read_profile_account_metadata(storage_path)
        raw_email = metadata.get("email")
        raw_authuser = metadata.get("authuser")
        display_email = raw_email if isinstance(raw_email, str) else None
        nonempty_email = raw_email if isinstance(raw_email, str) and raw_email else None
        metadata_valid = (
            type(raw_authuser) is int
            and raw_authuser >= 0
            and (raw_email is None or isinstance(raw_email, str) and bool(raw_email.strip()))
        )
        return ProfileAccountObservation(
            display_email=display_email,
            nonempty_email=nonempty_email,
            metadata_valid=metadata_valid,
        )
    finally:
        del metadata, storage_path


def profiles_by_account_email(
    profile_names: list[str], *, get_storage_path: StoragePathForProfile
) -> dict[str, str]:
    """Return first profile per case-folded non-empty account email."""
    profiles_by_email: dict[str, str] = {}
    for profile_name in profile_names:
        observation = observe_profile_account(get_storage_path(profile=profile_name))
        if observation.nonempty_email is not None:
            profiles_by_email.setdefault(observation.nonempty_email.casefold(), profile_name)
    return profiles_by_email


def profile_account_email(
    profile_name: str, *, get_storage_path: StoragePathForProfile
) -> str | None:
    """Return one profile's exact non-empty account email."""
    return observe_profile_account(get_storage_path(profile=profile_name)).nonempty_email


def _next_available_profile_name(base_name: str, unavailable: frozenset[str]) -> str:
    if base_name not in unavailable:
        return base_name
    suffix = 2
    while True:
        candidate = f"{base_name}-{suffix}"
        if candidate not in unavailable:
            return candidate
        suffix += 1


def resolve_all_accounts_target(
    request: ProfileTargetRequest, *, get_storage_path: StoragePathForProfile
) -> str:
    """Pick an overwrite-safe destination for one discovered account."""
    if (
        request.update
        and request.base_name in request.existing_profiles
        and request.base_name not in request.claimed
    ):
        existing_email = profile_account_email(
            request.base_name,
            get_storage_path=get_storage_path,
        )
        if existing_email is None or existing_email.casefold() == request.account_email.casefold():
            return request.base_name
    return _next_available_profile_name(
        request.base_name,
        request.unavailable | request.claimed,
    )


async def repair_playwright_account(request: ProfileRepairRequest) -> ProfileRepairOutcome:
    """Repair account metadata and project the public result to a neutral value."""
    storage_path: Path | None = None
    page_html: str | None = None
    repair_coroutine = None
    public_result = None
    outcome: ProfileRepairOutcome | None = None
    try:
        storage_path = request.storage_path
        page_html = request.page_html
        repair_coroutine = _repair_playwright_account(request)
        public_result = await repair_coroutine
        if public_result.written:
            outcome = ProfileRepairOutcome(status="WRITTEN", email=public_result.email)
        elif public_result.error is not None:
            outcome = ProfileRepairOutcome(status="ERROR", detail=public_result.error)
        else:
            outcome = ProfileRepairOutcome(
                status="AMBIGUOUS",
                detail=public_result.ambiguity_reason,
            )
        return outcome
    finally:
        del request, storage_path, page_html, repair_coroutine, public_result, outcome


def is_protected_profile(name: str, *, configured_default: str, effective_active: str) -> bool:
    """Return ``True`` when ``name`` is the active or configured-default profile.

    ``profile delete`` blocks removal of either so a user can't strand
    themselves without a usable profile. Pure decision — the adapter raises its
    own ``ClickException`` with the "switch first" remediation hint.
    """
    return name in (configured_default, effective_active)


def set_default_profile_mutator(name: str) -> Callable[[dict], dict]:
    """Return a ``config.json`` mutator that sets ``default_profile`` to ``name``.

    Used by ``profile switch`` under the adapter's locked read-modify-write.
    """

    def _set_default(data: dict) -> dict:
        data["default_profile"] = name
        return data

    return _set_default


def retarget_default_profile_mutator(
    *,
    old_name: str,
    new_name: str,
) -> tuple[Callable[[dict], dict], Callable[[], bool]]:
    """Return a ``config.json`` mutator that retargets the default after a rename.

    The mutator rewrites ``default_profile`` from ``old_name`` to ``new_name``
    **only** when the current default (treating a missing key as the implicit
    ``"default"``) is ``old_name`` — the single read of ``default_profile`` that
    matters happens under the adapter's lock. The returned ``was_updated``
    predicate reports whether the rewrite fired, so the adapter can print the
    "Updated default profile in config" note.
    """
    updated = False

    def _retarget_default(current: dict) -> dict:
        nonlocal updated
        if (current.get("default_profile") or "default") == old_name:
            current["default_profile"] = new_name
            updated = True
        return current

    def _was_updated() -> bool:
        return updated

    return _retarget_default, _was_updated


__all__ = [
    "ProfileAccountObservation",
    "ProfileEntry",
    "ProfileRepairOutcome",
    "ProfileRepairRequest",
    "ProfileRepairStatus",
    "ProfileTargetRequest",
    "StoragePathForProfile",
    "gather_profile_accounts",
    "gather_profile_list",
    "is_protected_profile",
    "observe_profile_account",
    "profile_account_email",
    "profiles_by_account_email",
    "repair_playwright_account",
    "resolve_all_accounts_target",
    "retarget_default_profile_mutator",
    "set_default_profile_mutator",
]
