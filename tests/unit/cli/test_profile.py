"""Tests for cli.profile helpers."""

import importlib
import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.cli.profile import _PROFILE_NAME_RE, email_to_profile_name
from notebooklm.notebooklm_cli import cli

profile_module = importlib.import_module("notebooklm.cli.profile")


class TestEmailToProfileName:
    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("alice@example.com", "alice"),
            ("alice.smith@example.com", "alice-smith"),
            ("bob+work@gmail.com", "bob-work"),
            ("teng.lin.9414@gmail.com", "teng-lin-9414"),
            ("under_score@gmail.com", "under_score"),
            ("dash-already@gmail.com", "dash-already"),
        ],
    )
    def test_sanitization(self, email, expected):
        assert email_to_profile_name(email) == expected

    def test_falls_back_when_local_part_starts_with_punctuation(self):
        # All-punctuation local-part collapses to empty → fallback fires.
        assert email_to_profile_name("...@example.com") == "account"

    def test_uses_provided_fallback(self):
        assert email_to_profile_name("...@example.com", fallback="custom") == "custom"

    def test_no_at_sign_treats_input_as_local_part(self):
        assert email_to_profile_name("plain") == "plain"

    def test_result_always_passes_profile_name_validation(self):
        # Hard property: every output must satisfy the regex used by the
        # `profile create` command, otherwise downstream usage would fail.
        for email in [
            "alice@example.com",
            "a.b.c+d@test.org",
            "...@x.com",  # falls back
            "x" * 64 + "@long.com",
        ]:
            name = email_to_profile_name(email)
            assert _PROFILE_NAME_RE.match(name), name


class TestProfileListAccountMetadata:
    def test_json_includes_account_metadata(self, tmp_path):
        profile_dir = tmp_path / "profiles" / "bob"
        profile_dir.mkdir(parents=True)
        storage_path = profile_dir / "storage_state.json"
        storage_path.write_text("{}")
        (profile_dir / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )

        def fake_get_storage_path(profile=None):
            assert profile == "bob"
            return storage_path

        runner = CliRunner()
        with (
            patch.object(profile_module, "list_profiles", return_value=["bob"]),
            patch.object(profile_module, "resolve_profile", return_value="bob"),
            patch.object(profile_module, "get_storage_path", side_effect=fake_get_storage_path),
        ):
            result = runner.invoke(cli, ["profile", "list", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["profiles"] == [
            {
                "name": "bob",
                "active": True,
                "authenticated": True,
                "account": "bob@gmail.com",
            }
        ]
