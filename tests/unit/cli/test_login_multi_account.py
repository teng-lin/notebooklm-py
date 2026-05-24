"""Tests for ``login --account`` / ``login --all-accounts`` / stale-account-metadata cleanup.

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from _fixtures import patch_session_login_dual
from notebooklm.notebooklm_cli import cli

from ._session_helpers import (
    _account_exists,
    _multiaccount_rookiepy_mock,
    _read_account,
)


def _write_account_metadata(storage_file, *, authuser, email):
    from notebooklm.auth import write_account_metadata

    write_account_metadata(storage_file, authuser=authuser, email=email)


def _profile_storage_path(target_root):
    def fake_get_storage_path(profile=None):
        return target_root / (profile or "default") / "storage_state.json"

    return fake_get_storage_path


def _account_enum(accounts=None):
    account_specs = (
        accounts
        if accounts is not None
        else [
            (0, "alice@example.com", True),
            (1, "bob@gmail.com", False),
        ]
    )

    async def _enum(*args, **kwargs):
        from notebooklm.auth import Account

        return [
            Account(authuser=authuser, email=email, is_default=is_default)
            for authuser, email, is_default in account_specs
        ]

    return _enum


class TestLoginMultiAccount:
    """--account / --profile-name / --all-accounts on `notebooklm login --browser-cookies`."""

    def test_account_writes_default_profile_by_default(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
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
        storage_file = target_root / "default" / "storage_state.json"
        assert _account_exists(storage_file)
        assert not (target_root / "bob").exists()
        mock_sync.assert_called_once_with(storage_path=storage_file, profile=None)
        assert _read_account(storage_file) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_account_honors_global_profile(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "--profile",
                    "work",
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--account",
                    "bob@gmail.com",
                ],
            )

        assert result.exit_code == 0, result.output
        storage_file = target_root / "work" / "storage_state.json"
        assert _account_exists(storage_file)
        mock_sync.assert_called_once_with(storage_path=storage_file, profile="work")
        assert _read_account(storage_file) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_account_profile_name_still_writes_named_profile(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--account",
                    "bob@gmail.com",
                    "--profile-name",
                    "bob",
                ],
            )

        assert result.exit_code == 0, result.output
        storage_file = target_root / "bob" / "storage_state.json"
        assert _account_exists(storage_file)
        assert not (target_root / "default" / "storage_state.json").exists()
        mock_sync.assert_called_once_with(storage_path=storage_file, profile="bob")
        assert _read_account(storage_file) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_account_storage_bypasses_profile_targeting(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()
        target = tmp_path / "custom-storage.json"
        _write_account_metadata(target, authuser=0, email="alice@example.com")

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("click.confirm") as mock_confirm,
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--account",
                    "bob@gmail.com",
                    "--storage",
                    str(target),
                ],
            )

        assert result.exit_code == 0, result.output
        mock_confirm.assert_not_called()
        mock_sync.assert_called_once_with(storage_path=target, profile=None)
        assert _read_account(target) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_account_same_existing_profile_account_does_not_prompt(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"
        storage_file = target_root / "default" / "storage_state.json"
        _write_account_metadata(storage_file, authuser=1, email="bob@gmail.com")

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("click.confirm") as mock_confirm,
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch(
                "notebooklm.auth.enumerate_accounts",
                new=_account_enum([(1, "bob@gmail.com", False)]),
            ),
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
        mock_confirm.assert_not_called()
        assert _read_account(storage_file) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_account_different_existing_profile_account_aborts_without_confirm(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"
        storage_file = target_root / "default" / "storage_state.json"
        _write_account_metadata(storage_file, authuser=0, email="alice@example.com")

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
                input="\n",
            )

        assert result.exit_code != 0
        assert "already has auth for alice@example.com" in result.output
        assert "not overwriting with bob@gmail.com" in result.output
        assert _read_account(storage_file) == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_account_existing_profile_without_metadata_aborts_without_confirm(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"
        storage_file = target_root / "default" / "storage_state.json"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text(json.dumps({"cookies": [], "origins": []}))

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
                input="\n",
            )

        assert result.exit_code != 0
        assert "saved auth without account metadata" in result.output
        assert "not overwriting" in result.output
        assert "bob@gmail.com" in result.output
        assert _read_account(storage_file) == {}

    def test_account_profile_name_existing_different_account_aborts_without_confirm(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"
        storage_file = target_root / "work" / "storage_state.json"
        _write_account_metadata(storage_file, authuser=0, email="alice@example.com")

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--account",
                    "bob@gmail.com",
                    "--profile-name",
                    "work",
                ],
                input="\n",
            )

        assert result.exit_code != 0
        assert "profile 'work' already has auth for alice@example.com" in result.output
        assert _read_account(storage_file) == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_account_different_existing_profile_account_overwrites_after_confirm(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()
        target_root = tmp_path / "profiles"
        storage_file = target_root / "default" / "storage_state.json"
        _write_account_metadata(storage_file, authuser=0, email="alice@example.com")

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
                input="y\n",
            )

        assert result.exit_code == 0, result.output
        assert _read_account(storage_file) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }

    def test_storage_without_account_keeps_default_import_path(self, runner, tmp_path):
        target = tmp_path / "storage_state.json"

        with (
            patch_session_login_dual("_login_with_browser_cookies") as login_mock,
            patch(
                "notebooklm.auth.enumerate_accounts",
                side_effect=AssertionError("should not enumerate accounts"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--storage", str(target)],
            )

        assert result.exit_code == 0, result.output
        login_mock.assert_called_once()
        assert login_mock.call_args.args[0] == target
        assert login_mock.call_args.args[1] == "chrome"

    def test_account_not_found_aborts(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome", "--account", "bob@gmail.com"],
            )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_all_accounts_writes_one_profile_per_account(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
            ]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        alice_meta = _read_account(target_root / "alice" / "storage_state.json")
        bob_meta = _read_account(target_root / "bob" / "storage_state.json")
        assert alice_meta == {"authuser": 0, "email": "alice@example.com"}
        assert bob_meta == {"authuser": 1, "email": "bob@gmail.com"}
        mock_sync.assert_called_once_with(
            storage_path=target_root / "bob" / "storage_state.json",
            profile="bob",
        )

    def test_all_accounts_rerun_reuses_profiles_by_email(self, runner, tmp_path):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
            ]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            first = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])
            second = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert sorted(path.name for path in target_root.iterdir()) == ["alice", "bob"]

    def test_all_accounts_does_not_overwrite_same_name_without_matching_email(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        target_root = tmp_path / "profiles"
        existing = target_root / "alice"
        existing.mkdir(parents=True)
        (existing / "storage_state.json").write_text("{}")

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert result.exit_code == 0, result.output
        alice2_storage = target_root / "alice-2" / "storage_state.json"
        assert _account_exists(alice2_storage)
        assert _read_account(alice2_storage) == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_all_accounts_updates_existing_profile_when_authuser_index_changes(
        self, runner, tmp_path
    ):
        mock_rk = _multiaccount_rookiepy_mock()

        first_accounts = None

        async def _enum(*args, **kwargs):
            nonlocal first_accounts
            from notebooklm.auth import Account

            if first_accounts is None:
                first_accounts = True
                return [
                    Account(authuser=0, email="alice@example.com", is_default=True),
                    Account(authuser=1, email="bob@gmail.com", is_default=False),
                ]
            return [Account(authuser=0, email="bob@gmail.com", is_default=True)]

        target_root = tmp_path / "profiles"

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            if not target_root.exists():
                return []
            return sorted(path.name for path in target_root.iterdir() if path.is_dir())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            first = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])
            second = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--all-accounts"])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert _read_account(target_root / "bob" / "storage_state.json") == {
            "authuser": 0,
            "email": "bob@gmail.com",
        }
        assert sorted(path.name for path in target_root.iterdir()) == ["alice", "bob"]

    def test_account_without_browser_cookies_rejected(self, runner):
        # --account only makes sense with --browser-cookies; the CLI should
        # tell the user instead of silently ignoring it.
        result = runner.invoke(cli, ["login", "--account", "bob@gmail.com"])
        assert result.exit_code != 0
        assert "browser-cookies" in result.output

    def test_authuser_option_is_not_exposed(self, runner):
        result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--authuser", "1"])
        assert result.exit_code != 0
        assert "--authuser" in result.output
        assert "no such option" in result.output.lower()

    def test_all_accounts_combined_with_account_rejected(self, runner):
        result = runner.invoke(
            cli,
            [
                "login",
                "--browser-cookies",
                "chrome",
                "--all-accounts",
                "--account",
                "bob@gmail.com",
            ],
        )
        assert result.exit_code != 0
        assert "all-accounts" in result.output.lower()


class TestLoginAllAccountsUpdate:
    """``--update`` lets ``--all-accounts`` adopt name-matching profiles in
    place instead of allocating a suffixed ``alice-2`` when the natural name
    is held by a hand-created profile with no account metadata."""

    @staticmethod
    def _run_all_accounts(
        runner,
        tmp_path,
        *,
        update: bool,
        accounts: list[tuple[int, str, bool]],
        preexisting: dict[str, dict | None] | None = None,
    ):
        """Run ``login --browser-cookies chrome --all-accounts [--update]``
        against a mocked rookiepy + ``enumerate_accounts`` setup.

        Args:
            accounts: ``(authuser, email, is_default)`` tuples returned by the
                mocked ``enumerate_accounts``.
            preexisting: map of ``profile_dir -> context.json contents``
                (``None`` = create the directory + an empty storage_state.json
                with no context.json, i.e. a hand-created profile with no
                account metadata).
        """
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=a, email=e, is_default=d) for a, e, d in accounts]

        target_root = tmp_path / "profiles"
        target_root.mkdir(parents=True)
        for name, ctx in (preexisting or {}).items():
            d = target_root / name
            d.mkdir()
            (d / "storage_state.json").write_text("{}")
            if ctx is not None:
                (d / "context.json").write_text(json.dumps(ctx))

        def fake_get_storage_path(profile=None):
            return target_root / (profile or "default") / "storage_state.json"

        def fake_list_profiles():
            return sorted(p.name for p in target_root.iterdir() if p.is_dir())

        argv = ["login", "--browser-cookies", "chrome", "--all-accounts"]
        if update:
            argv.append("--update")
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", side_effect=fake_get_storage_path),
            patch("notebooklm.paths.list_profiles", side_effect=fake_list_profiles),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, argv)
        return result, target_root

    def test_update_adopts_unsuffixed_profile_with_no_metadata(self, runner, tmp_path):
        # Pre-existing "alice" hand-created via `notebooklm login --profile alice`
        # — no context.json, no email metadata. With --update, it should
        # be adopted in place instead of getting an alice-2 suffix.
        result, root = self._run_all_accounts(
            runner,
            tmp_path,
            update=True,
            accounts=[(0, "alice@example.com", True)],
            preexisting={"alice": None},
        )
        assert result.exit_code == 0, result.output
        alice_storage = root / "alice" / "storage_state.json"
        assert _account_exists(alice_storage)
        assert (root / "alice-2").exists() is False
        assert _read_account(alice_storage) == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_default_still_allocates_suffix_for_unsuffixed_no_metadata(self, runner, tmp_path):
        # Same setup as above but WITHOUT --update — confirms the new flag is
        # the only opt-in for the in-place adoption.
        result, root = self._run_all_accounts(
            runner,
            tmp_path,
            update=False,
            accounts=[(0, "alice@example.com", True)],
            preexisting={"alice": None},
        )
        assert result.exit_code == 0, result.output
        # P1-20: the new profile lands in alice-2/ with an in-band account
        # record in its storage_state.json.
        assert _account_exists(root / "alice-2" / "storage_state.json")
        # alice still exists but was never touched — no account record either
        # in-band or in the (absent) sibling context.json.
        assert not _account_exists(root / "alice" / "storage_state.json")
        assert (root / "alice" / "context.json").exists() is False

    def test_update_does_not_clobber_profile_bound_to_different_email(self, runner, tmp_path):
        # Safety guard: a profile named "alice" that already binds
        # alice@OTHER.com must NOT be hijacked by alice@example.com just
        # because --update is on. Falls back to the suffix path.
        result, root = self._run_all_accounts(
            runner,
            tmp_path,
            update=True,
            accounts=[(0, "alice@example.com", True)],
            preexisting={"alice": {"account": {"authuser": 0, "email": "alice@OTHER.com"}}},
        )
        assert result.exit_code == 0, result.output
        # The new profile lands in alice-2 with an in-band record.
        assert _account_exists(root / "alice-2" / "storage_state.json")
        # Existing alice metadata (legacy sibling context.json) is untouched.
        assert _read_account(root / "alice" / "storage_state.json") == {
            "authuser": 0,
            "email": "alice@OTHER.com",
        }

    def test_update_is_idempotent_when_profile_already_has_matching_metadata(
        self, runner, tmp_path
    ):
        # If "alice" already binds the same email, --update changes nothing
        # observable (re-stamps the same metadata; doesn't create alice-2).
        result, root = self._run_all_accounts(
            runner,
            tmp_path,
            update=True,
            accounts=[(0, "alice@example.com", True)],
            preexisting={"alice": {"account": {"authuser": 0, "email": "alice@example.com"}}},
        )
        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in root.iterdir()) == ["alice"]
        # P1-20: --update re-writes account metadata in-band; the read goes
        # through ``read_account_metadata`` which prefers the in-band record
        # when present and falls back to the legacy sibling context.json
        # otherwise. Either way the assertion is on the canonical reader.
        assert _read_account(root / "alice" / "storage_state.json") == {
            "authuser": 0,
            "email": "alice@example.com",
        }

    def test_update_requires_all_accounts(self, runner):
        result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--update"])
        assert result.exit_code != 0
        assert "--update" in result.output
        assert "--all-accounts" in result.output

    def test_all_accounts_matches_existing_profile_case_insensitively(self, runner, tmp_path):
        """Stored email metadata may differ in case from what Google returns
        on a later probe (e.g. ``Alice@Gmail.com`` stored, ``alice@gmail.com``
        probed). The email-keyed reuse path must casefold both sides so the
        same profile is reused rather than allocating a suffixed duplicate.
        Regression for CodeRabbit's review on #594.
        """
        # No --update — proving the case-insensitive match works on the
        # default reuse-by-email-metadata path (not the --update name path).
        result, root = self._run_all_accounts(
            runner,
            tmp_path,
            update=False,
            accounts=[(0, "alice@gmail.com", True)],
            preexisting={"alice": {"account": {"authuser": 0, "email": "Alice@Gmail.com"}}},
        )
        assert result.exit_code == 0, result.output
        # No alice-2 — the existing alice profile was reused despite the
        # casing mismatch.
        assert (root / "alice-2").exists() is False
        # Re-stamped metadata uses the email as Google reports it now. The
        # in-band reader takes precedence over the legacy sibling record so
        # the casefold-and-rewrite test sees the new value.
        assert _read_account(root / "alice" / "storage_state.json") == {
            "authuser": 0,
            "email": "alice@gmail.com",
        }


class TestStaleAccountMetadataCleanup:
    """Default-account login must clear stale account metadata from previous targeted runs."""

    def test_default_login_removes_stale_account_metadata(self, runner, tmp_path):
        storage_file = tmp_path / "storage.json"
        # Simulate a previous targeted extraction.
        (tmp_path / "context.json").write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "bob@gmail.com"},
                }
            ),
            encoding="utf-8",
        )

        mock_cookies = [
            {
                "domain": ".google.com",
                "name": name,
                "value": f"{name}-value",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            }
            for name in ("SID", "APISID", "SAPISID", "__Secure-1PSIDTS")
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session_cmd._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])

        assert result.exit_code == 0, result.output
        # Account metadata must be gone so subsequent token fetches don't keep
        # routing to the old account, while unrelated notebook context survives.
        assert json.loads((tmp_path / "context.json").read_text()) == {"notebook_id": "nb_existing"}


# =============================================================================
# Chromium multi-user-profile fan-out (issue #571)
# =============================================================================
