"""CLI integration tests for ``notebooklm auth`` (VCR replay).

The ``auth`` group mixes local-only subcommands (``inspect`` / ``logout`` --
filesystem and browser-cookie only) with RPC-path subcommands:

* ``auth check``   -- local cookie validation by default (no HTTP); with
  ``--test`` it also exercises the token-fetch round-trip (a homepage GET to
  re-mint CSRF / session).
* ``auth refresh`` -- the one-shot keepalive: loads the profile's
  ``storage_state.json``, fetches CSRF + session from ``notebooklm.google.com``
  (a homepage GET; their side effect is the rotated cookie jar), and persists
  the jar back to disk.

These tests drive the RPC-path subcommands through Click's ``CliRunner`` while
VCR replays recorded traffic from ``tests/cassettes`` -- no live auth, no new
recording. They close the ``auth`` cli_vcr coverage gap (issue #1452 Phase 3):
the gate's ``COVERAGE_EXEMPT`` reason for ``auth`` was stale -- the cassette the
token-fetch path needs (``auth_rotate_cookies_refresh.yaml``) already existed,
so the group is exercisable without a maintainer recording.

Cassettes reused:

* ``cli_auth.yaml`` -- intentionally empty (``interactions: []``). Pins the
  *local* ``auth check`` path so that if a future refactor makes the no-network
  validation start issuing HTTP, VCR's ``record_mode="none"`` trips a loud
  ``CannotOverwriteExistingCassetteException`` (the doctor / profile idiom).
* ``auth_rotate_cookies_refresh.yaml`` -- the recorded refresh handshake. Its
  homepage ``GET https://notebooklm.google.com/`` (interaction 2) is exactly the
  request the token-fetch path makes; the two ``wXbhsf`` batchexecute POSTs it
  also carries are left unplayed (VCR tolerates unplayed cassette interactions,
  erroring only on UNMATCHED requests). Cross-referenced against
  ``tests/integration/test_auth_refresh_vcr.py``, which drives the same cassette
  through the client API.

Why ``auth refresh`` / ``auth check --test`` deliberately do NOT use
``mock_auth_for_vcr``: that fixture patches ``notebooklm.auth.fetch_tokens_with_domains``
to a stub, which would short-circuit the very token-fetch round-trip these tests
mean to replay. Instead they run the real fetch against an isolated profile with
a real ``storage_state.json`` so the homepage GET reaches the cassette. The
autouse ``_disable_keepalive_poke_for_vcr`` fixture (in
``tests/integration/conftest.py``) suppresses the layer-1 ``RotateCookies`` poke
for every ``@pytest.mark.vcr`` test, so no un-recorded ``accounts.google.com``
POST escapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebooklm import paths
from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# A storage_state.json with the cookies the token-fetch path needs. SID is the
# load-bearing cookie for ``auth check``'s ``sid_cookie`` probe; the rest round
# out a realistic Google auth jar. Values are obviously synthetic -- VCR matches
# on request shape, never on cookie values, so the recorded homepage GET replays
# regardless of what is sent.
_STORAGE_COOKIES = [
    {"name": "SID", "value": "fixture-sid", "domain": ".google.com", "path": "/"},
    {"name": "HSID", "value": "fixture-hsid", "domain": ".google.com", "path": "/"},
    {"name": "SSID", "value": "fixture-ssid", "domain": ".google.com", "path": "/"},
    {"name": "APISID", "value": "fixture-apisid", "domain": ".google.com", "path": "/"},
    {"name": "SAPISID", "value": "fixture-sapisid", "domain": ".google.com", "path": "/"},
    {
        "name": "__Secure-1PSIDTS",
        "value": "fixture-1psidts",
        "domain": ".google.com",
        "path": "/",
    },
]


@pytest.fixture
def isolated_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``NOTEBOOKLM_HOME`` into ``tmp_path`` with a seeded default profile.

    Writes a real ``storage_state.json`` under the default profile so the
    token-fetch path loads genuine on-disk cookies (the keepalive needs a
    writable storage file). Clears ``notebooklm.paths`` module caches before and
    after so the sandbox env var actually takes effect, mirroring the
    ``isolated_home`` fixtures in ``test_doctor.py`` / ``test_profile.py``.

    Returns the resolved ``storage_state.json`` path so tests can assert the
    keepalive persisted the rotated jar back to it.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths.set_active_profile(None)
    paths._reset_config_cache()

    profile_dir = tmp_path / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    try:
        profile_dir.chmod(0o700)
    except (OSError, NotImplementedError):
        pass
    storage_path = profile_dir / "storage_state.json"
    # Seed valid in-band account metadata (``notebooklm.account``) so the
    # keepalive's post-fetch ``_is_valid_account_metadata`` check passes and the
    # "Identifying Google account..." repair path (which would issue a SECOND,
    # un-recorded homepage GET) is skipped -- the cassette records exactly ONE
    # homepage GET, so the keepalive must make exactly one.
    storage_path.write_text(
        json.dumps(
            {
                "cookies": _STORAGE_COOKIES,
                "notebooklm": {"account": {"authuser": 0, "email": "fixture@example.com"}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"default_profile": "default"}), encoding="utf-8"
    )

    yield storage_path

    paths.set_active_profile(None)
    paths._reset_config_cache()


class TestAuthCheckCommand:
    """``notebooklm auth check`` -- local cookie validation (+ optional --test)."""

    @notebooklm_vcr.use_cassette("cli_auth.yaml")
    def test_check_local_passes(self, runner, isolated_profile: Path) -> None:
        """Default ``auth check`` validates the seeded jar with NO HTTP.

        The empty ``cli_auth.yaml`` cassette would trip on any request, so a
        clean exit 0 also proves the no-network contract of the default path.
        """
        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["checks"]["storage_exists"] is True
        assert data["checks"]["sid_cookie"] is True

    @notebooklm_vcr.use_cassette("cli_auth.yaml")
    def test_check_missing_storage_fails(
        self, runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``auth check`` on a profile with no ``storage_state.json`` reports failure.

        Still makes no HTTP request (the empty cassette guards that), and exits
        non-zero because the storage-exists probe fails.
        """
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        paths.set_active_profile(None)
        paths._reset_config_cache()
        try:
            result = runner.invoke(cli, ["auth", "check", "--json"])
        finally:
            paths.set_active_profile(None)
            paths._reset_config_cache()

        assert result.exit_code != 0, result.output
        data = json.loads(result.output)
        assert data["checks"]["storage_exists"] is False

    def test_check_test_fetch_round_trip(self, runner, isolated_profile: Path) -> None:
        """``auth check --test`` replays the homepage token-fetch GET and reports pass.

        Reuses the refresh cassette's homepage GET (interaction 2). The
        ``--test`` path mints CSRF + session from the recorded HTML; a
        ``token_fetch == True`` check proves the extractor ran against real
        recorded page chrome, and ``play_count == 1`` proves the round-trip
        actually hit the cassette rather than short-circuiting.
        """
        with notebooklm_vcr.use_cassette("auth_rotate_cookies_refresh.yaml") as cassette:
            result = runner.invoke(cli, ["auth", "check", "--test", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["checks"]["token_fetch"] is True
        assert cassette.play_count == 1, "expected exactly one recorded homepage GET to replay"


class TestAuthRefreshCommand:
    """``notebooklm auth refresh`` -- one-shot keepalive token fetch."""

    def test_refresh_keepalive(self, runner, isolated_profile: Path) -> None:
        """``auth refresh`` replays the homepage GET and reports the refreshed path.

        The keepalive loads the seeded jar, performs the homepage GET (replayed
        from the cassette), re-mints CSRF / session, and persists the jar back
        to ``storage_state.json``. Asserts exit 0, the success line naming the
        storage path, and ``play_count == 1`` -- the recorded GET is what makes
        the fetch succeed offline, and the count proves it was actually issued.
        """
        with notebooklm_vcr.use_cassette("auth_rotate_cookies_refresh.yaml") as cassette:
            result = runner.invoke(cli, ["auth", "refresh"])

        assert result.exit_code == 0, result.output
        assert "refreshed" in result.output
        assert cassette.play_count == 1, "expected exactly one recorded homepage GET to replay"
        # The keepalive's whole point is a writable storage file; it must still
        # exist (and remain valid JSON) after the rotated jar is persisted.
        assert isolated_profile.exists()
        json.loads(isolated_profile.read_text(encoding="utf-8"))

    def test_refresh_quiet_suppresses_success_line(self, runner, isolated_profile: Path) -> None:
        """``auth refresh --quiet`` runs the same fetch but prints no output at all.

        The quiet keepalive still issues (and consumes) the recorded homepage GET
        -- ``play_count == 1`` -- but suppresses the success line entirely, so
        stdout/stderr come back empty.
        """
        with notebooklm_vcr.use_cassette("auth_rotate_cookies_refresh.yaml") as cassette:
            result = runner.invoke(cli, ["auth", "refresh", "--quiet"])

        assert result.exit_code == 0, result.output
        assert not result.output.strip(), (
            f"quiet refresh must print nothing, got: {result.output!r}"
        )
        assert cassette.play_count == 1, "expected exactly one recorded homepage GET to replay"
