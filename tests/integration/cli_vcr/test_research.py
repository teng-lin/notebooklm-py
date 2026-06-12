"""CLI integration tests for ``notebooklm research`` (VCR replay).

The ``research`` group (``status`` / ``wait``) is an RPC-backed command set: it
opens a real :class:`~notebooklm.client.NotebookLMClient` and polls the research
session over ``batchexecute``. These tests drive the full CLI -> Client -> RPC
path through Click's ``CliRunner`` while VCR replays the recorded poll traffic
from ``tests/cassettes`` -- no live auth, no new recording.

Cassettes reused (recorded against the python-API / research suite; the
``cli_vcr`` suite shares ``cassette_library_dir`` so they replay directly):

* ``research_poll.yaml``       -- a poll that classifies to an active/terminal
  verdict (rpcids ``Ljjv0c`` then ``e3bVqc``); drives the populated
  ``research status`` path.
* ``research_poll_empty.yaml`` -- a poll against a notebook with no research
  task (single ``e3bVqc``); drives both the ``no_research`` ``research status``
  path and the single-poll terminal ``research wait`` path.

These close the ``research`` cli_vcr coverage gap (issue #1452 Phase 3): the
gate's ``COVERAGE_EXEMPT`` reason for ``research`` was stale -- the cassettes
already existed, so the group is exercisable without a maintainer recording.

Why this matters beyond the gate: ``research status``/``wait`` resolve the
notebook, poll the session, classify the result, and project the ``--json``
envelope -- a path no other cli_vcr test touches. The matcher keys on
``rpcids`` + decoded body shape (never the notebook id), so the placeholder id
``mock_context`` injects replays cleanly against cassettes recorded elsewhere.

The poll/``status`` commands only enter the RPC layer; the leading homepage GET
some cassettes carry (auth bootstrap) is simply left unplayed, which VCR's
``record_mode="none"`` tolerates -- it errors only on UNMATCHED requests the
cassette lacks, never on unplayed cassette interactions.
"""

from __future__ import annotations

import json

import pytest

from notebooklm.notebooklm_cli import cli
from notebooklm.types import ResearchStatus

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Allowed ``status`` values for a *populated* research poll -- every canonical
# ``ResearchStatus`` value except ``no_research`` (which is the empty-poll
# sentinel). Derived from the enum as a set-membership floor, NOT a pin on the
# recorded value, so the assertion survives a re-record into any populated state
# (issue #1452 re-record-safe convention).
_POPULATED_STATUS_VALUES = frozenset(
    member.value for member in ResearchStatus if member is not ResearchStatus.NO_RESEARCH
)


class TestResearchStatusCommand:
    """``notebooklm research status`` -- single non-blocking poll."""

    def test_status_text(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """Text-mode status against a populated poll exits 0 and prints a verdict.

        The recorded poll classifies to the ``in_progress`` branch, which prints
        a non-empty status line. The ``cassette.play_count == 1`` assertion pins
        that the command actually issued (and consumed) the recorded poll RPC --
        without it, a regression that short-circuited to a local default would
        still satisfy the exit-0 / output-present checks.
        """
        with notebooklm_vcr.use_cassette("research_poll.yaml") as cassette:
            result = runner.invoke(cli, ["research", "status"])

        assert result.exit_code == 0, result.output
        assert result.output.strip(), "expected a status line"
        # A traceback in the output means the command crashed rather than
        # rendering a classified result.
        assert "Traceback" not in result.output
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"

    def test_status_json_envelope(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """``research status --json`` emits the canonical ``to_public_dict`` payload.

        Asserts the envelope *shape* (a JSON object carrying a ``status`` key
        whose value is a string) plus the populated-cassette invariant that the
        status is a known *active-or-terminal* :class:`ResearchStatus` value (NOT
        ``no_research``). That catches a short-circuit / wrong-cassette regression
        where the command never reached the recorded in-flight poll, while staying
        re-record-safe: it is a set-membership check against the canonical enum
        (not a pin on the recorded value), so a re-record that lands in any
        populated state (today it is ``in_progress``) still passes.
        """
        with notebooklm_vcr.use_cassette("research_poll.yaml") as cassette:
            result = runner.invoke(cli, ["research", "status", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict), f"expected a JSON object, got: {result.output!r}"
        assert isinstance(data.get("status"), str), f"missing/invalid status: {data!r}"
        assert data["status"] in _POPULATED_STATUS_VALUES, (
            f"populated poll status must be a known non-no_research value: {data!r}"
        )
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"

    def test_status_no_research_text(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """A poll with no research task reports the ``no_research`` branch.

        ``research status`` (unlike ``research wait``) exits 0 even when no
        research is running -- it is a non-blocking status probe. The text
        render prints the "No research running" line. ``play_count == 1`` proves
        the verdict came from the recorded empty poll, not a skipped RPC.
        """
        with notebooklm_vcr.use_cassette("research_poll_empty.yaml") as cassette:
            result = runner.invoke(cli, ["research", "status"])

        assert result.exit_code == 0, result.output
        assert "No research running" in result.output
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"

    def test_status_no_research_json(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """``research status --json`` on an idle notebook reports ``no_research``."""
        with notebooklm_vcr.use_cassette("research_poll_empty.yaml") as cassette:
            result = runner.invoke(cli, ["research", "status", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict), f"expected a JSON object, got: {result.output!r}"
        assert data.get("status") == "no_research", f"expected no_research, got: {data!r}"
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"


class TestResearchWaitCommand:
    """``notebooklm research wait`` -- blocking poll loop.

    ``wait`` drives ``ResearchAPI.wait_for_completion``, which polls until a
    *terminal* status (``completed`` / ``failed`` / ``no_research``) or the
    timeout fires. ``research_poll_empty.yaml`` returns ``no_research`` on its
    single recorded ``e3bVqc`` poll, so ``wait_for_completion`` returns after
    exactly one interaction -- a deterministic terminal path that exercises the
    full CLI ``wait`` -> resolve -> poll-loop -> render -> exit-code chain
    without needing a cassette that records a multi-tick progression.

    ``fast_sleep`` no-ops ``asyncio.sleep`` so that if the poll loop ever sleeps
    before reaching its verdict the test still runs at full speed; on the
    single-poll terminal path the terminal check short-circuits before any
    sleep, so it is belt-and-braces here.
    """

    def test_wait_no_research_text(
        self, runner, mock_auth_for_vcr, mock_context, fast_sleep
    ) -> None:
        """``research wait`` on an idle notebook exits 1 with a no-research message.

        Unlike ``research status`` (a non-blocking probe that exits 0), ``wait``
        treats "nothing to wait for" as a failure outcome and exits 1 per the
        handler's ``no_research`` branch. ``play_count == 1`` proves the verdict
        came from the recorded poll, not a loop that short-circuited before
        issuing any RPC.
        """
        with notebooklm_vcr.use_cassette("research_poll_empty.yaml") as cassette:
            result = runner.invoke(cli, ["research", "wait", "--interval", "1"])

        assert result.exit_code == 1, result.output
        assert "No research running" in result.output
        assert "Traceback" not in result.output
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"

    def test_wait_no_research_json(
        self, runner, mock_auth_for_vcr, mock_context, fast_sleep
    ) -> None:
        """``research wait --json`` on an idle notebook emits the ``no_research`` envelope.

        Shape-only: a JSON object whose ``status`` is ``no_research`` and which
        carries an ``error`` string -- never a recorded value, so re-record-safe.
        """
        with notebooklm_vcr.use_cassette("research_poll_empty.yaml") as cassette:
            result = runner.invoke(cli, ["research", "wait", "--json", "--interval", "1"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert isinstance(data, dict), f"expected a JSON object, got: {result.output!r}"
        assert data.get("status") == "no_research", f"expected no_research, got: {data!r}"
        assert isinstance(data.get("error"), str), f"missing error string: {data!r}"
        assert cassette.play_count == 1, "expected exactly one recorded poll RPC to replay"
