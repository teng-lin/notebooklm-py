"""CLI integration tests for notebook mutation commands.

These tests exercise the full CLI -> Client -> RPC path using VCR cassettes,
covering the notebook lifecycle happy paths flagged by issue #1316:
``list`` / ``create`` / ``rename`` / ``delete``.

The read-only ``list`` / ``summary`` / ``status`` paths already have coverage
in ``test_notebooks.py`` (which reuses the ``notebooks_list.yaml`` /
``notebooks_get_summary.yaml`` cassettes). This module is deliberately the
*mutation* sibling so the two files do not contend for the same cassettes:
the dedicated CLI cassettes here each capture exactly the single RPC the
command under test emits.

Cassette design
---------------
Each mutating command resolves its notebook target via a full UUID passed with
``-n`` so ``resolve_notebook_id`` short-circuits the prefix lookup and never
emits an extra ``LIST_NOTEBOOKS`` RPC before the command under test (mirrors
the ``mock_context`` docstring in ``conftest.py``). The result is one
single-purpose cassette per command:

* ``cli_notebook_list.yaml``   -> ``LIST_NOTEBOOKS``
* ``cli_notebook_create.yaml`` -> ``CREATE_NOTEBOOK``
* ``cli_notebook_rename.yaml`` -> ``RENAME_NOTEBOOK``
* ``cli_notebook_delete.yaml`` -> ``DELETE_NOTEBOOK``

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_notebook.py -m vcr

The create/rename/delete cassettes were recorded against a throwaway notebook
created for the recording session and deleted at the end of it, so no
persistent account state is mutated by replaying them.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from ._fixtures import VCR_MUTABLE_NOTEBOOK_ID
from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``VCR_MUTABLE_NOTEBOOK_ID`` (an alias of ``MUTATION_NOTEBOOK_ID`` in
# ``_fixtures``) is a full UUID passed with ``-n`` so ``resolve_notebook_id``
# stays on its fast path (no ``LIST_NOTEBOOKS`` preflight) and each cassette
# below holds exactly one RPC. The value is never matched against the recorded
# body (VCR matches batchexecute on ``rpcids`` + decoded shape); it *is*
# load-bearing for the input-echo asserts below, where the CLI threads the id
# back into its own ``--json`` output.


class TestListCommand:
    """Test ``notebooklm list`` (read-only)."""

    @notebooklm_vcr.use_cassette("cli_notebook_list.yaml")
    def test_list_notebooks(self, runner, mock_auth_for_vcr):
        """``list`` renders the table from a real client + LIST_NOTEBOOKS RPC."""
        result = runner.invoke(cli, ["list"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_notebook_list.yaml")
    def test_list_notebooks_json(self, runner, mock_auth_for_vcr):
        """``list --json`` emits a machine-readable payload."""
        result = runner.invoke(cli, ["list", "--json"])
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert data is not None, "Expected valid JSON output"
        assert isinstance(data, list | dict)


class TestCreateCommand:
    """Test ``notebooklm create <title>``."""

    @notebooklm_vcr.use_cassette("cli_notebook_create.yaml")
    def test_create_notebook(self, runner, mock_auth_for_vcr):
        """``create`` issues a single CREATE_NOTEBOOK RPC and reports the id."""
        result = runner.invoke(cli, ["create", "VCR CLI Test Notebook"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_notebook_create.yaml")
    def test_create_notebook_json(self, runner, mock_auth_for_vcr):
        """``create --json`` surfaces the created notebook id + title."""
        result = runner.invoke(cli, ["create", "VCR CLI Test Notebook", "--json"])
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        notebook = data.get("notebook")
        assert isinstance(notebook, dict), f"Expected 'notebook' object: {data!r}"
        assert notebook.get("id"), "Created notebook must carry an id"


class TestRenameCommand:
    """Test ``notebooklm rename <new_title> -n <id>``."""

    @notebooklm_vcr.use_cassette("cli_notebook_rename.yaml")
    def test_rename_notebook(self, runner, mock_auth_for_vcr):
        """``rename`` issues a single RENAME_NOTEBOOK RPC for the full UUID.

        The id and the new title echoed back in the prose are the substantive
        assertions.
        """
        result = runner.invoke(
            cli,
            ["rename", "VCR CLI Renamed", "-n", VCR_MUTABLE_NOTEBOOK_ID],
        )
        assert_command_success(result, allow_no_context=False)
        assert VCR_MUTABLE_NOTEBOOK_ID in result.output
        assert "VCR CLI Renamed" in result.output

    @notebooklm_vcr.use_cassette("cli_notebook_rename.yaml")
    def test_rename_notebook_json(self, runner, mock_auth_for_vcr):
        """``rename --json`` reuses the same RPC and reports a structured result.

        ``rename_cmd`` (``cli/notebook_cmd.py``) carries ``@json_option`` and,
        when ``--json`` is passed, emits
        ``{"notebook_id": "<id>", "title": "<title>", "success": True}`` instead
        of the prose.
        Same cassette: the JSON branch only changes the *output formatting*, not
        the underlying RENAME_NOTEBOOK call.
        """
        result = runner.invoke(
            cli,
            ["rename", "VCR CLI Renamed", "-n", VCR_MUTABLE_NOTEBOOK_ID, "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert data.get("notebook_id") == VCR_MUTABLE_NOTEBOOK_ID
        assert data.get("title") == "VCR CLI Renamed"
        assert data.get("success") is True


class TestDeleteCommand:
    """Test ``notebooklm delete -n <id> --yes``."""

    @notebooklm_vcr.use_cassette("cli_notebook_delete.yaml")
    def test_delete_notebook(self, runner, mock_auth_for_vcr):
        """``delete --yes`` issues a single DELETE_NOTEBOOK RPC."""
        result = runner.invoke(
            cli,
            ["delete", "-n", VCR_MUTABLE_NOTEBOOK_ID, "--yes"],
        )
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("cli_notebook_delete.yaml")
    def test_delete_notebook_json(self, runner, mock_auth_for_vcr):
        """``delete --yes --json`` reports the deleted id with success=True."""
        result = runner.invoke(
            cli,
            ["delete", "-n", VCR_MUTABLE_NOTEBOOK_ID, "--yes", "--json"],
        )
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert data.get("notebook_id") == VCR_MUTABLE_NOTEBOOK_ID
        assert data.get("success") is True
