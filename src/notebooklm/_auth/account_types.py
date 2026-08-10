"""Dependency-neutral Google account and repair result values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    """A Google account discovered via authuser=N probing.

    Attributes:
        authuser: The integer index used in ``?authuser=N`` URL parameters.
            Index 0 is the default account; subsequent indices follow the
            order Google reports for the browser session.
        email: The account's email address as it appears in the NotebookLM
            page's ``WIZ_global_data`` block.
        is_default: True only for the account at ``authuser=0``.
        browser_profile: For Chromium-family browsers with multiple
            user-data profiles, the on-disk directory name (``"Default"``,
            ``"Profile 1"``) the cookies came from. ``None`` for non-chromium
            browsers and for the legacy single-jar path where source isn't
            tracked.
    """

    authuser: int
    email: str
    is_default: bool
    browser_profile: str | None = None


@dataclass(frozen=True)
class PlaywrightAccountRepairResult:
    """Outcome of :func:`repair_account_metadata_from_playwright_storage`.

    Exactly one of ``ambiguity_reason`` / ``error`` is set when ``written`` is
    ``False`` — callers use which one is set to pick between the two distinct
    user-facing warnings (a clean "could not disambiguate" vs. an unexpected
    failure worth surfacing exception detail for).
    """

    written: bool
    email: str | None = None
    ambiguity_reason: str | None = None
    error: str | None = None


# Preserve the historical class identity used by repr and pickle GLOBAL bytes.
Account.__module__ = "notebooklm._auth.account"
PlaywrightAccountRepairResult.__module__ = "notebooklm._auth.account"
