"""E2E for layer-3 headless re-auth (gated on a real persistent profile).

This test drives a REAL headless browser against the persistent login profile
and is excluded from CI by default (``-m e2e`` + a profile/opt-in gate). It
exists to exercise the one path the unit tests fake: the actual
launch -> navigate -> classify -> capture -> persist sequence against a live
Google session.

Run locally, after ``notebooklm login`` has populated the profile, with::

    NOTEBOOKLM_HEADLESS_REAUTH=1 pytest tests/e2e/test_headless_reauth.py -m e2e

It is SKIPPED unless both:

* the persistent browser profile exists and is non-empty (a real Google
  session to harvest), and
* the operator opted in via ``NOTEBOOKLM_HEADLESS_REAUTH=1`` (the same opt-in
  the production mid-RPC path requires) — so the test never silently drives a
  browser in an environment that did not ask for it.
"""

from __future__ import annotations

import os

import pytest

from notebooklm._auth.headless_reauth import (
    HeadlessReauthStatus,
    attempt_headless_reauth,
)
from notebooklm.paths import get_browser_profile_dir, get_storage_path


def _profile_is_reusable() -> bool:
    profile = get_browser_profile_dir()
    if not profile.is_dir():
        return False
    try:
        next(profile.iterdir())
    except (StopIteration, OSError):
        return False
    return True


def _playwright_available() -> bool:
    """True when the ``browser`` extra is importable.

    Without it ``attempt_headless_reauth`` returns ``UNAVAILABLE`` (nothing to
    drive), which is NOT one of the SUCCESS/FAILED outcomes this test asserts —
    so the gate must skip rather than let the test fail on a missing optional
    dependency.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


_GATE = pytest.mark.skipif(
    os.environ.get("NOTEBOOKLM_HEADLESS_REAUTH") != "1"
    or not _profile_is_reusable()
    or not _playwright_available(),
    reason=(
        "headless re-auth e2e requires NOTEBOOKLM_HEADLESS_REAUTH=1, a non-empty "
        "persistent browser profile (run 'notebooklm login' first), and the "
        "'browser' extra installed"
    ),
)


@pytest.mark.e2e
@_GATE
def test_headless_reauth_against_live_profile() -> None:
    """Drive a real headless re-auth and assert an honest, typed outcome.

    A live profile session yields SUCCESS (cookies re-minted + persisted); an
    expired profile session yields FAILED. UNAVAILABLE should not occur here
    because the gate already proved opt-in + a reusable profile. Either way the
    result is one of the three honest, typed outcomes — never a silent ``None``.
    """
    result = attempt_headless_reauth(
        storage_path=get_storage_path(),
        allow_headless=True,
    )

    assert result.status in {
        HeadlessReauthStatus.SUCCESS,
        HeadlessReauthStatus.FAILED,
    }, f"unexpected status {result.status} (reason: {result.reason})"

    if result.status is HeadlessReauthStatus.SUCCESS:
        assert result.succeeded is True
        assert result.storage_path == get_storage_path()
        assert get_storage_path().exists()
    else:
        # Honest failure — the profile's Google session is also expired.
        assert result.succeeded is False
        assert result.storage_path is None
