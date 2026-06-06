"""CLI integration tests for the ``share`` command group.

These tests exercise the full CLI -> Client -> RPC path using VCR cassettes,
covering the sharing happy paths flagged by issue #1316. The issue names them
``add`` / ``list`` / ``revoke``; the real CLI surface (``cli/share_cmd.py``)
exposes them as:

* ``share add <email>``  -> add a collaborator   (issue's "add")
* ``share status``       -> list collaborators    (issue's "list")
* ``share remove <email>`` -> revoke access       (issue's "revoke")

RPC fan-out per command
-----------------------
``client.sharing`` issues these RPCs (see ``src/notebooklm/_sharing.py``):

* ``status`` -> one ``GET_SHARE_STATUS`` (``JFMDGd``).
* ``add``    -> ``SHARE_NOTEBOOK`` (``QDyure``) then ``GET_SHARE_STATUS`` to
  re-read the updated user list.
* ``remove`` -> ``SHARE_NOTEBOOK`` (``QDyure``) then ``GET_SHARE_STATUS``.

Each command resolves its notebook via a full UUID passed with ``-n`` so
``resolve_notebook_id`` skips the ``LIST_NOTEBOOKS`` preflight (mirrors the
``mock_context`` docstring in ``conftest.py``). The cassettes therefore hold
only the sharing RPC chain above.

Privacy
-------
Sharing responses embed real account email addresses + display names. The
cassettes here were recorded against a throwaway notebook and then scrubbed:
the recording owner's address collapses to ``SCRUBBED_EMAIL@example.com`` and
the display name to ``SCRUBBED_NAME`` via the email/display-name scrubbers in
``tests/cassette_patterns.py``. The synthetic collaborator used for
``add`` / ``remove`` is an ``@example.com`` address (a reserved,
non-routable domain), recorded with ``--no-notify`` so no real email is sent.

Because the email/name scrubbers rewrite the recorded user list, replay
assertions deliberately stay structural (exit code + JSON shape) rather than
asserting on a specific scrubbed email value.

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_share.py -m vcr
"""

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import VCR_SHARE_NOTEBOOK_ID
from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``VCR_SHARE_NOTEBOOK_ID`` is an alias of ``MUTATION_NOTEBOOK_ID`` in
# ``_fixtures`` (the same alias ``test_notebook.py`` uses); a full UUID passed
# with ``-n`` keeps ``resolve_notebook_id`` on its fast path so each cassette
# captures only the sharing RPC chain. The value is never matched against the
# recorded body (VCR matches batchexecute on ``rpcids`` + decoded shape).

# Synthetic, non-routable collaborator address (RFC 2606 reserved domain).
VCR_SHARE_EMAIL = "vcr-share-test@example.com"


class TestShareStatusCommand:
    """Test ``notebooklm share status`` (lists collaborators)."""

    @notebooklm_vcr.use_cassette("cli_share_status.yaml")
    def test_share_status(self, runner, mock_auth_for_vcr):
        """``share status`` renders the sharing summary from one RPC."""
        result = runner.invoke(cli, ["share", "status", "-n", VCR_SHARE_NOTEBOOK_ID])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_share_status.yaml")
    def test_share_status_json(self, runner, mock_auth_for_vcr):
        """``share status --json`` emits the machine-readable sharing payload."""
        result = runner.invoke(cli, ["share", "status", "-n", VCR_SHARE_NOTEBOOK_ID, "--json"])
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert data.get("notebook_id") == VCR_SHARE_NOTEBOOK_ID
        assert "is_public" in data
        assert isinstance(data.get("shared_users"), list)


class TestShareAddCommand:
    """Test ``notebooklm share add <email>``."""

    @notebooklm_vcr.use_cassette("cli_share_add.yaml")
    def test_share_add(self, runner, mock_auth_for_vcr):
        """``share add --no-notify`` runs SHARE_NOTEBOOK + GET_SHARE_STATUS."""
        result = runner.invoke(
            cli,
            [
                "share",
                "add",
                VCR_SHARE_EMAIL,
                "-n",
                VCR_SHARE_NOTEBOOK_ID,
                "--permission",
                "viewer",
                "--no-notify",
            ],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_share_add.yaml")
    def test_share_add_json(self, runner, mock_auth_for_vcr):
        """``share add --json`` reports the added user + permission."""
        result = runner.invoke(
            cli,
            [
                "share",
                "add",
                VCR_SHARE_EMAIL,
                "-n",
                VCR_SHARE_NOTEBOOK_ID,
                "--permission",
                "viewer",
                "--no-notify",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert data.get("added_user") == VCR_SHARE_EMAIL
        assert data.get("permission") == "viewer"
        assert data.get("notified") is False


class TestShareRemoveCommand:
    """Test ``notebooklm share remove <email>`` (revoke access)."""

    @notebooklm_vcr.use_cassette("cli_share_remove.yaml")
    def test_share_remove(self, runner, mock_auth_for_vcr):
        """``share remove --yes`` runs SHARE_NOTEBOOK + GET_SHARE_STATUS."""
        result = runner.invoke(
            cli,
            ["share", "remove", VCR_SHARE_EMAIL, "-n", VCR_SHARE_NOTEBOOK_ID, "--yes"],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_share_remove.yaml")
    def test_share_remove_json(self, runner, mock_auth_for_vcr):
        """``share remove --yes --json`` reports the revoked user."""
        result = runner.invoke(
            cli,
            [
                "share",
                "remove",
                VCR_SHARE_EMAIL,
                "-n",
                VCR_SHARE_NOTEBOOK_ID,
                "--yes",
                "--json",
            ],
        )
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert data.get("removed_user") == VCR_SHARE_EMAIL
