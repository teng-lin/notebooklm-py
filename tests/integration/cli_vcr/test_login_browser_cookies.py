"""CLI integration tests for ``notebooklm login --browser-cookies``.

The VCR cassette captures ONLY the post-import auth verification GET — the
homepage fetch that ``_login_with_browser_cookies`` performs after
``atomic_write_json`` lands the storage_state file. No batchexecute RPC, no
OAuth handshake, and no RotateCookies POST is recorded.

Two monkeypatches keep the recorded path narrow:

1. ``_read_browser_cookies`` — patched to return a sanitized rookiepy cookie
   set instead of opening the user's real browser DB.
2. ``_sync_server_language_to_config`` — neutralized so it does not fire the
   ``get_output_language`` batchexecute RPC that ``_login_with_browser_cookies``
   normally invokes at line ``session.py:984``.

``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` suppresses the layer-1 RotateCookies
POST. ``NOTEBOOKLM_HOME`` is redirected to ``tmp_path`` so the test never
writes outside the sandbox.
"""

from __future__ import annotations

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


# A sanitized rookiepy cookie payload. Required cookies (``SID`` +
# ``__Secure-1PSIDTS``) plus the secondary bindings (``APISID`` +
# ``SAPISID`` and ``OSID``) so ``_has_valid_secondary_binding`` is
# happy and no secondary-binding warning is emitted during the test.
SANITIZED_ROOKIEPY_COOKIES: list[dict] = [
    {
        "domain": ".google.com",
        "name": "SID",
        "value": "FIXTURE_SID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": False,
    },
    {
        "domain": ".google.com",
        "name": "HSID",
        "value": "FIXTURE_HSID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "SSID",
        "value": "FIXTURE_SSID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "APISID",
        "value": "FIXTURE_APISID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": False,
        "http_only": False,
    },
    {
        "domain": ".google.com",
        "name": "SAPISID",
        "value": "FIXTURE_SAPISID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "OSID",
        "value": "FIXTURE_OSID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "__Secure-1PSID",
        "value": "FIXTURE_1PSID_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "__Secure-1PSIDTS",
        "value": "FIXTURE_1PSIDTS_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "__Secure-3PSIDTS",
        "value": "FIXTURE_3PSIDTS_VALUE",
        "path": "/",
        "expires": 2000000000,
        "secure": True,
        "http_only": True,
    },
]


class TestLoginBrowserCookies:
    """Test 'notebooklm login --browser-cookies' (rookiepy fast-path)."""

    @notebooklm_vcr.use_cassette("cli_login_browser_cookies_check.yaml")
    def test_browser_cookies_imports_and_verifies(self, runner, tmp_path, monkeypatch) -> None:
        """The fast-path writes storage_state.json then verifies via homepage GET.

        Asserts:
          * exit code is 0
          * confirmation prints ("Cookies verified successfully.")
          * storage_state.json was written to the sandbox profile
        """
        # Redirect NotebookLM home into the sandbox so no real config is touched.
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        # Suppress layer-1 RotateCookies poke (cassette doesn't capture it).
        monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")

        # Replace the real browser-cookie reader with our sanitized fixture so
        # the test never opens the user's actual browser cookie database.
        monkeypatch.setattr(
            "notebooklm.cli.session._read_browser_cookies",
            lambda *a, **kw: SANITIZED_ROOKIEPY_COOKIES,
        )

        # Skip the post-verification settings RPC (line session.py:984) so the
        # cassette only captures the homepage GET. Without this, the run would
        # also fire a batchexecute call for ``get_output_language``.
        monkeypatch.setattr(
            "notebooklm.cli.session._sync_server_language_to_config",
            lambda *a, **kw: None,
        )

        result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 0, result.output
        assert "Cookies verified successfully." in result.output
        # The storage_state file must have been atomically written under the
        # profile dir inside NOTEBOOKLM_HOME.
        storage_files = list(tmp_path.glob("**/storage_state.json"))
        assert storage_files, (
            f"Expected storage_state.json under {tmp_path}; output was: {result.output}"
        )

    @notebooklm_vcr.use_cassette("cli_login_browser_cookies_check.yaml")
    def test_browser_cookies_routes_to_read_helper(self, runner, tmp_path, monkeypatch) -> None:
        """``--browser-cookies <name>`` forwards the browser name verbatim.

        Regression guard: a defaults rewrite that swallowed the argument would
        silently fall through to the Playwright path. We capture the call to
        ``_read_browser_cookies`` and assert the browser-name positional arg
        matches what the user passed.
        """
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")

        called_with: list[str] = []

        def _capture(name: str, **kwargs):
            called_with.append(name)
            return SANITIZED_ROOKIEPY_COOKIES

        monkeypatch.setattr("notebooklm.cli.session._read_browser_cookies", _capture)
        monkeypatch.setattr(
            "notebooklm.cli.session._sync_server_language_to_config",
            lambda *a, **kw: None,
        )

        result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])

        assert result.exit_code == 0, result.output
        assert called_with == ["firefox"], (
            f"Expected --browser-cookies firefox to route through 'firefox', got: {called_with}"
        )
