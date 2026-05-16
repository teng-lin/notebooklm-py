"""CLI integration tests for ``notebooklm doctor``.

The ``doctor`` command is a local-only diagnostic — it inspects
``$NOTEBOOKLM_HOME`` (profile directory, ``storage_state.json``,
``config.json``) and makes **no** HTTP requests. The matched cassette
``cli_doctor.yaml`` is therefore intentionally empty (``interactions: []``).

Why register an empty cassette at all?
    With ``record_mode="none"`` (the default for CI replay), VCR raises on
    any unmatched request. Pinning ``doctor`` to an empty cassette turns
    "future refactor accidentally adds a network call" into a loud test
    failure (``CannotOverwriteExistingCassetteException``) — the author
    must then re-record the cassette intentionally. This closes audit I10
    (CLI VCR coverage gap) without recording fake traffic.

Both tests run inside an isolated ``NOTEBOOKLM_HOME`` (``tmp_path``) so the
real user's profile / config / storage is never touched, and the existing
``notebooklm.paths`` module caches are reset before each test so the
sandbox env var actually takes effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebooklm import paths
from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``NOTEBOOKLM_HOME`` into ``tmp_path`` and clear module caches.

    Mirrors the ``isolated_notebooklm_home`` autouse fixture in
    ``tests/unit/cli/test_doctor.py`` but lives here so the cli_vcr suite
    doesn't have to depend on a unit-test conftest.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths.set_active_profile(None)
    paths._reset_config_cache()
    yield tmp_path
    paths.set_active_profile(None)
    paths._reset_config_cache()


def _make_profile(home: Path, name: str = "default") -> Path:
    """Create a profile directory at ``home/profiles/<name>`` with 0o700 perms."""
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    # chmod is a no-op on Windows; the doctor command tolerates that already.
    try:
        profile_dir.chmod(0o700)
    except (OSError, NotImplementedError):
        pass
    return profile_dir


def _write_storage(profile_dir: Path, cookies: list[dict[str, str]]) -> None:
    (profile_dir / "storage_state.json").write_text(
        json.dumps({"cookies": cookies}), encoding="utf-8"
    )


class TestDoctorCommand:
    """``notebooklm doctor`` — local diagnostic, no HTTP."""

    @pytest.mark.parametrize("json_flag", [False, True])
    @notebooklm_vcr.use_cassette("cli_doctor.yaml")
    def test_doctor_happy_path(self, runner, isolated_home: Path, json_flag: bool) -> None:
        """Doctor with a clean profile + SID cookie reports all checks pass.

        Asserts:
          * exit code 0
          * No HTTP traffic emitted (empty cassette would trip on a request).
          * ``--json`` output, when requested, is parseable and reports
            ``auth.status == "pass"``.
        """
        profile_dir = _make_profile(isolated_home)
        _write_storage(profile_dir, [{"name": "SID", "value": "fixture-sid"}])
        (isolated_home / "config.json").write_text(
            json.dumps({"default_profile": "default"}), encoding="utf-8"
        )

        args = ["doctor"]
        if json_flag:
            args.append("--json")

        result = runner.invoke(cli, args)

        assert result.exit_code == 0, result.output
        if json_flag:
            data = json.loads(result.output)
            assert data["profile"] == "default"
            assert data["checks"]["auth"]["status"] == "pass"

    @notebooklm_vcr.use_cassette("cli_doctor.yaml")
    def test_doctor_reports_missing_auth(self, runner, isolated_home: Path) -> None:
        """Doctor with no ``storage_state.json`` reports auth failure cleanly.

        The auth-failure path is the second axis the task acceptance asks
        for. Even on the failure path the command still exits 0 (it's a
        diagnostic — ``doctor`` reports failures rather than exiting
        non-zero) and emits no HTTP requests.
        """
        _make_profile(isolated_home)
        # Deliberately no storage_state.json written.

        result = runner.invoke(cli, ["doctor", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["checks"]["auth"]["status"] == "fail"
        assert data["checks"]["auth"]["detail"] == "not authenticated"
