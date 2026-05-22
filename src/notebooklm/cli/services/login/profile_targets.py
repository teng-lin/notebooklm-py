"""Profile-name allocation + sanitization helpers.

Leaf module within the ``cli/services/login/`` package — no in-package
imports. Owns the public-facing ``email_to_profile_name`` helper
(re-exported by :mod:`notebooklm.cli.profile_cmd`) plus the internal
allocation helpers used by the ``--all-accounts`` writer in
:mod:`.refresh`.
"""

from __future__ import annotations

import re

import click

from ....auth import read_account_metadata
from ....paths import get_storage_path

# Profile name validation: alphanumeric, hyphens, underscores. Must start with alphanum.
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_profile_name(name: str) -> str:
    """Validate a profile name."""
    if not _PROFILE_NAME_RE.match(name):
        raise click.ClickException(
            f"Invalid profile name '{name}'. "
            "Use alphanumeric characters, hyphens, and underscores. Must start with a letter or digit."
        )
    return name


def email_to_profile_name(email: str, *, fallback: str = "account") -> str:
    """Derive a valid profile name from an email address.

    Profile names are restricted to ``[a-zA-Z0-9_-]`` (see
    :data:`_PROFILE_NAME_RE`) and must start with an alphanumeric character.
    Email local-parts routinely contain ``.``, ``+``, etc. that aren't
    allowed, so we rewrite them to hyphens.

    Examples::

        alice@example.com         -> "alice"
        alice.smith@example.com   -> "alice-smith"
        bob+work@gmail.com        -> "bob-work"
        teng.lin.9414@gmail.com   -> "teng-lin-9414"

    Args:
        email: Account email address.
        fallback: Profile name to use when sanitization yields an empty
            string or a name that does not start with an alphanum.

    Returns:
        A profile name guaranteed to satisfy :data:`_PROFILE_NAME_RE`.
    """
    local = email.split("@", 1)[0] if "@" in email else email
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", local)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-_")
    if not sanitized or not _PROFILE_NAME_RE.match(sanitized):
        # The function's contract is "always returns a valid profile name", so
        # protect callers that pass a malformed fallback (e.g. "-tmp").
        return fallback if _PROFILE_NAME_RE.match(fallback) else "account"
    return sanitized


def _profiles_by_account_email(profile_names: list[str]) -> dict[str, str]:
    """Return existing profiles keyed by *casefolded* account metadata email.

    Keys are casefolded so that mixed-casing in stored ``context.json``
    metadata (``Alice@Gmail.com`` vs. an incoming ``alice@gmail.com``)
    doesn't cause us to miss the match and wrongly allocate a suffixed
    profile. Lookup callers must casefold their email key likewise.
    """
    profiles_by_email: dict[str, str] = {}
    for profile in profile_names:
        metadata = read_account_metadata(get_storage_path(profile=profile))
        email = metadata.get("email")
        if isinstance(email, str) and email:
            # list_profiles() is sorted, so this also prefers the unsuffixed
            # profile over older duplicate suffixes such as alice-2.
            profiles_by_email.setdefault(email.casefold(), profile)
    return profiles_by_email


def _profile_account_email(profile: str) -> str | None:
    """Return the account email recorded in ``profile``'s ``context.json``.

    ``None`` when the profile has no account metadata at all (hand-created
    via plain ``notebooklm login --profile NAME``, or pre-dating the
    account-tracking feature). Used by ``--all-accounts --update`` to
    decide whether adopting a name-matching profile is safe.
    """
    metadata = read_account_metadata(get_storage_path(profile=profile))
    email = metadata.get("email")
    return email if isinstance(email, str) and email else None


def _next_available_profile_name(base_name: str, unavailable: set[str]) -> str:
    """Return ``base_name`` or the next ``-N`` suffix not in ``unavailable``."""
    if base_name not in unavailable:
        return base_name

    suffix = 2
    while True:
        candidate = f"{base_name}-{suffix}"
        if candidate not in unavailable:
            return candidate
        suffix += 1


def _resolve_all_accounts_target(
    *,
    base_name: str,
    account_email: str,
    existing_profiles: set[str],
    unavailable: set[str],
    claimed: set[str],
    update: bool,
) -> str:
    """Pick the destination profile when no email-metadata match exists.

    Without ``--update`` (default): always allocate the next available
    suffix (``alice``, ``alice-2``, …) — never touch a hand-created profile.

    With ``--update``: adopt the unsuffixed ``base_name`` profile in place
    if it exists AND has either (a) no account metadata at all or (b)
    metadata for the same email (defensive — that should have been picked
    up by ``profiles_by_email`` upstream, but the casefold mismatch case is
    cheap to handle). Profiles whose metadata binds a *different* email
    fall back to the suffix path to avoid clobbering them.
    """
    if update and base_name in existing_profiles and base_name not in claimed:
        existing_email = _profile_account_email(base_name)
        if existing_email is None or existing_email.casefold() == account_email.casefold():
            return base_name
    return _next_available_profile_name(base_name, unavailable | claimed)
