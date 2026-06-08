"""Transport-neutral profile-management business logic.

This is the Click-free core of ``cli/profile_cmd.py``: it owns the ``profile
list`` data-gathering (profile name → active/authenticated/account rows), the
``profile delete`` active/default guard decision, and the ``config.json``
default-profile retarget mutators used by ``switch`` / ``rename``. Every
transport adapter (the Click CLI today, the FastMCP server / future HTTP surface
tomorrow) drives this core and renders the typed rows / applies the decision
with its own surface + exit-code policy + confirmation prompts.

Boundary-imposed seams:

* **The profile/storage/account helpers are injected, never imported.**
  :func:`gather_profile_list` takes ``list_profiles`` / ``resolve_profile`` /
  ``get_storage_path`` / ``read_account_metadata`` callables; the CLI wrapper
  reads those off its own ``profile_cmd`` module at call time so the historical
  ``patch.object(profile_cmd, "list_profiles", ...)`` test seams keep landing.
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
from typing import Any

#: Returns the list of known profile names.
ListProfilesFn = Callable[[], list[str]]
#: Returns the active (effective) profile name.
ResolveProfileFn = Callable[[], str]
#: Resolves a profile's ``storage_state.json`` path.
StoragePathFn = Callable[..., Path]
#: Reads a storage file's persisted account metadata (``{}`` when absent).
ReadAccountMetadataFn = Callable[[Path], dict[str, Any]]


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
    "ProfileEntry",
    "gather_profile_list",
    "is_protected_profile",
    "retarget_default_profile_mutator",
    "set_default_profile_mutator",
]
