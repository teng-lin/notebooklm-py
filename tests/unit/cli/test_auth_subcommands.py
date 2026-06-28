"""Tests for the ``auth`` subgroup (check, logout, refresh, inspect) and the ``--browser-cookies`` login path.

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import json
import stat
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import notebooklm.auth as auth_module
import notebooklm.cli._firefox_containers as firefox_containers
import notebooklm.cli.services.session_context as _sc
from notebooklm.notebooklm_cli import cli
from tests._fixtures import patch_session_login_dual

from ._session_helpers import (
    _multiaccount_rookiepy_mock,
    _read_account,
)


def _valid_cookie_export(extra_cookies=None):
    cookies = [
        {"name": "SID", "value": "fixture-sid", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "fixture-psidts",
            "domain": ".google.com",
            "path": "/",
        },
        {"name": "APISID", "value": "fixture-apisid", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "fixture-sapisid", "domain": ".google.com", "path": "/"},
    ]
    if extra_cookies:
        cookies.extend(extra_cookies)
    return cookies


class TestAuthImportCookiesCommand:
    """Tests for the 'auth import-cookies' command."""

    def test_import_cookies_accepts_bare_cookie_list_and_storage_override(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(
            json.dumps(
                _valid_cookie_export(
                    [
                        {
                            "name": "UNRELATED",
                            "value": "should-not-persist",
                            "domain": ".example.com",
                            "path": "/",
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        assert "imported" in result.output
        stored = json.loads(storage_path.read_text(encoding="utf-8"))
        stored_names = {cookie["name"] for cookie in stored["cookies"]}
        assert {"SID", "__Secure-1PSIDTS", "APISID", "SAPISID"} <= stored_names
        assert "UNRELATED" not in stored_names

    def test_import_cookies_accepts_playwright_storage_state_from_stdin(self, runner, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        payload = {"cookies": _valid_cookie_export(), "origins": []}

        result = runner.invoke(
            cli,
            ["--storage", str(storage_path), "auth", "import-cookies", "-", "--json"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["success"] is True
        assert output["cookie_count"] == 4
        assert json.loads(storage_path.read_text(encoding="utf-8"))["cookies"]

    def test_import_cookies_drops_origins_from_playwright_storage_state(self, runner, tmp_path):
        input_path = tmp_path / "playwright-storage-state.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(
            json.dumps(
                {
                    "cookies": _valid_cookie_export(),
                    "origins": [
                        {
                            "origin": "https://evil.example.com",
                            "localStorage": [{"name": "token", "value": "do-not-persist"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        stored = json.loads(storage_path.read_text(encoding="utf-8"))
        assert stored["cookies"]
        assert stored["origins"] == []

    def test_import_cookies_rejects_env_auth_json_interlock(self, runner, tmp_path, monkeypatch):
        input_path = tmp_path / "cookies.json"
        input_path.write_text(json.dumps(_valid_cookie_export()), encoding="utf-8")
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps({"cookies": []}))

        result = runner.invoke(cli, ["auth", "import-cookies", str(input_path)])

        assert result.exit_code != 0
        assert "auth import-cookies" in result.output
        assert "NOTEBOOKLM_AUTH_JSON" in result.output

    def test_import_cookies_rejects_malformed_json(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text("{not valid json", encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "Invalid JSON" in result.output
        assert not storage_path.exists()

    def test_import_cookies_rejects_unsupported_json_shape(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(json.dumps({"not_cookies": []}), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "Cookie JSON must be either" in result.output
        assert not storage_path.exists()

    def test_import_cookies_include_domains_opts_into_sibling_product_cookies(
        self, runner, tmp_path
    ):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(
            json.dumps(
                _valid_cookie_export(
                    [
                        {
                            "name": "DOCS_PREF",
                            "value": "docs-cookie",
                            "domain": "docs.google.com",
                            "path": "/",
                        }
                    ]
                )
            ),
            encoding="utf-8",
        )

        result_default = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result_default.exit_code == 0, result_default.output
        default_names = {
            cookie["name"]
            for cookie in json.loads(storage_path.read_text(encoding="utf-8"))["cookies"]
        }
        assert "DOCS_PREF" not in default_names

        result_optin = runner.invoke(
            cli,
            [
                "--storage",
                str(storage_path),
                "auth",
                "import-cookies",
                str(input_path),
                "--include-domains",
                "docs",
            ],
        )

        assert result_optin.exit_code == 0, result_optin.output
        optin_names = {
            cookie["name"]
            for cookie in json.loads(storage_path.read_text(encoding="utf-8"))["cookies"]
        }
        assert "DOCS_PREF" in optin_names

    def test_import_cookies_sets_private_file_and_directory_permissions(self, runner, tmp_path):
        if sys.platform == "win32":
            pytest.skip("POSIX permission bits are not stable on Windows")
        input_path = tmp_path / "cookies.json"
        auth_dir = tmp_path / "profile"
        storage_path = auth_dir / "storage_state.json"
        input_path.write_text(json.dumps(_valid_cookie_export()), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(storage_path.stat().st_mode) == 0o600

    def test_import_cookies_rejects_empty_required_cookie_values(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        cookies = _valid_cookie_export()
        for cookie in cookies:
            if cookie["name"] == "SID":
                cookie["value"] = ""
        input_path.write_text(json.dumps(cookies), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "Required cookies must have non-empty string values" in result.output
        assert "SID" in result.output
        assert not storage_path.exists()

    def test_import_cookies_rejects_missing_required_cookies_without_leaking_values(
        self, runner, tmp_path
    ):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        secret_value = "super-secret-cookie-value"
        input_path.write_text(
            json.dumps(
                [
                    {
                        "name": "APISID",
                        "value": secret_value,
                        "domain": ".google.com",
                        "path": "/",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "Missing required cookies" in result.output
        assert secret_value not in result.output
        assert not storage_path.exists()

    def test_import_cookies_backs_up_existing_storage_state(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        input_path.write_text(json.dumps(_valid_cookie_export()), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        backup_path = storage_path.with_name(storage_path.name + ".bak")
        assert backup_path.exists(), "previous storage_state should be backed up"
        # The backup holds the PRIOR contents; the live file holds the import.
        assert json.loads(backup_path.read_text(encoding="utf-8"))["cookies"] == []
        assert json.loads(storage_path.read_text(encoding="utf-8"))["cookies"]
        assert "backed up to" in result.output
        if sys.platform != "win32":
            # The .bak holds credentials too — it must be private (0o600).
            assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600

    def test_import_cookies_no_backup_when_target_absent(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(json.dumps(_valid_cookie_export()), encoding="utf-8")

        result = runner.invoke(
            cli,
            ["--storage", str(storage_path), "auth", "import-cookies", str(input_path), "--json"],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["backup_path"] is None
        assert not storage_path.with_name(storage_path.name + ".bak").exists()

    def test_import_cookies_forces_secure_on_secure_prefixed_cookie(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        # __Secure-1PSIDTS arrives with secure omitted (bare-list export style).
        cookies = [c for c in _valid_cookie_export() if c["name"] != "__Secure-1PSIDTS"]
        cookies.append(
            {
                "name": "__Secure-1PSIDTS",
                "value": "fixture-psidts",
                "domain": ".google.com",
                "path": "/",
                "secure": False,
            }
        )
        input_path.write_text(json.dumps(cookies), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        stored = json.loads(storage_path.read_text(encoding="utf-8"))
        secure_cookie = next(c for c in stored["cookies"] if c["name"] == "__Secure-1PSIDTS")
        assert secure_cookie["secure"] is True

    def test_import_cookies_rejects_present_but_empty_secondary_binding(self, runner, tmp_path):
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        # SID + __Secure-1PSIDTS present and non-empty, but the secondary binding
        # (APISID/SAPISID) is present-with-empty values and there is no OSID:
        # the name-level check would pass, so the value-level guard must catch it.
        cookies = [
            {"name": "SID", "value": "fixture-sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "fixture-psidts",
                "domain": ".google.com",
                "path": "/",
            },
            {"name": "APISID", "value": "", "domain": ".google.com", "path": "/"},
            {"name": "SAPISID", "value": "", "domain": ".google.com", "path": "/"},
        ]
        input_path.write_text(json.dumps(cookies), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "present but have empty values" in result.output
        assert "OSID" in result.output
        assert not storage_path.exists()

    def test_import_cookies_allows_missing_secondary_binding(self, runner, tmp_path):
        # No secondary-binding cookie present at all: like the login flow (which
        # only warns), import-cookies must NOT hard-reject this — it persists.
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        cookies = [
            {"name": "SID", "value": "fixture-sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "fixture-psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ]
        input_path.write_text(json.dumps(cookies), encoding="utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code == 0, result.output
        assert storage_path.exists()

    def test_import_cookies_rejects_non_object_cookie_entry(self, runner, tmp_path):
        # A bare list containing a non-object element must fail cleanly at the
        # normalization boundary, not crash in the downstream extractor.
        input_path = tmp_path / "cookies.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text(json.dumps([*_valid_cookie_export(), "not-an-object"]), "utf-8")

        result = runner.invoke(
            cli, ["--storage", str(storage_path), "auth", "import-cookies", str(input_path)]
        )

        assert result.exit_code != 0
        assert "must be a JSON object" in result.output
        assert not storage_path.exists()

    def test_import_cookies_json_error_output_is_json(self, runner, tmp_path):
        # The handle_errors(json_output=...) fix: a failure under --json must
        # render the JSON error envelope, not plain text.
        input_path = tmp_path / "bad.json"
        storage_path = tmp_path / "storage_state.json"
        input_path.write_text("not valid json", encoding="utf-8")

        result = runner.invoke(
            cli,
            ["--storage", str(storage_path), "auth", "import-cookies", str(input_path), "--json"],
        )

        assert result.exit_code != 0
        assert json.loads(result.output)["error"] is True  # parses as JSON


class TestAuthCheckCommand:
    """Tests for the 'auth check' command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        """Provide a temporary storage path for testing."""
        storage_file = tmp_path / "storage_state.json"
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            yield storage_file

    def _write_valid_storage(self, path):
        """A storage_state that passes every local check, with in-band account."""
        path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": n, "value": f"{n}-v", "domain": ".google.com", "path": "/"}
                        for n in ("SID", "__Secure-1PSIDTS", "APISID", "SAPISID")
                    ],
                    "notebooklm": {"account": {"email": "you@gmail.com", "authuser": 0}},
                }
            ),
            encoding="utf-8",
        )

    def test_auth_check_identity_fields_parity_rich_vs_json(self, runner, mock_storage_path):
        """Every identity/location fact is shown in BOTH the table and --json,
        sourced from one result so the two surfaces never disagree (issue #1640)."""
        self._write_valid_storage(mock_storage_path)
        auth_module.write_master_token(
            mock_storage_path.with_name("master_token.json"),
            email="you@gmail.com",
            master_token="aas_et/secret",
            android_id="0123456789abcdef",
        )
        master_path = str(mock_storage_path.with_name("master_token.json"))

        json_result = runner.invoke(cli, ["auth", "check", "--json"])
        assert json_result.exit_code == 0, json_result.output
        payload = json.loads(json_result.output)

        # JSON exposes the identity facts at top level.
        assert payload["account"]["email"] == "you@gmail.com"
        assert payload["storage_path"] == str(mock_storage_path)
        assert payload["master_token"]["path"] == master_path
        assert payload["master_token"]["present"] is True
        assert payload["psidts"]["present"] is True

        rich_result = runner.invoke(cli, ["auth", "check"])
        assert rich_result.exit_code == 0, rich_result.output
        text = rich_result.output

        # Parity: each identity fact in the JSON also appears in the table. Paths
        # can wrap in the Rich table, so compare on the filename, not the full
        # path string.
        assert payload["account"]["email"] in text
        assert "master_token.json" in text
        assert "__Secure-1PSIDTS" in text
        assert "Account" in text and "Master token" in text

    def test_auth_check_test_json_includes_live_notebook_count(self, runner, tmp_path):
        """notebook_count flows through the REAL command wiring on --test --json.

        Regression for the nested-event-loop bug: the count probe must run from
        sync context (after run_auth_check's loop closes), not inside it, or
        run_async would raise and the count would silently be null. Uses an
        explicit --storage path so both the core check and the probe's auth
        loader resolve the same file.
        """
        storage = tmp_path / "storage_state.json"
        self._write_valid_storage(storage)

        class _FakeNotebooks:
            async def list(self):
                return [object(), object(), object()]  # 3 notebooks

        class _FakeClient:
            notebooks = _FakeNotebooks()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def factory(auth=None, **kwargs):
            return _FakeClient()

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_token", "session_id")
            result = runner.invoke(
                cli,
                ["--storage", str(storage), "auth", "check", "--test", "--json"],
                obj={"client_factory": factory},
            )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["notebook_count"] == 3

    def test_auth_check_never_leaks_secret_values(self, runner, tmp_path):
        """auth check must surface identity (names, domains, paths, email) but
        NEVER a secret value — no cookie values, no master_token value — in either
        the Rich table or --json. Security invariant for issue #1640."""
        storage = tmp_path / "storage_state.json"
        secrets = {
            "SID": "SID_SECRET_VALUE_abc123",
            "__Secure-1PSIDTS": "PSIDTS_SECRET_VALUE_xyz789",
            "APISID": "APISID_SECRET_VALUE",
            "SAPISID": "SAPISID_SECRET_VALUE",
        }
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": n, "value": v, "domain": ".google.com", "path": "/"}
                        for n, v in secrets.items()
                    ],
                    "notebooklm": {"account": {"email": "you@gmail.com", "authuser": 0}},
                }
            ),
            encoding="utf-8",
        )
        master_secret = "aas_et/MASTER_TOKEN_SECRET_DO_NOT_LEAK"
        auth_module.write_master_token(
            storage.with_name("master_token.json"),
            email="you@gmail.com",
            master_token=master_secret,
            android_id="0123456789abcdef",
        )
        forbidden = [master_secret, *secrets.values()]

        for args in (
            ["--storage", str(storage), "auth", "check"],
            ["--storage", str(storage), "auth", "check", "--json"],
        ):
            result = runner.invoke(cli, args)
            assert result.exit_code == 0, result.output
            leaked = [s for s in forbidden if s in result.output]
            assert not leaked, f"auth check leaked secret value(s) {leaked} via {args}"

    def test_auth_check_master_token_psidts_hint(self, runner, mock_storage_path):
        """Missing PSIDTS on a master-token profile shows the corrected guidance,
        not the browser-extraction / App-Bound Encryption hint."""
        # SID + secondary binding but no __Secure-1PSIDTS.
        mock_storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": n, "value": f"{n}-v", "domain": ".google.com", "path": "/"}
                        for n in ("SID", "APISID", "SAPISID")
                    ]
                }
            ),
            encoding="utf-8",
        )
        auth_module.write_master_token(
            mock_storage_path.with_name("master_token.json"),
            email="you@gmail.com",
            master_token="aas_et/secret",
            android_id="0123456789abcdef",
        )

        result = runner.invoke(cli, ["auth", "check", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "master_token.json is present" in payload["details"]["error"]
        assert "App-Bound Encryption" not in payload["details"]["error"]

    def test_auth_check_storage_not_found(self, runner, mock_storage_path):
        """Test auth check when storage file doesn't exist."""
        # Ensure file doesn't exist
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check"])

        # Failed check ⇒ non-zero exit in text mode too (issue #1569).
        assert result.exit_code != 0
        assert "Storage exists" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_storage_not_found_json(self, runner, mock_storage_path):
        """Test auth check --json when storage file doesn't exist.

        Spec: failure paths in --json mode must exit nonzero so automation
        can fail-fast on `notebooklm auth check --json`.
        """
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is False
        assert "not found" in output["details"]["error"]

    def test_auth_check_invalid_json(self, runner, mock_storage_path):
        """Test auth check when storage file contains invalid JSON."""
        mock_storage_path.write_text("{ invalid json }")

        result = runner.invoke(cli, ["auth", "check"])

        # Failed check ⇒ non-zero exit in text mode too (issue #1569).
        assert result.exit_code != 0
        assert "JSON valid" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_invalid_json_output(self, runner, mock_storage_path):
        """Test auth check --json when storage contains invalid JSON.

        Spec: failure paths in --json mode must exit nonzero.
        """
        mock_storage_path.write_text("not valid json at all")

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is False
        assert "Invalid JSON" in output["details"]["error"]

    def test_auth_check_unreadable_storage_text_mode(self, runner, mock_storage_path):
        """`auth check` when storage exists but cannot be read (OSError).

        Regression test for the bug where ``auth check`` caught only
        ``json.JSONDecodeError``, so an ``OSError`` raised by
        ``storage_path.read_text(...)`` (e.g. permission denied, or path
        is a directory) leaked as a raw Python traceback instead of
        being reported through the structured ``_output_auth_check``
        renderer.

        Repro: replace the storage file with a directory. Reading a
        directory as text raises ``IsADirectoryError``, a subclass of
        ``OSError``.

        Contract: text mode shows the checks table (no traceback) and exits
        non-zero on the failed ``json_valid`` check, matching --json mode and
        the invalid-JSON case (issue #1569).
        """
        # The fixture yields a path under tmp_path but does not create the
        # file. Make the path a directory so `read_text` raises
        # IsADirectoryError instead of FileNotFoundError.
        if mock_storage_path.exists():
            mock_storage_path.unlink()
        mock_storage_path.mkdir()

        result = runner.invoke(cli, ["auth", "check"])

        # Failed check ⇒ non-zero exit, but via a clean SystemExit — no
        # traceback should leak to the caller.
        assert result.exit_code != 0, (
            f"expected non-zero text-mode exit on failed check: "
            f"stdout={result.output!r} exc={result.exception!r}"
        )
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"unhandled exception leaked: {result.exception!r}"
        )
        # Structured renderer ran — checks table visible.
        assert "JSON valid" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_unreadable_storage_json_mode(self, runner, mock_storage_path):
        """`auth check --json` when storage exists but cannot be read (OSError).

        Acceptance criterion (plan P1.T3): "auth check --json with an
        unreadable storage file emits a structured JSON error (not a
        raw traceback) and exits non-zero."
        """
        if mock_storage_path.exists():
            mock_storage_path.unlink()
        mock_storage_path.mkdir()

        result = runner.invoke(cli, ["auth", "check", "--json"])

        # MUST exit non-zero for fail-fast automation.
        assert result.exit_code != 0, (
            f"--json mode silently exited 0 on unreadable storage: stdout={result.output!r}"
        )
        # Stdout MUST be pure parseable JSON, NOT a Python traceback.
        output = json.loads(result.output)
        assert output["status"] == "error"
        # Storage exists (directory does exist), but JSON parsing fails.
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is False
        # An error message must be present so callers can log/diagnose.
        assert output["details"]["error"], f"empty error message on unreadable storage: {output!r}"

    def test_auth_check_non_utf8_storage_json_mode(self, runner, mock_storage_path):
        """`auth check --json` when storage exists but is not valid UTF-8.

        Same bug class as the OSError leak: ``read_text(encoding="utf-8")``
        raises ``UnicodeDecodeError`` (a ``ValueError`` subclass, NOT an
        ``OSError`` subclass) on a binary or corrupted storage file, and
        the original ``except json.JSONDecodeError`` clause never caught
        it. Without the broadened handler, the traceback would leak to
        stderr and stdout would be empty — breaking JSON-parsing callers.
        """
        # Write invalid UTF-8 bytes — a 0xff byte at position 0 is never
        # the start of a valid UTF-8 sequence.
        mock_storage_path.write_bytes(b"\xff\xfe\xfd not valid utf-8")

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0, (
            f"--json mode silently exited 0 on non-UTF-8 storage: stdout={result.output!r}"
        )
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is False
        assert output["details"]["error"], f"empty error message on non-UTF-8 storage: {output!r}"

    def test_auth_check_missing_sid_cookie(self, runner, mock_storage_path):
        """Test auth check when SID cookie is missing."""
        # Valid JSON but no SID cookie
        storage_data = {
            "cookies": [
                {"name": "OTHER", "value": "test", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        # Missing SID cookie is a failed check ⇒ non-zero exit (issue #1569).
        assert result.exit_code != 0
        assert "SID" in result.output or "cookie" in result.output.lower()

    def test_auth_check_valid_storage(self, runner, mock_storage_path):
        """Test auth check with valid storage containing SID."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "pass" in result.output.lower() or "✓" in result.output
        assert "Authentication is valid" in result.output

    def test_auth_check_valid_storage_json(self, runner, mock_storage_path):
        """Test auth check --json with valid storage."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is True
        assert output["checks"]["cookies_present"] is True
        assert output["checks"]["sid_cookie"] is True
        assert "SID" in output["details"]["cookies_found"]

    def test_auth_check_missing_1psidts_surfaces_tier1_error(self, runner, mock_storage_path):
        """SID present but ``__Secure-1PSIDTS`` absent must surface the Tier 1 error.

        Pinned by the #371 two-tier pre-flight: ``MINIMUM_REQUIRED_COOKIES``
        now contains both ``SID`` and ``__Secure-1PSIDTS``; the load helpers
        in ``auth.py`` raise on absence, and ``auth check`` reports the raised
        ``ValueError`` so users see the new diagnostic.

        The fix closes the previous exit-code gap: ``auth check --json`` now exits
        nonzero whenever it reports ``status="error"``.
        """
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                # Note: __Secure-1PSIDTS deliberately omitted.
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["cookies_present"] is False
        assert "__Secure-1PSIDTS" in output["details"].get("error", "")

    def test_auth_check_with_test_flag_success(self, runner, mock_storage_path):
        """Test auth check --test with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_token_abc", "session_id_xyz")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        assert result.exit_code == 0
        assert "Token fetch" in result.output
        assert "pass" in result.output.lower() or "✓" in result.output

    def test_auth_check_with_test_flag_failure(self, runner, mock_storage_path):
        """Test auth check --test when token fetch fails."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        # Text mode must exit non-zero on a failed executed check, matching
        # --json mode, so unattended automation can fail-fast (issue #1569).
        assert result.exit_code != 0
        assert "Token fetch" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output
        assert "expired" in result.output.lower() or "refresh" in result.output.lower()

    def test_auth_check_with_test_flag_json(self, runner, mock_storage_path):
        """Test auth check --test --json with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_12345", "sess_67890")

            result = runner.invoke(cli, ["auth", "check", "--test", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["token_fetch"] is True
        assert output["details"]["csrf_length"] == 10
        assert output["details"]["session_id_length"] == 10

    def test_auth_check_passive_uses_passive_fetch(self, runner, mock_storage_path):
        """``--test --passive`` routes through the read-only passive fetch.

        The passive path must NOT touch ``fetch_tokens_with_domains`` (which
        runs NOTEBOOKLM_REFRESH_CMD, rotates cookies, and persists to disk).
        Issue #1569: a readiness probe must be side-effect-free.
        """
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data), encoding="utf-8")

        with (
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_active,
        ):
            mock_passive.return_value = ("csrf_token_abc", "session_id_xyz")

            result = runner.invoke(cli, ["auth", "check", "--test", "--passive"])

        assert result.exit_code == 0
        mock_passive.assert_awaited_once()
        mock_active.assert_not_called()
        assert "Token fetch" in result.output
        assert "pass" in result.output.lower() or "✓" in result.output

    def test_auth_check_passive_failure_exits_nonzero(self, runner, mock_storage_path):
        """``--test --passive`` still fails loud (non-zero) when the probe fails."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data), encoding="utf-8")

        with patch.object(
            auth_module, "fetch_tokens_passive", new_callable=AsyncMock
        ) as mock_passive:
            mock_passive.side_effect = ValueError("Authentication expired")

            result = runner.invoke(cli, ["auth", "check", "--test", "--passive", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["token_fetch"] is False

    def test_auth_check_passive_without_test_warns_no_effect(self, runner, mock_storage_path):
        """``--passive`` without ``--test`` is a no-op on already-passive local
        checks; warn (not fail) so the caller is not misled."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data), encoding="utf-8")

        result = runner.invoke(cli, ["auth", "check", "--passive"])

        # Still succeeds (local checks all pass), but the note is surfaced.
        assert result.exit_code == 0
        assert "no effect without --test" in result.output

    def test_auth_check_env_var_takes_precedence(self, runner, mock_storage_path, monkeypatch):
        """Test auth check uses NOTEBOOKLM_AUTH_JSON when set."""
        # Even if storage file doesn't exist, env var should work
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        env_storage = {
            "cookies": [
                {"name": "SID", "value": "env_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["details"]["auth_source"] == "NOTEBOOKLM_AUTH_JSON"

    def test_auth_check_passive_with_env_auth_passes_none_path(
        self, runner, mock_storage_path, monkeypatch
    ):
        """``--test --passive`` under NOTEBOOKLM_AUTH_JSON routes the passive
        probe with ``token_path=None`` (read-from-env), like the active path."""
        if mock_storage_path.exists():
            mock_storage_path.unlink()
        env_storage = {
            "cookies": [
                {"name": "SID", "value": "env_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        with patch.object(
            auth_module, "fetch_tokens_passive", new_callable=AsyncMock
        ) as mock_passive:
            mock_passive.return_value = ("csrf_env", "session_env")

            result = runner.invoke(cli, ["auth", "check", "--test", "--passive", "--json"])

        assert result.exit_code == 0
        # has_env_auth ⇒ token_path is None (the env-var read signal), profile arg follows.
        mock_passive.assert_awaited_once()
        assert mock_passive.await_args.args[0] is None
        output = json.loads(result.output)
        assert output["checks"]["token_fetch"] is True

    def test_auth_check_shows_cookie_domains(self, runner, mock_storage_path):
        """Test auth check displays cookie domains."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "NID", "value": "test_nid", "domain": ".google.com.sg"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        # Use ``==`` membership rather than ``in`` to keep CodeQL's
        # ``py/incomplete-url-substring-sanitization`` rule from flagging a
        # false positive — ``cookie_domains`` is a list, not a URL string.
        assert any(d == ".google.com" for d in output["details"]["cookie_domains"])

    def test_auth_check_shows_cookies_by_domain(self, runner, mock_storage_path):
        """Test auth check --json includes detailed cookies_by_domain."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSID", "value": "secure1", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        cookies_by_domain = output["details"]["cookies_by_domain"]

        # Verify .google.com has expected cookies. ``.get(...) is not None``
        # silences CodeQL's ``py/incomplete-url-substring-sanitization`` —
        # ``cookies_by_domain`` is a dict keyed by exact domain, not a URL
        # being substring-validated.
        assert cookies_by_domain.get(".google.com") is not None
        assert "SID" in cookies_by_domain[".google.com"]
        assert "HSID" in cookies_by_domain[".google.com"]
        assert "__Secure-1PSID" in cookies_by_domain[".google.com"]

        # Verify regional domain has its cookies
        assert cookies_by_domain.get(".google.com.sg") is not None
        assert "SID" in cookies_by_domain[".google.com.sg"]

    def test_auth_check_skipped_token_fetch_shown(self, runner, mock_storage_path):
        """Test auth check shows token fetch as skipped when --test not used."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["checks"]["token_fetch"] is None  # Not tested

    def test_auth_check_help(self, runner):
        """Test auth check --help shows usage information."""
        result = runner.invoke(cli, ["auth", "check", "--help"])

        assert result.exit_code == 0
        assert "Check authentication status" in result.output
        assert "--test" in result.output
        assert "--json" in result.output


# =============================================================================
# LOGIN LANGUAGE SYNC TESTS
# =============================================================================


class TestLoginBrowserCookies:
    """Tests for notebooklm login --browser-cookies."""

    def test_browser_cookies_in_help(self, runner):
        """--browser-cookies appears in login --help."""
        result = runner.invoke(cli, ["login", "--help"])
        assert "--browser-cookies" in result.output

    def test_rookiepy_not_installed_shows_error(self, runner):
        """Shows helpful error when rookiepy is not installed."""
        with patch.dict(sys.modules, {"rookiepy": None}):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "rookiepy" in result.output
        assert "pip install" in result.output

    def test_auto_detect_calls_rookiepy_load(self, runner, tmp_path):
        """Auto-detect calls rookiepy.load()."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.load.assert_called_once()

    def test_named_browser_calls_rookiepy_function(self, runner, tmp_path):
        """Named browser calls the matching rookiepy function."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.chrome = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.chrome.assert_called_once()
        mock_sync.assert_called_once_with(storage_path=storage_file, profile=None)

    def test_no_google_cookies_shows_error(self, runner, tmp_path):
        """Shows error when no Google cookies found."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=[])

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "SID" in result.output or "Google" in result.output

    def test_locked_db_shows_close_browser_hint(self, runner, tmp_path):
        """Shows close-browser hint when DB is locked."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(side_effect=OSError("database is locked"))

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "close" in output_lower or "browser" in output_lower

    def test_cookies_saved_to_storage_file(self, runner, tmp_path):
        """Cookies are written to storage_state.json."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "mysid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "APISID",
                "value": "apisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "SAPISID",
                "value": "sapisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "mysid" for c in data["cookies"])

    def test_unknown_browser_shows_error(self, runner, tmp_path):
        """Unknown browser name shows a clear error."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(
            side_effect=AttributeError("module has no attribute 'netscape'")
        )

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "netscape"])
        assert result.exit_code != 0

    # ------------------------------------------------------------------
    # firefox::<container> syntax (issue #367)
    # ------------------------------------------------------------------

    def test_firefox_container_syntax_invokes_extractor(self, runner, tmp_path):
        """``--browser-cookies firefox::<name>`` calls the container extractor.

        rookiepy must NOT be touched on this path — that's the whole point
        of the bypass.
        """
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "work_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        mock_rookiepy = MagicMock()
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch.object(
                firefox_containers,
                "resolve_container_id",
                return_value=2,
            ),
            patch.object(
                firefox_containers,
                "extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code == 0, result.output
        mock_extract.assert_called_once()
        # rookiepy must NOT have been called for the firefox:: path.
        mock_rookiepy.firefox.assert_not_called()
        mock_rookiepy.load.assert_not_called()
        # The container's SID should land in the saved storage state.
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "work_sid" for c in data["cookies"])

    def test_firefox_container_none_passes_literal_none(self, runner, tmp_path):
        """``firefox::none`` resolves to ``"none"`` and skips rookiepy."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "default_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch.object(
                firefox_containers,
                "extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::none"])
        assert result.exit_code == 0, result.output
        # Confirm the extractor was called with the ``"none"`` sentinel.
        _, kwargs = mock_extract.call_args
        positional = mock_extract.call_args.args
        # signature: extract_firefox_container_cookies(profile, container_id, domains=…)
        assert positional[1] == "none" or kwargs.get("container_id") == "none"

    def test_firefox_container_unknown_name_shows_listing(self, runner, tmp_path):
        """Unknown container name shows a helpful error and exits non-zero."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch.object(
                firefox_containers,
                "resolve_container_id",
                side_effect=ValueError(
                    "Firefox container 'Nope' not found. Available containers: 'Work', 'Personal'."
                ),
            ),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Nope"])
        assert result.exit_code != 0
        assert "Nope" in result.output
        assert "Work" in result.output

    def test_firefox_container_no_firefox_profile_shows_error(self, runner, tmp_path):
        """Missing Firefox install shows a friendly error, not a stack trace."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=None,
            ),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code != 0
        # The message should mention firefox / profile so the user knows what's up.
        out_lower = result.output.lower()
        assert "firefox" in out_lower
        assert "profile" in out_lower

    def test_firefox_empty_container_spec_rejected(self, runner, tmp_path):
        """`--browser-cookies firefox::` (empty spec) must error, not silently
        fall through to the unfiltered merge this feature exists to prevent.
        Regression guard for the polish review (3-way HIGH consensus).
        """
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::"])
        assert result.exit_code != 0
        assert "Empty Firefox container specifier" in result.output
        # The error should point at the correct syntax so the user can recover.
        assert "firefox::none" in result.output
        assert "container-name" in result.output

    def test_unscoped_firefox_warns_when_containers_in_use(self, runner, tmp_path):
        """Unscoped ``firefox`` emits a yellow warning if containers are in use."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch.object(
                firefox_containers,
                "has_container_cookies_in_use",
                return_value=True,
            ),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        # Rich may wrap the message; assert on substrings that survive wrap.
        assert "Multi-Account" in result.output
        assert "firefox::" in result.output

    def test_unscoped_firefox_no_warning_when_no_containers(self, runner, tmp_path):
        """No warning when the profile is not actually using containers."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch.object(
                firefox_containers,
                "find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch.object(
                firefox_containers,
                "has_container_cookies_in_use",
                return_value=False,
            ),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        assert "Multi-Account" not in result.output


# =============================================================================
# AUTH LOGOUT COMMAND TESTS
# =============================================================================


class TestAuthLogoutCommand:
    def test_auth_logout_deletes_storage_and_browser_profile(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Test auth logout deletes both storage_state.json and browser_profile/."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        mock_context_file.write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@example.com"}})
        )
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()
        (browser_dir / "Default").mkdir()
        (browser_dir / "Default" / "Cookies").write_text("data")

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            result = runner.invoke(cli, ["auth", "logout"])
        mock_browser_dir.assert_called()

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()
        assert not mock_context_file.exists()
        assert not browser_dir.exists()

    def test_auth_logout_when_already_logged_out(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Test auth logout is a no-op with friendly message when not logged in."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "browser_profile"
        # Neither exists

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 0
        assert "already" in result.output.lower() or "No active session" in result.output

    def test_auth_logout_partial_state_only_storage(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Test auth logout handles case where only storage_state.json exists."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # browser_dir does not exist

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()

    def test_auth_logout_handles_permission_error_on_rmtree(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Test auth logout handles locked browser profile gracefully."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            # ``execute_logout`` (in ``services.session_context``) owns the
            # browser-profile rmtree; patch the consumer module's ``shutil``
            # (#1367 removed the ``session_cmd`` stdlib re-export).
            patch.object(
                _sc.shutil,
                "rmtree",
                side_effect=OSError("sharing violation"),
            ) as mock_rmtree,
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        # ``assert_called`` is mandatory (plan failure-mode caveat #3): the
        # exit-1 assertion alone would false-green on a dead wrong-namespace
        # patch.
        mock_rmtree.assert_called_once()
        assert result.exit_code == 1
        assert "in use" in result.output.lower() or "Cannot" in result.output

    def test_auth_logout_handles_permission_error_on_unlink(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Test auth logout handles locked storage_state.json gracefully on Windows."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch.object(
                type(storage_file),
                "unlink",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 1
        assert "Cannot" in result.output or "in use" in result.output.lower()

    def test_auth_logout_clears_cached_notebook_context(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Logout must remove context.json so the next command does not reuse
        notebook_id / conversation_id from the previous account.

        Issues #114 / #294 surfaced as "not found" / permission errors after an
        account switch. The PR's account-mismatch hint steers users to
        logout→login as the fix; the flow only works if context is actually
        cleared on logout.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        # Simulate cached notebook / conversation from a previous session.
        mock_context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "old-account-notebook",
                    "conversation_id": "old-account-conversation",
                }
            )
        )
        assert mock_context_file.exists()

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not mock_context_file.exists()

    def test_auth_logout_no_context_file_does_not_error(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Logout must tolerate a missing context.json without erroring.

        clear_context() is a no-op when the file does not exist; assert that
        the main logout path still succeeds.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No context file, no browser dir.

        assert not mock_context_file.exists()

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_auth_logout_handles_os_error_on_context_unlink(
        self, runner, tmp_path, mock_context_file, monkeypatch
    ):
        """Logout must surface an OSError on context.json removal as SystemExit(1).

        Parity with the existing handlers for storage_state.json and the browser
        profile: a locked/unwritable context file should produce a clean
        diagnostic message, not an unhandled traceback.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir — nothing to remove in that step.
        mock_context_file.write_text('{"notebook_id": "stale"}')

        mock_browser_dir = MagicMock(return_value=browser_dir)
        monkeypatch.setattr(_sc, "get_browser_profile_dir", mock_browser_dir)
        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch.object(
                _sc,
                "clear_context",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        mock_browser_dir.assert_called()
        assert result.exit_code == 1
        assert "context file" in result.output.lower()


# =============================================================================
# AUTH REFRESH COMMAND TESTS
# =============================================================================


class TestAuthRefreshCommand:
    """Tests for the 'auth refresh' one-shot keepalive command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            yield storage_file

    def test_auth_refresh_success(self, runner, mock_storage_path):
        """auth refresh exits 0 and prints `ok` on a successful token fetch."""
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 0
        assert "ok" in result.output.lower()
        mock_fetch.assert_awaited_once()

    def test_auth_refresh_json_success(self, runner, mock_storage_path):
        """--json emits a single structured keepalive result on stdout."""
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["verified"] is False

    def test_auth_refresh_json_verify_success(self, runner, mock_storage_path):
        """``--json --verify`` success emits a single document with verified=True —
        the human '[green]ok[/green] verified' line must NOT leak onto stdout."""
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            mock_passive.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--verify", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)  # raises if a stray line preceded the JSON
        assert payload["status"] == "ok"
        assert payload["verified"] is True
        assert "verified:" not in result.stdout  # no human line leaked

    def test_auth_refresh_json_with_browser_cookies_is_refused(self, runner, mock_storage_path):
        """``--json`` + ``--browser-cookies`` returns the error envelope, never the
        interactive login-IO output (which writes Rich text to stdout and would
        corrupt the single-JSON-document contract)."""
        result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "json_unsupported_with_browser_cookies"

    def test_auth_refresh_quiet_suppresses_success_output(self, runner, mock_storage_path):
        """--quiet keeps stdout clean when refresh succeeds (cron-friendly)."""
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--quiet"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_auth_refresh_failure_exits_nonzero(self, runner, mock_storage_path):
        """Token fetch failure exits non-zero with a friendly message — picked
        up by cron logs.

        The command body is wrapped in ``handle_errors``, so an
        unexpected ``ValueError`` flows through the UNEXPECTED_ERROR branch
        (exit 2) and the user sees a friendly 'Unexpected error: <msg>' line
        rather than a Python traceback.
        """
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired or invalid.")
            result = runner.invoke(cli, ["auth", "refresh"])
        # Exit code 2 per error_handler.py policy for unexpected errors.
        assert result.exit_code == 2
        # The original message is still surfaced verbatim, so cron logs keep
        # the diagnostic content.
        assert "authentication expired" in result.output.lower()
        # No Python traceback in stdout/stderr.
        assert "Traceback (most recent call last)" not in result.output

    def test_auth_refresh_failure_does_not_print_exception_class(self, runner, mock_storage_path):
        """``auth refresh`` no longer leaks ``type(exc).__name__`` into the
        user-facing message. The previous code path produced
        ``Error: ConnectTimeout: `` (with class name), which is implementation
        detail leakage. ``handle_errors`` produces ``Unexpected error: <msg>``
        instead.

        Regression guard for the error-handler polish.
        """
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = httpx.ConnectTimeout("")  # empty message
            result = runner.invoke(cli, ["auth", "refresh"])
        # Non-zero exit, friendly handler, no traceback.
        assert result.exit_code == 2
        assert "Traceback (most recent call last)" not in result.output
        # Critical: no ``ConnectTimeout`` class name in output.
        assert "ConnectTimeout" not in result.output, (
            f"auth refresh must not leak exception class names; got: {result.output!r}"
        )
        # And no ``Error: <ClassName>:`` leak pattern from the old code path.
        assert "Error: ConnectTimeout" not in result.output
        # A friendly Unexpected-error line should still appear.
        assert "Unexpected error" in result.output

    def test_auth_refresh_browser_cookies_failure_uses_typed_handler(
        self, runner, mock_storage_path
    ):
        """The ``--browser-cookies`` failure path also flows through
        ``handle_errors`` — same polish guarantee as the keepalive path.

        Previously the browser-cookies branch had its own bespoke
        ``except Exception: click.echo(f"Error: {type(exc).__name__}: ...")``
        block; it now relies on the wrapping ``with handle_errors():``.
        """
        with patch_session_login_dual("_refresh_from_browser_cookies") as mock_refresh:
            mock_refresh.side_effect = RuntimeError("rookiepy could not read cookies")
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])
        assert result.exit_code == 2  # unexpected error per error_handler policy
        assert "Traceback (most recent call last)" not in result.output
        # No leaked ``RuntimeError`` class name.
        assert "RuntimeError" not in result.output
        assert "Error: RuntimeError" not in result.output
        # Friendly Unexpected-error message + the original detail.
        assert "Unexpected error" in result.output
        assert "rookiepy could not read cookies" in result.output

    def test_auth_refresh_verify_success(self, runner, mock_storage_path):
        """``--verify`` runs a passive token fetch after refresh; exit 0 on success."""
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            mock_passive.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--verify"])
        assert result.exit_code == 0
        mock_passive.assert_awaited_once()
        assert "verified" in result.output.lower()

    def test_auth_refresh_verify_failure_exits_nonzero(self, runner, mock_storage_path):
        """Refresh can succeed while the post-refresh token fetch still fails.

        ``--verify`` makes that fail loud (exit 1) so a scheduler can rely on
        the exit code rather than trusting refresh success alone (issue #1569).
        """
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            mock_passive.side_effect = ValueError("Authentication expired or invalid.")
            result = runner.invoke(cli, ["auth", "refresh", "--verify"])
        assert result.exit_code == 1
        assert "post-refresh token fetch failed" in result.output.lower()
        assert "Traceback (most recent call last)" not in result.output

    def test_auth_refresh_verify_failure_json_envelope(self, runner, mock_storage_path):
        """``--verify`` failure under ``--json`` emits the error envelope on stdout
        (exit 1), not a human stderr line — the json contract holds on this path."""
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            mock_passive.side_effect = ValueError("Authentication expired or invalid.")
            result = runner.invoke(cli, ["auth", "refresh", "--verify", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "post_refresh_token_fetch_failed"

    def test_auth_refresh_verify_after_browser_cookies(self, runner, mock_storage_path):
        """``--verify`` also gates the ``--browser-cookies`` rewrite path."""
        with (
            patch_session_login_dual("_refresh_from_browser_cookies"),
            patch.object(
                auth_module, "fetch_tokens_passive", new_callable=AsyncMock
            ) as mock_passive,
        ):
            mock_passive.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(
                cli, ["auth", "refresh", "--browser-cookies", "chrome", "--verify"]
            )
        assert result.exit_code == 0
        mock_passive.assert_awaited_once()

    def test_auth_refresh_rejects_env_var_auth(self, runner, monkeypatch, mock_storage_path):
        """NOTEBOOKLM_AUTH_JSON has no writable backing store; refreshing it
        would silently rotate SIDTS but persist nothing. Refuse loudly."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 1
        assert "NOTEBOOKLM_AUTH_JSON" in result.output
        assert "incompatible" in result.output.lower()
        # Critical: no token fetch should run when the env var is set —
        # otherwise we'd be doing a server-side rotation that gets lost.
        mock_fetch.assert_not_awaited()

    def test_auth_refresh_propagates_global_profile_flag(self, runner, tmp_path):
        """`notebooklm --profile work auth refresh` resolves the work profile.

        Guards against the launchd/cron case where the global -p flag must
        flow through ctx.obj into fetch_tokens_with_domains.
        """
        work_storage = tmp_path / "work_storage_state.json"
        work_storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "y", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        def fake_storage_path(profile=None):
            assert profile == "work", f"expected profile='work', got {profile!r}"
            return work_storage

        with (
            patch_session_login_dual("get_storage_path", side_effect=fake_storage_path),
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["--profile", "work", "auth", "refresh"])

        assert result.exit_code == 0, result.output
        # fetch_tokens_with_domains(path, profile) — verify the work profile
        # was threaded through to the auth layer.
        called_args = mock_fetch.call_args
        assert called_args.args[0] == work_storage
        assert called_args.args[1] == "work"

    def test_auth_refresh_browser_cookies_repairs_account_after_order_change(
        self, runner, tmp_path
    ):
        """If a browser account logs out and indices shift, match by email and
        rewrite context.json with the new internal account index."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="bob@gmail.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", return_value=storage),
            patch.object(auth_module, "enumerate_accounts", new=_enum),
            patch_session_login_dual("_sync_server_language_to_config") as mock_sync,
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf_ok", "session_ok"),
            ) as mock_fetch,
        ):
            result = runner.invoke(
                cli,
                ["--profile", "bob", "auth", "refresh", "--browser-cookies", "chrome"],
            )

        assert result.exit_code == 0, result.output
        assert "bob@gmail.com" in result.output
        assert "authuser" not in result.output
        assert _read_account(storage) == {
            "authuser": 0,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_awaited_once()
        mock_sync.assert_called_once_with(storage_path=storage, profile="bob")

    def test_auth_refresh_browser_cookies_fails_when_profile_email_signed_out(
        self, runner, tmp_path
    ):
        """A stored email is identity; if that account is absent from the browser,
        do not refresh the profile with a different signed-in account."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", return_value=storage),
            patch.object(auth_module, "enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        assert "bob@gmail.com" in result.output
        assert "not signed in" in result.output.lower()
        assert "alice@example.com" in result.output
        # In this test the storage file was pre-seeded with a sibling
        # context.json (legacy layout). The reader falls back to that record
        # because no in-band write has occurred — assertion stays unchanged.
        assert _read_account(storage) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_not_awaited()


# =============================================================================
# AUTH INSPECT + MULTI-ACCOUNT LOGIN TESTS (issue #359)
# =============================================================================


class TestAuthInspect:
    def test_injected_io_run_async_reaches_login_service_helper(self):
        """The injected ``LoginIO`` sink's ``run_async`` drives the probe (#1393).

        Previously ``_enumerate_one_jar`` bound a module-level ``run_async``;
        the async bridge is now the injected sink's ``run_async``. This pins
        that the helper routes the account-enumeration probe through the sink.
        """
        from notebooklm.auth import Account
        from notebooklm.cli.services.login import _enumerate_one_jar
        from tests._fixtures.login_io import make_recording_io

        raw_cookies = _multiaccount_rookiepy_mock().chrome.return_value
        accounts = [Account(authuser=0, email="alice@example.com", is_default=True)]

        def fake_run_async(awaitable):
            awaitable.close()
            return accounts

        io = make_recording_io(run_async=MagicMock(side_effect=fake_run_async))
        with patch.object(auth_module, "enumerate_accounts", return_value=object()):
            result = _enumerate_one_jar(raw_cookies, "chrome", browser_profile=None, io=io)

        assert result == accounts
        io.run_async.assert_called_once()

    def test_enumerate_one_jar_network_error_non_quiet_exits_without_reraising(self):
        from notebooklm.cli.services.login import _enumerate_one_jar
        from notebooklm.cli.services.login.outcomes import NetworkFailure

        raw_cookies = _multiaccount_rookiepy_mock().chrome.return_value

        async def fail_enumerate(*args, **kwargs):
            raise httpx.RequestError("offline")

        with patch.object(auth_module, "enumerate_accounts", new=fail_enumerate):
            result = _enumerate_one_jar(raw_cookies, "chrome", browser_profile=None)

        assert isinstance(result, NetworkFailure)
        message = result.message
        assert "network error" in message
        assert "offline" in message

    def test_select_account_without_marked_default_uses_first_account(self, caplog):
        from notebooklm.auth import Account
        from notebooklm.cli.services.login import _select_account
        from tests._fixtures.login_io import RecordingLoginIO

        accounts = [
            Account(authuser=0, email="alice@example.com", is_default=False),
            Account(authuser=1, email="bob@gmail.com", is_default=False),
        ]

        # The no-default warning is now emitted through the injected ``LoginIO``
        # sink (#1393); capture it on a ``RecordingLoginIO`` instead of the
        # module-level console.
        io = RecordingLoginIO()
        with caplog.at_level("WARNING", logger="notebooklm.cli.services.login.cookie_writes"):
            selected = _select_account(io, accounts, account_email=None)

        assert selected == accounts[0]
        warning_text = io.emitted[0]
        assert "default account" in warning_text
        assert "alice@example.com" in warning_text
        assert "default account" in caplog.text
        assert "alice@example.com" in caplog.text

    def test_select_account_empty_accounts_returns_user_message(self):
        from notebooklm.cli.services.login.cookie_writes import _select_account
        from notebooklm.cli.services.login.outcomes import CookieValidationFailure
        from tests._fixtures.login_io import make_recording_io

        result = _select_account(make_recording_io(), [], account_email=None)

        assert isinstance(result, CookieValidationFailure)
        message = result.message
        assert "No signed-in Google accounts found" in message

    def test_select_refresh_account_empty_accounts_returns_user_message(self):
        from notebooklm.cli.services.login.cookie_writes import _select_refresh_account
        from notebooklm.cli.services.login.outcomes import CookieValidationFailure

        result = _select_refresh_account([], {}, "chrome")

        assert isinstance(result, CookieValidationFailure)
        message = result.message
        assert "No signed-in Google accounts found in chrome" in message

    def test_inspect_lists_accounts(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
                Account(authuser=2, email="carol@ws.com", is_default=False),
            ]

        # The account-enumeration probe now runs through the injected ``LoginIO``
        # sink's ``run_async`` (the default ``PlaywrightLoginIO`` →
        # ``cli.runtime.run_async``, #1393); mocking ``enumerate_accounts`` is
        # enough — the real ``run_async`` drives the (already-async) stub.
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch.object(auth_module, "enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])
        assert result.exit_code == 0, result.output
        assert "alice@example.com" in result.output
        assert "bob@gmail.com" in result.output
        assert "carol@ws.com" in result.output
        assert "authuser" not in result.output

    def test_inspect_json_output(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch.object(auth_module, "enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["accounts"][0]["email"] == "alice@example.com"
        assert "authuser" not in data["accounts"][0]
        assert data["accounts"][0]["is_default"] is True
