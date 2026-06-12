"""Shared helpers for the ``cli/test_session*.py`` split files.

This module hosts the fixtures and synthetic-data factories that the
session-CLI test suite (originally a single 4400-LOC ``test_session.py``)
used to keep co-located at the top of the file. After D1 PR-3 split the
suite into seven focused files (``test_login.py``,
``test_login_multi_account.py``, ``test_login_chromium_fanout.py``,
``test_use.py``, ``test_status_clear.py``, ``test_auth_subcommands.py``,
``test_session_edge_cases.py``), each file imports the helpers it needs
from here rather than duplicating them.

Fixtures (``runner``, ``mock_auth``, ``mock_context_file``) live in
``tests/unit/cli/conftest.py``, not here — pytest gives conftest fixtures
priority over plugin fixtures, so defining them in two places makes the
plugin copies dead. The conftest definitions cover the session-CLI use
cases (the ``__Secure-1PSIDTS`` cookie + the consumer-module
``get_context_path`` patch sites, e.g.
``notebooklm.cli.services.session_context.get_context_path``).

This file holds the chromium-fanout helpers (``_make_chromium_profile``,
``_chromium_fanout_setup``, ``_install_chromium_fanout_patches``) and the
``_multiaccount_rookiepy_mock`` factory so both
``test_login_chromium_fanout.py`` and any tests in
``test_auth_subcommands.py`` that hit the fan-out paths can pull them
from a single canonical location.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import notebooklm.auth as auth_module
import notebooklm.cli._chromium_profiles as chromium_profiles

# ---------------------------------------------------------------------------
# Account-metadata read helpers (D1 PR-3: session-CLI tests read account
# metadata via the canonical reader; both the legacy sibling-``context.json``
# layout and the new in-band ``notebooklm`` namespace key are handled
# transparently).
# ---------------------------------------------------------------------------


def _read_account(storage_path: Path) -> dict:
    """Test-suite helper: read account metadata via the canonical reader.

    Post-P1-20 the account record lives inside ``storage_state.json`` under
    the ``notebooklm`` namespace key; ``read_account_metadata`` handles both
    the new in-band layout and the legacy sibling ``context.json`` fallback,
    so tests that previously read ``context.json`` directly can route
    through this helper without caring which on-disk shape they hit.
    """
    from notebooklm.auth import read_account_metadata

    return read_account_metadata(storage_path)


def _account_exists(storage_path: Path) -> bool:
    """Test-suite helper: True iff any non-empty account record is present."""
    return bool(_read_account(storage_path))


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------
#
# Note: ``runner``, ``mock_auth``, and ``mock_context_file`` fixtures used to
# live here, but pytest's fixture resolution gives conftest fixtures higher
# priority than plugin fixtures (plugin < root conftest < directory conftest).
# Since ``tests/unit/cli/conftest.py`` already defines those names, the
# plugin copies were dead. The authoritative definitions now live in
# ``tests/unit/cli/conftest.py`` — including the consumer-module
# ``get_context_path`` patches (the ``session_cmd`` re-export was retired
# in #1367; ``read_status`` now resolves the symbol on
# ``notebooklm.cli.services.session_context``) and the ``__Secure-1PSIDTS``
# cookie in ``mock_auth`` (required by ``_validate_required_cookies``).


# ---------------------------------------------------------------------------
# Multi-account rookiepy mock (login --account / --all-accounts flow)
# ---------------------------------------------------------------------------


def _multiaccount_rookiepy_mock() -> MagicMock:
    """Build a rookiepy mock that returns SID-bearing cookies for any domain query.

    Account enumeration is controlled by a separately-patched
    ``enumerate_accounts`` coroutine in each test that calls this.
    """
    cookies = [
        {
            "domain": ".google.com",
            "name": name,
            "value": f"{name}-value",
            "path": "/",
            "secure": True,
            "expires": 9999,
            "http_only": False,
        }
        for name in ("SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSIDTS")
    ]
    mock = MagicMock()
    mock.chrome = MagicMock(return_value=cookies)
    mock.load = MagicMock(return_value=cookies)
    return mock


# ---------------------------------------------------------------------------
# Chromium multi-user-profile fan-out helpers (#571)
# ---------------------------------------------------------------------------


def _make_chromium_profile(directory_name: str, human_name: str, cookies_db: Path) -> Any:
    """Build a synthetic ``ChromiumProfile`` for fan-out tests."""
    from notebooklm.cli._chromium_profiles import ChromiumProfile

    return ChromiumProfile(
        browser="chrome",
        directory_name=directory_name,
        human_name=human_name,
        cookies_db=cookies_db,
    )


def _chromium_fanout_setup(
    tmp_path: Path,
    profile_specs: list[tuple[str, str, list[dict[str, Any]]]],
) -> tuple[list[Any], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Install fixtures that make the chromium fan-out path deterministic.

    Args:
        tmp_path: pytest ``tmp_path``.
        profile_specs: list of ``(directory_name, human_name, accounts)``
            where ``accounts`` is a list of dicts
            ``{"authuser": int, "email": str, "is_default": bool}``.

    Returns:
        Tuple ``(profiles, cookies_per_profile, accounts_per_profile)`` of
        the data structures that the patches will return. Useful when a
        test wants to assert on what was set up.
    """
    profiles: list[Any] = []
    cookies_per_profile: dict[str, list[dict[str, Any]]] = {}
    accounts_per_profile: dict[str, list[dict[str, Any]]] = {}
    for dir_name, human, account_dicts in profile_specs:
        db = tmp_path / f"{dir_name}-Cookies"
        db.write_bytes(b"x")  # presence-only, never opened by mocks
        profile = _make_chromium_profile(dir_name, human, db)
        profiles.append(profile)
        # Unique per-profile cookie value so the writer-side assertions
        # can distinguish which profile's jar was used to write each
        # account's notebooklm ``storage_state.json``.
        cookies_per_profile[dir_name] = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": f"SID-from-{dir_name}",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": f"SIDTS-from-{dir_name}",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        accounts_per_profile[dir_name] = account_dicts
    return profiles, cookies_per_profile, accounts_per_profile


@contextlib.contextmanager
def _install_chromium_fanout_patches(
    profiles: list[Any],
    cookies_per_profile: dict[str, list[dict[str, Any]]],
    accounts_per_profile: dict[str, list[dict[str, Any]]],
    *,
    read_calls: list[str] | None = None,
):
    """Install all fan-out patches for the test body.

    Patches discovery, per-profile cookie reads, ``rookiepy`` (so the
    optional-dep import inside ``read_chromium_profile_cookies``
    succeeds), and ``enumerate_accounts`` so each profile yields its own
    account list.
    """
    from notebooklm.auth import Account

    def fake_discover(browser_name: str):
        return profiles if browser_name.lower() == "chrome" else []

    def fake_read(profile, *, domains):
        if read_calls is not None:
            read_calls.append(profile.directory_name)
        return cookies_per_profile[profile.directory_name]

    pending = {p.directory_name: list(accounts_per_profile[p.directory_name]) for p in profiles}

    async def fake_enumerate(jar, *args, **kwargs):
        # ``_enumerate_one_jar`` builds a jar from the cookies it just read,
        # so the SID value (unique per profile in our setup) identifies which
        # profile this call corresponds to.
        sid = jar.get("SID", default="")
        for dir_name in pending:
            if sid == f"SID-from-{dir_name}":
                spec = pending.pop(dir_name)
                return [
                    Account(authuser=a["authuser"], email=a["email"], is_default=a["is_default"])
                    for a in spec
                ]
        raise AssertionError(f"unexpected enumerate_accounts call (SID={sid!r})")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict("sys.modules", {"rookiepy": MagicMock()}))
        stack.enter_context(
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                side_effect=fake_discover,
            )
        )
        stack.enter_context(
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=fake_read,
            )
        )
        stack.enter_context(
            patch.object(auth_module, "enumerate_accounts", side_effect=fake_enumerate)
        )
        yield
