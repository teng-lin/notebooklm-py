"""Tests for auth account profile + multi-account switching (split from tests/unit/test_auth.py for D1 PR-2).

This file owns one concern from the auth subpackage. The original
``tests/unit/test_auth.py`` (4090 LOC) was split into six concern-aligned
files alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth as auth_module
from notebooklm._auth import account as _auth_account
from notebooklm.auth import (
    Account,
    enumerate_accounts,
    extract_email_from_html,
    fetch_tokens_with_domains,
)


def _wiz_html_with_email(email: str) -> str:
    """Build a minimal NotebookLM-style page that embeds a user email."""
    return f'<script>window.WIZ_global_data = {{"oM1Kwf":"{email}"}};</script>'


class TestExtractEmailFromHtml:
    def test_extracts_first_email(self):
        html = _wiz_html_with_email("alice@example.com")
        assert extract_email_from_html(html) == "alice@example.com"

    def test_skips_known_non_user_addresses(self):
        # Footer-style emails come first, but the active user follows.
        html = (
            '"support@google.com" "noreply@accounts.google.com" '
            '"privacy@gmail.com" "alice@example.com"'
        )
        assert extract_email_from_html(html) == "alice@example.com"

    def test_returns_none_when_absent(self):
        assert extract_email_from_html("<html>no emails here</html>") is None

    def test_returns_none_when_only_blacklisted(self):
        html = '"support@google.com" "noreply@google.com"'
        assert extract_email_from_html(html) is None

    def test_blacklist_does_not_drop_workspace_local_parts(self):
        # ``support@customer.com`` is a legitimate Workspace user. Local-part
        # blacklist must be scoped to google-owned domains. Regression for
        # PR #364 review feedback.
        html = '"support@customer.com"'
        assert extract_email_from_html(html) == "support@customer.com"


class TestEnumerateAccounts:
    """Test multi-account discovery via authuser=N probing."""

    @pytest.mark.asyncio
    async def test_single_account(self, httpx_mock: HTTPXMock):
        """One signed-in account: authuser=0 returns it, authuser=1 falls back to it."""
        default_html = _wiz_html_with_email("alice@example.com")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0", content=default_html.encode()
        )
        # Silent fallback: authuser=1 returns the same email; loop must stop.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1", content=default_html.encode()
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=3)

        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]

    @pytest.mark.asyncio
    async def test_multiple_accounts_stops_on_silent_fallback(self, httpx_mock: HTTPXMock):
        """Three real accounts; authuser=3 silently returns account 0's email."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1",
            content=_wiz_html_with_email("bob@gmail.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=2",
            content=_wiz_html_with_email("carol@workspace.com").encode(),
        )
        # Silent fallback at index 3: matches default email → enumeration stops.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=3",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=5)

        assert accounts == [
            Account(authuser=0, email="alice@example.com", is_default=True),
            Account(authuser=1, email="bob@gmail.com", is_default=False),
            Account(authuser=2, email="carol@workspace.com", is_default=False),
        ]

    @pytest.mark.asyncio
    async def test_raises_when_authuser_zero_unauthenticated(self, httpx_mock: HTTPXMock):
        """Bare cookies → authuser=0 redirects to login → ValueError."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin", content=b"<html>Login</html>"
        )

        jar = httpx.Cookies()
        with pytest.raises(ValueError, match="Authentication expired or invalid"):
            await enumerate_accounts(jar)

    @pytest.mark.asyncio
    async def test_stops_when_subsequent_index_unparseable(self, httpx_mock: HTTPXMock):
        """authuser=N>0 with no parseable email → end-of-list, not error."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1", content=b"<html>nothing</html>"
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=3)

        assert len(accounts) == 1
        assert accounts[0].email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_respects_max_authuser_cap(self, httpx_mock: HTTPXMock):
        """Caller-provided ``max_authuser`` bounds the probe loop."""
        # Each index has a unique email so the silent-fallback check never trips.
        for n in range(0, 3):
            httpx_mock.add_response(
                url=f"https://notebooklm.google.com/?authuser={n}",
                content=_wiz_html_with_email(f"user{n}@example.com").encode(),
            )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2)

        assert [a.authuser for a in accounts] == [0, 1, 2]


class TestAccountMetadata:
    """Persist authuser index in context.json next to storage_state.json."""

    def test_read_returns_empty_when_missing(self, tmp_path):
        from notebooklm.auth import read_account_metadata

        storage = tmp_path / "storage_state.json"
        assert read_account_metadata(storage) == {}

    def test_read_returns_empty_when_storage_path_is_none(self):
        from notebooklm.auth import read_account_metadata

        assert read_account_metadata(None) == {}

    def test_get_authuser_defaults_to_zero(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        assert get_authuser_for_storage(storage) == 0

    def test_seam_aliased_patch_to_account_helpers(self, monkeypatch, tmp_path):
        """``get_authuser_for_storage`` resolves ``read_account_metadata`` via bare-name lookup."""
        storage = tmp_path / "storage_state.json"

        def fake_read_account_metadata(storage_path: Path | None) -> dict[str, Any]:
            assert storage_path == storage
            return {"authuser": 3, "email": "carol@example.com"}

        # Seam-aliased object-attribute patch (ADR-0007): patches the owning
        # module so bare-name lookups inside ``_auth.account`` observe the fake.
        monkeypatch.setattr(_auth_account, "read_account_metadata", fake_read_account_metadata)

        assert auth_module.get_authuser_for_storage(storage) == 3
        assert auth_module.get_account_email_for_storage(storage) == "carol@example.com"

    def test_get_authuser_reads_persisted_value(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage, write_account_metadata

        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=2, email="bob@gmail.com")
        assert get_authuser_for_storage(storage) == 2

    def test_write_updates_storage_state_with_in_band_account(self, tmp_path):
        """Post-P1-20: account metadata lands in ``storage_state.json``.

        The legacy two-file behavior (account in ``context.json``) is replaced
        by a unified atomic record under the ``notebooklm`` namespace key.
        Non-account context state in ``context.json`` (e.g. ``notebook_id``)
        is preserved untouched.
        """
        from notebooklm.auth import read_account_metadata, write_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb_existing"}), encoding="utf-8"
        )
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        meta = read_account_metadata(storage)
        assert meta == {"authuser": 1, "email": "alice@example.com"}
        # P1-20: account record now lives inside storage_state.json.
        in_band = json.loads(storage.read_text())["notebooklm"]
        assert in_band["version"] == 1
        assert in_band["account"] == {"authuser": 1, "email": "alice@example.com"}
        # CLI context state in context.json survives the write.
        assert json.loads((tmp_path / "context.json").read_text()) == {
            "notebook_id": "nb_existing",
        }

    def test_get_authuser_ignores_negative(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": -1}}), encoding="utf-8"
        )
        assert get_authuser_for_storage(storage) == 0

    def test_get_authuser_ignores_non_int(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": "1"}}), encoding="utf-8"
        )
        assert get_authuser_for_storage(storage) == 0

    def test_read_returns_empty_for_malformed_json(self, tmp_path):
        from notebooklm.auth import read_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        assert read_account_metadata(storage) == {}

    def test_clear_account_metadata_preserves_notebook_context(self, tmp_path):
        from notebooklm.auth import clear_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "alice@example.com"},
                }
            ),
            encoding="utf-8",
        )

        clear_account_metadata(storage)

        assert json.loads((tmp_path / "context.json").read_text()) == {"notebook_id": "nb_existing"}


class TestAuthuserPlumbing:
    """fetch_tokens_with_domains must honor account routing in context.json."""

    @pytest.mark.asyncio
    async def test_auth_tokens_from_auth_json_preserves_in_band_account(
        self, monkeypatch, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import AuthTokens

        monkeypatch.setenv(
            "NOTEBOOKLM_AUTH_JSON",
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ],
                    "notebooklm": {
                        "version": 1,
                        "account": {"authuser": 2, "email": "bob@example.com"},
                    },
                }
            ),
        )

        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=bob%40example.com",
            content=b'"SNlM0e":"csrf_env" "FdrFJe":"sess_env"',
        )

        auth = await AuthTokens.from_storage()

        assert auth.storage_path is None
        assert auth.authuser == 2
        assert auth.account_email == "bob@example.com"
        assert auth.csrf_token == "csrf_env"
        assert auth.session_id == "sess_env"

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_prefers_persisted_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import write_account_metadata

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        write_account_metadata(storage, authuser=2, email="bob@example.com")

        # Token fetch must use the stable email route, not the reorder-prone
        # integer index.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=bob%40example.com",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"
        # If pytest-httpx had to fall back to a default match, the assert above
        # would fail; the explicit URL match is the contract.

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_uses_explicit_authuser_without_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=2",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage, authuser=2)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"

    @pytest.mark.asyncio
    async def test_explicit_authuser_overrides_persisted_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import write_account_metadata

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        write_account_metadata(storage, authuser=2, email="bob@example.com")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage, authuser=0)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"
