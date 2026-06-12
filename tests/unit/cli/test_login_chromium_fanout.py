"""Tests for the Chromium multi-user-profile fan-out flows (``auth inspect``, ``--all-accounts``, ``--account``, explicit-profile selector, boundary conditions; #571).

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import notebooklm.auth as auth_module
import notebooklm.cli._chromium_profiles as chromium_profiles
import notebooklm.paths as paths_module
from notebooklm.notebooklm_cli import cli
from tests._fixtures import patch_session_login_dual

from ._session_helpers import (
    _account_exists,
    _chromium_fanout_setup,
    _install_chromium_fanout_patches,
    _multiaccount_rookiepy_mock,
)


class TestChromiumFanoutAuthInspect:
    """``auth inspect`` aggregates accounts across Chrome user-profiles (#571)."""

    def test_lists_accounts_across_profiles_email_only_by_default(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 2",
                    "Side",
                    [{"authuser": 0, "email": "carol@ws.com", "is_default": True}],
                ),
            ],
        )
        with _install_chromium_fanout_patches(profiles, cookies, accounts):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])
        assert result.exit_code == 0, result.output
        assert "alice@gmail.com" in result.output
        assert "bob@gmail.com" in result.output
        assert "carol@ws.com" in result.output
        # The result table is email-primary — no per-row browser-profile column
        # by default. The "Reading cookies from N profiles…" status line may
        # mention profile names, but the table rows don't repeat them.
        table_lines = [line for line in result.output.splitlines() if "@" in line]
        for row in table_lines:
            assert "Default" not in row
            assert "Profile 1" not in row
            assert "Personal" not in row
            assert "Work" not in row
        # And the help text nudges the user toward -v.
        assert "-v" in result.output

    def test_verbose_shows_browser_user_profile_column(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )
        with _install_chromium_fanout_patches(profiles, cookies, accounts):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "-v"])
        assert result.exit_code == 0, result.output
        assert "Default" in result.output
        assert "Profile 1" in result.output

    def test_json_output_includes_browser_profile(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )
        with _install_chromium_fanout_patches(profiles, cookies, accounts):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        emails = {a["email"]: a["browser_profile"] for a in data["accounts"]}
        assert emails == {
            "alice@gmail.com": "Default",
            "bob@gmail.com": "Profile 1",
        }

    def test_duplicate_email_across_profiles_deduped_first_wins(self, runner, tmp_path):
        # Same email signed in to two Chrome user-profiles. Default wins
        # (it's iterated first) and the second occurrence is dropped.
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
            ],
        )
        with _install_chromium_fanout_patches(profiles, cookies, accounts):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert [a["email"] for a in data["accounts"]] == ["alice@gmail.com"]
        assert data["accounts"][0]["browser_profile"] == "Default"

    def test_all_profile_cookie_read_failures_surface_read_error(self, runner, tmp_path):
        profiles, _, _ = _chromium_fanout_setup(
            tmp_path,
            [
                ("Default", "Personal", []),
                ("Profile 1", "Work", []),
            ],
        )

        def fake_discover(browser_name):
            return profiles if browser_name.lower() == "chrome" else []

        def fake_read(profile, *, domains):
            raise RuntimeError(f"locked {profile.directory_name}")

        with (
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                side_effect=fake_discover,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=fake_read,
            ),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])

        assert result.exit_code == 1, result.output
        assert "Could not read cookies from any chrome user-profile" in result.output
        assert "locked Default" in result.output
        assert "No signed-in Google accounts found" not in result.output


class TestChromiumFanoutAllAccounts:
    """``login --browser-cookies chrome --all-accounts`` writes one profile per
    unique Google account across every Chrome user-profile (#571)."""

    def test_all_accounts_writes_profile_per_browser_user_profile(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 2",
                    "Side",
                    [{"authuser": 0, "email": "carol@ws.com", "is_default": True}],
                ),
            ],
        )
        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(p.name for p in target_root.iterdir() if p.is_dir())

        with (
            _install_chromium_fanout_patches(profiles, cookies, accounts),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch.object(paths_module, "list_profiles", side_effect=fake_list_profiles),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        assert _account_exists(target_root / "alice" / "storage_state.json")
        assert _account_exists(target_root / "bob" / "storage_state.json")
        assert _account_exists(target_root / "carol" / "storage_state.json")
        # Cookies written for "bob" must come from Profile 1, not Default —
        # this is the core bug the fan-out fixes.
        bob_storage = json.loads((target_root / "bob" / "storage_state.json").read_text())
        sid_cookie = next(c for c in bob_storage["cookies"] if c["name"] == "SID")
        assert sid_cookie["value"] == "SID-from-Profile 1"
        # And alice@gmail.com's cookies come from Default.
        alice_storage = json.loads((target_root / "alice" / "storage_state.json").read_text())
        alice_sid = next(c for c in alice_storage["cookies"] if c["name"] == "SID")
        assert alice_sid["value"] == "SID-from-Default"

    def test_all_accounts_handles_profile_with_no_signed_in_account(self, runner, tmp_path):
        # Profile 2 has a Cookies DB but no signed-in Google account
        # (rookiepy decrypt succeeds, but enumerate_accounts rejects the jar).
        # The remaining two profiles' accounts should still be written.
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                ("Profile 1", "Work", []),  # empty → signals signed-out below
                (
                    "Profile 2",
                    "Side",
                    [{"authuser": 0, "email": "carol@ws.com", "is_default": True}],
                ),
            ],
        )

        # Override the enumerate_accounts handler to SystemExit for Profile 1,
        # mimicking ``_enumerate_one_jar`` exiting on a missing-SID jar.
        from notebooklm.auth import Account

        async def fake_enumerate(jar, *args, **kwargs):
            sid = jar.get("SID", default="")
            if sid == "SID-from-Profile 1":
                # ``_enumerate_one_jar`` catches this and converts to a
                # SystemExit, which the fan-out then catches as "signed out".
                raise ValueError("no signed-in account at authuser=0")
            for dir_name, spec in accounts.items():
                if sid == f"SID-from-{dir_name}" and spec:
                    return [
                        Account(
                            authuser=a["authuser"], email=a["email"], is_default=a["is_default"]
                        )
                        for a in spec
                    ]
            raise AssertionError(f"unexpected SID {sid!r}")

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(p.name for p in target_root.iterdir() if p.is_dir())

        def fake_read(profile, *, domains):
            return cookies[profile.directory_name]

        def fake_discover(browser_name):
            return profiles if browser_name.lower() == "chrome" else []

        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                side_effect=fake_discover,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=fake_read,
            ),
            patch.object(auth_module, "enumerate_accounts", side_effect=fake_enumerate),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch.object(paths_module, "list_profiles", side_effect=fake_list_profiles),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        assert _account_exists(target_root / "alice" / "storage_state.json")
        assert _account_exists(target_root / "carol" / "storage_state.json")
        # No profile written for the signed-out Profile 1.
        assert not any(p.name not in {"alice", "carol"} for p in target_root.iterdir())


class TestChromiumFanoutAccountSelector:
    """``--account EMAIL`` picks the right cookie source even when the email
    lives in a non-Default Chrome user-profile (#571)."""

    def test_account_email_from_non_default_profile(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )
        target_dir = tmp_path / "profiles" / "bob"

        def fake_get_storage_path(profile=None):
            return target_dir / "storage_state.json"

        with (
            _install_chromium_fanout_patches(profiles, cookies, accounts),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
            )

        assert result.exit_code == 0, result.output
        storage = json.loads((target_dir / "storage_state.json").read_text())
        sid = next(c for c in storage["cookies"] if c["name"] == "SID")
        # Critically: bob's cookies must come from Profile 1, NOT Default.
        # Before #571 the CLI couldn't see Profile 1 at all.
        assert sid["value"] == "SID-from-Profile 1"


class TestChromiumExplicitProfileSelector:
    """``chrome::<profile>`` scopes cookie reads to one Chromium user-profile."""

    def test_auth_inspect_scopes_to_human_profile_name(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )
        read_calls: list[str] = []

        with _install_chromium_fanout_patches(profiles, cookies, accounts, read_calls=read_calls):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome::Work", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["accounts"] == [
            {
                "email": "bob@gmail.com",
                "is_default": True,
                "browser_profile": "Profile 1",
            }
        ]
        assert read_calls == ["Profile 1"]

    def test_login_direct_cookie_read_scopes_to_directory_name(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )
        storage_file = tmp_path / "storage.json"
        read_calls: list[str] = []

        with (
            _install_chromium_fanout_patches(profiles, cookies, accounts, read_calls=read_calls),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome::Profile 1"])

        assert result.exit_code == 0, result.output
        storage = json.loads(storage_file.read_text())
        sid = next(c for c in storage["cookies"] if c["name"] == "SID")
        assert sid["value"] == "SID-from-Profile 1"
        assert read_calls == ["Profile 1"]

    def test_login_account_mismatch_does_not_fall_back_to_other_profile(self, runner, tmp_path):
        profiles, cookies, accounts = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "a.b@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "my-profile",
                    [{"authuser": 0, "email": "c.d@gmail.com", "is_default": True}],
                ),
            ],
        )
        read_calls: list[str] = []

        with (
            _install_chromium_fanout_patches(profiles, cookies, accounts, read_calls=read_calls),
            patch_session_login_dual(
                "_write_extracted_cookies",
                side_effect=AssertionError("must not write cookies for an account mismatch"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome::my-profile",
                    "--account",
                    "a.b@gmail.com",
                ],
            )

        assert result.exit_code != 0, result.output
        assert "Account a.b@gmail.com not found among signed-in accounts" in result.output
        assert "Available accounts: c.d@gmail.com" in result.output
        assert read_calls == ["Profile 1"]

    def test_unknown_profile_selector_lists_available_profiles(self, runner, tmp_path):
        profiles, _cookies, _accounts = _chromium_fanout_setup(
            tmp_path,
            [
                ("Default", "Personal", []),
                ("Profile 1", "Work", []),
            ],
        )

        with (
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                return_value=profiles,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=AssertionError("must not read cookies for an unknown selector"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome::Missing"])

        assert result.exit_code != 0, result.output
        assert "Missing" in result.output
        assert "Personal" in result.output
        assert "Default" in result.output
        assert "Work" in result.output
        assert "Profile 1" in result.output

    def test_ambiguous_human_name_selector_asks_for_directory(self, runner, tmp_path):
        profiles, _cookies, _accounts = _chromium_fanout_setup(
            tmp_path,
            [
                ("Profile 1", "Work", []),
                ("Profile 2", "Work", []),
            ],
        )

        with (
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                return_value=profiles,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=AssertionError("must not read cookies for an ambiguous selector"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome::Work"])

        assert result.exit_code != 0, result.output
        assert "ambiguous" in result.output
        assert "Profile 1" in result.output
        assert "Profile 2" in result.output

    def test_empty_profile_selector_is_rejected_before_cookie_read(self, runner):
        with patch.object(
            chromium_profiles,
            "read_chromium_profile_cookies",
            side_effect=AssertionError("must not read cookies for an empty selector"),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome::"])

        assert result.exit_code != 0, result.output
        assert "Empty Chromium profile selector" in result.output


class TestChromiumFanoutBoundaryConditions:
    """Boundary cases around when fan-out activates vs. legacy single-jar
    path (raised by reviewers on #580)."""

    def test_single_chromium_profile_uses_legacy_single_jar_path(self, runner):
        """When discovery surfaces exactly ONE chromium user-profile, the
        legacy ``_read_browser_cookies`` path runs (so existing rookiepy
        mocks keep working). The new ``read_chromium_profile_cookies`` /
        ``any_browser`` fan-out path must NOT be invoked.
        """
        from notebooklm.cli._chromium_profiles import ChromiumProfile

        only_default = [
            ChromiumProfile(
                browser="chrome",
                directory_name="Default",
                human_name="Default",
                cookies_db=Path("/dev/null"),
            )
        ]

        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                return_value=only_default,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=AssertionError(
                    "fan-out must NOT run when only 1 chromium profile exists"
                ),
            ),
            patch.object(auth_module, "enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])

        assert result.exit_code == 0, result.output
        # Legacy path was used → mock_rk.chrome was called, not any_browser.
        mock_rk.chrome.assert_called_once()
        assert "alice@example.com" in result.output

    def test_network_error_aborts_fanout_not_silent_skip(self, runner, tmp_path):
        """``httpx.RequestError`` during ``enumerate_accounts`` must abort
        the entire fan-out with a clear network error, not get caught as
        a per-profile "signed out" skip. (CodeRabbit major on #580.)
        """
        profiles, cookies, _ = _chromium_fanout_setup(
            tmp_path,
            [
                (
                    "Default",
                    "Personal",
                    [{"authuser": 0, "email": "alice@gmail.com", "is_default": True}],
                ),
                (
                    "Profile 1",
                    "Work",
                    [{"authuser": 0, "email": "bob@gmail.com", "is_default": True}],
                ),
            ],
        )

        async def fake_enumerate(jar, *args, **kwargs):
            raise httpx.ConnectError("DNS failure: notebooklm.google.com")

        def fake_discover(browser_name):
            return profiles if browser_name.lower() == "chrome" else []

        def fake_read(profile, *, domains):
            return cookies[profile.directory_name]

        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch.object(
                chromium_profiles,
                "discover_chromium_profiles",
                side_effect=fake_discover,
            ),
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=fake_read,
            ),
            patch.object(auth_module, "enumerate_accounts", side_effect=fake_enumerate),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])

        # Fan-out must abort with non-zero exit + a network error message —
        # NOT silently return "No accounts found" by collapsing each profile
        # probe into the soft signed-out skip.
        assert result.exit_code != 0, result.output
        assert "network" in result.output.lower()
        assert "DNS failure" in result.output
