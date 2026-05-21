"""Click ``IntRange`` validation for shared int options.

These tests exercise the Click option layer (P1.T7) — they verify that
``--interval``, ``--retry``, and ``--limit`` reject out-of-range integers at
parse time with ``exit_code == 2`` (Click's convention for ``UsageError``)
so command bodies never see invalid values such as ``--interval 0`` (which
would otherwise blow up inside the polling loop with ``ZeroDivisionError`` or
busy-spin) or ``--limit -1``.

Every test here fails at the Click parser before the command body runs, so
no auth or HTTP mocking is needed — the CLI never reaches a real call site.
"""

from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

# ---------------------------------------------------------------------------
# --interval: IntRange(min=1) — zero would cause divide-by-zero / busy-loop
# ---------------------------------------------------------------------------


class TestIntervalRange:
    def test_research_wait_interval_zero_is_usage_error(self) -> None:
        """``research wait --interval 0`` must fail at parse time.

        Before the IntRange guard, this would propagate into the sleep loop
        and either busy-spin or trigger ZeroDivisionError downstream.
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["research", "wait", "--interval", "0"])
        assert result.exit_code == 2
        # Click's IntRange emits "is not in the range x<=...<=y" / "must be >= 1"
        # depending on version; assert on a stable substring.
        assert "--interval" in result.output or "interval" in result.output.lower()
        assert "0" in result.output

    def test_research_wait_interval_negative_is_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["research", "wait", "--interval", "-1"])
        assert result.exit_code == 2

    def test_artifact_wait_interval_zero_is_usage_error(self) -> None:
        """``artifact wait --interval 0`` must also fail (shared options decorator).

        ``artifact wait`` uses ``wait_polling_options`` from
        ``cli/options.py``, so this exercises the shared decorator path.
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["artifact", "wait", "x", "--interval", "0"])
        assert result.exit_code == 2
        assert "interval" in result.output.lower()

    def test_source_wait_interval_zero_is_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "wait", "x", "--interval", "0"])
        assert result.exit_code == 2
        assert "interval" in result.output.lower()

    def test_interval_positive_value_accepts(self) -> None:
        """``--interval 1`` is the lower bound and must parse.

        We only check that Click does NOT reject the value at parse time;
        the command body itself may still fail (e.g. no auth) — that's fine,
        the goal is "parser accepts 1".
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["research", "wait", "--interval", "1", "--help"])
        # ``--help`` short-circuits the body, so a clean parse exits 0.
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --retry: IntRange(min=0) — negative retries are meaningless
# ---------------------------------------------------------------------------


class TestRetryRange:
    def test_generate_audio_retry_negative_is_usage_error(self) -> None:
        """``generate audio --retry -1`` must fail at parse time."""
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", "audio", "--retry", "-1"])
        assert result.exit_code == 2
        assert "--retry" in result.output or "retry" in result.output.lower()

    def test_generate_video_retry_negative_is_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", "video", "--retry", "-1"])
        assert result.exit_code == 2

    def test_retry_zero_accepts(self) -> None:
        """``--retry 0`` (the default — "no retries") must remain valid."""
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", "audio", "--retry", "0", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --limit: IntRange(min=0) — 0 means "show 0 rows"; negatives are rejected
# ---------------------------------------------------------------------------


class TestLimitRange:
    def test_notebook_list_limit_negative_is_usage_error(self) -> None:
        """``list --limit -1`` must fail at parse time."""
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--limit", "-1"])
        assert result.exit_code == 2
        assert "--limit" in result.output or "limit" in result.output.lower()

    def test_artifact_list_limit_negative_is_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["artifact", "list", "--limit", "-1"])
        assert result.exit_code == 2

    def test_source_list_limit_negative_is_usage_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "list", "--limit", "-1"])
        assert result.exit_code == 2

    def test_limit_zero_accepts(self) -> None:
        """``--limit 0`` must parse (semantics: show zero rows).

        This is the explicit decision documented in the plan: the existing
        slice ``rows[:0]`` yields an empty list, so 0 means "0 rows". Users
        omit the flag entirely for "unlimited".
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--limit", "0", "--help"])
        assert result.exit_code == 0

    def test_limit_help_text_documents_zero_semantics(self) -> None:
        """The shared ``--limit`` help text must spell out the 0 = zero rows rule.

        Click line-wraps long help strings in ``--help`` output, so collapse
        whitespace before matching to make the assertion linewrap-tolerant.
        """
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--help"])
        assert result.exit_code == 0
        # Plan-mandated wording: "Show at most N rows. 0 = show no rows. Omit for unlimited."
        collapsed = " ".join(result.output.split())
        assert "0 = show no rows" in collapsed
        assert "Omit for unlimited" in collapsed
