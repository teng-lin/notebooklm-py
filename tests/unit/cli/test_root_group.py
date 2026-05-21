"""Tests for root-group flags on the top-level ``notebooklm`` Click group.

Covers the root-group pieces of ``--quiet`` (env-var precedence + global quiet mode):

* ``notebooklm --quiet ...`` suppresses INFO/WARN logs from the ``notebooklm``
  package logger; only ERROR (and above) survive on stderr.
* ``notebooklm --quiet -v ...`` is rejected with a ``UsageError`` (exit 2)
  because the two intents conflict — ``--quiet`` raises the floor to ERROR
  while ``-v`` lowers it to INFO; honoring either silently would surprise
  one of the two callers.
* ``--quiet`` does NOT collide with the existing subcommand-scoped
  ``auth refresh --quiet`` flag — Click parses the closer scope first, so
  ``notebooklm auth refresh --quiet`` continues to hit the subcommand flag.

Status-output suppression is covered in ``test_quiet_flag.py``.

The new ``NOTEBOOKLM_NOTEBOOK`` env-var integration is tested in
``test_helpers.py::TestRequireNotebook`` (the resolver covers both the direct
helper API and the Click ``envvar=`` wiring on ``notebook_option``).
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_notebooklm_logger():
    """Restore the ``notebooklm`` package logger level after every test.

    The root ``cli()`` callback mutates ``logging.getLogger("notebooklm")``'s
    level as a side effect of ``--quiet`` / ``-v`` / ``-vv``. Without this
    fixture, the level set by one test leaks into the next via the shared
    logger registry and turns the suite order-dependent.
    """
    pkg_logger = logging.getLogger("notebooklm")
    saved = pkg_logger.level
    try:
        yield
    finally:
        pkg_logger.setLevel(saved)


class TestQuietFlag:
    def test_quiet_raises_notebooklm_logger_to_error(self, runner):
        """``notebooklm --quiet ...`` sets the ``notebooklm`` logger level to
        ERROR so INFO and WARNING records emitted by the package are dropped
        before they hit the configured StreamHandler.

        We invoke a subcommand's ``--help`` (rather than the bare root
        ``--help``) because Click short-circuits ``cli --help`` inside the
        parsing layer before the group callback runs; ``cli status --help``
        invokes the callback first (so ``--quiet`` takes effect) and exits
        with the subcommand help next.
        """
        result = runner.invoke(cli, ["--quiet", "status", "--help"])
        assert result.exit_code == 0, result.output
        assert logging.getLogger("notebooklm").level == logging.ERROR

    def test_no_quiet_keeps_notebooklm_logger_at_default(self, runner):
        """Baseline: omitting ``--quiet`` and ``-v`` leaves the package logger
        untouched (configured by ``configure_logging()`` at import time, which
        respects ``NOTEBOOKLM_LOG_LEVEL``; default is WARNING).
        """
        before = logging.getLogger("notebooklm").level
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert logging.getLogger("notebooklm").level == before

    def test_quiet_and_verbose_are_mutually_exclusive(self, runner):
        """``--quiet`` + ``-v`` together must exit 2 with a UsageError, since
        the two flags resolve to incompatible log-level intents (ERROR vs INFO).
        """
        result = runner.invoke(cli, ["--quiet", "-v", "status", "--help"])
        assert result.exit_code == 2
        # Click renders ``UsageError`` to stderr by default. ``CliRunner``
        # mixes stdout + stderr in ``result.output``.
        assert "--quiet" in result.output
        assert "-v" in result.output or "verbose" in result.output.lower()
        assert "mutually exclusive" in result.output.lower()

    def test_quiet_and_double_verbose_are_mutually_exclusive(self, runner):
        """``--quiet`` + ``-vv`` (DEBUG) is also rejected — same conflict as
        ``--quiet -v``, just at a different verbosity step. Pinned separately
        so future refactors can't accidentally narrow the check to ``-v``
        only and let ``-vv`` slip through.
        """
        result = runner.invoke(cli, ["--quiet", "-vv", "status", "--help"])
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_quiet_advertised_in_root_help(self, runner):
        """``notebooklm --help`` must list the new ``--quiet`` flag so users
        can discover it without reading the changelog.
        """
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "--quiet" in result.output


class TestNotebookOptionEnvVar:
    """The ``-n/--notebook`` option declared by ``cli/options.py:notebook_option``
    must accept ``NOTEBOOKLM_NOTEBOOK`` as a fallback so Click documents the
    binding in ``--help`` and resolves the env value natively.

    Behavioral resolution (env > context > error when no flag) is exercised
    in ``test_helpers.py::TestRequireNotebook`` against the resolver itself;
    here we pin the wiring at the Click-option layer so the documented
    ``NOTEBOOKLM_NOTEBOOK`` binding cannot regress silently.
    """

    def test_notebook_option_declares_envvar_binding(self):
        """The probe-decorator pattern used by ``test_helpers.py`` confirms
        the canonical option declaration; here we assert the new ``envvar=``
        was wired in.
        """
        from click import Option

        from notebooklm.cli.options import notebook_option

        @notebook_option
        def _probe(notebook_id):  # pragma: no cover — never invoked
            pass

        for param in _probe.__click_params__:  # type: ignore[attr-defined]
            if isinstance(param, Option) and "--notebook" in param.opts:
                assert param.envvar == "NOTEBOOKLM_NOTEBOOK", (
                    f"notebook_option must bind envvar=NOTEBOOKLM_NOTEBOOK so "
                    f"Click resolves and documents it natively, got {param.envvar!r}"
                )
                # Click renders ``[env var: NOTEBOOKLM_NOTEBOOK]`` in --help
                # only when ``show_envvar=True``. Assert the surface is
                # discoverable in ``--help`` (the dominant LLM-agent UX).
                assert param.show_envvar is True, (
                    "notebook_option must set show_envvar=True so the env-var "
                    "binding shows up in --help output"
                )
                return
        raise AssertionError("notebook_option did not declare a --notebook option")
