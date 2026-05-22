"""Characterization (golden-snapshot) tests for ``notebooklm generate`` commands.

These tests lock in the exact byte-for-byte output of every ``generate``
subcommand BEFORE the P3.T1 service-layer extraction lands, so the
extraction commit can prove handler-regression-free behavior. They cover:

* happy-path text + JSON output for each of the 10 leaf generate commands
* the rate-limited (retry-exhausted) error path
* the ``None``-result generation-failed error path
* video / cinematic-video usage-error message text
* report's "smart custom" format coercion and ``--append`` warning text

Test discipline (phase-3.md → Characterization-Test Discipline):

* This file MUST pass on the PR's branch base (main) **before** the
  extraction commit lands. Run with ``uv run pytest
  tests/unit/cli/test_generate_characterization.py -q`` against pre-T1
  main to confirm; the same suite must remain byte-stable after T1.
* Snapshots are stored as module-level string constants. Any drift in
  Click usage messages, JSON formatting, or status-line wording will
  surface here.
* JSON snapshots are compared as parsed dicts (order/whitespace
  resilient where contract is structural, not formatting); text
  snapshots are compared as exact strings.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner, Result

from notebooklm.notebooklm_cli import cli
from notebooklm.types import GenerationStatus

from .conftest import create_mock_client

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_AUTH_PAYLOAD = {
    "SID": "t",
    "__Secure-1PSIDTS": "t",
    "HSID": "t",
    "SSID": "t",
    "APISID": "t",
    "SAPISID": "t",
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def authed_invoke(
    runner: CliRunner,
) -> Iterator[Callable[..., Result]]:
    """Yield an ``invoke(args, *, configure=None)`` helper with auth seams mocked.

    ``configure`` (when supplied) receives a fresh mock client (already
    attached to the patched ``NotebookLMClient`` constructor) and is
    expected to attach the artifact-generation AsyncMock attributes the
    test needs. When ``configure`` is ``None`` (usage-error tests that
    never reach the API call), the mock client is still installed so the
    auth bootstrap path succeeds; the API methods stay as default
    ``MagicMock``s and are never invoked.
    """

    def _invoke(
        args: list[str],
        *,
        configure: Callable[[Any], None] | None = None,
    ) -> Result:
        mock_client = create_mock_client()
        if configure is not None:
            configure(mock_client)
        with (
            patch("notebooklm.cli.generate_cmd.NotebookLMClient") as mock_cls,
            patch(
                "notebooklm.cli.helpers.load_auth_from_storage",
                return_value=_AUTH_PAYLOAD,
            ),
            patch(
                "notebooklm.auth.fetch_tokens_with_domains",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            mock_cls.return_value = mock_client
            mock_fetch.return_value = ("csrf", "session")
            return runner.invoke(cli, args)

    yield _invoke


def _attach_async_return(method_name: str, value: Any) -> Callable[[Any], None]:
    """Build a ``configure`` callable that sets ``artifacts.<method>`` mock."""

    def _apply(mock_client: Any) -> None:
        setattr(mock_client.artifacts, method_name, AsyncMock(return_value=value))

    return _apply


# ---------------------------------------------------------------------------
# Happy-path golden output: text and JSON
# ---------------------------------------------------------------------------

# Mapping (CLI subcommand, ``client.artifacts.*`` method name, extra args, task_id).
# The 10 leaf generate commands per phase-3.md → P3.T1.
HAPPY_PATH_CASES: list[tuple[str, str, list[str], str]] = [
    ("audio", "generate_audio", [], "task_audio"),
    ("video", "generate_video", [], "task_video"),
    ("cinematic-video", "generate_cinematic_video", [], "task_cv"),
    ("slide-deck", "generate_slide_deck", [], "task_slides"),
    (
        "revise-slide",
        "revise_slide",
        ["Move title up", "--artifact", "art_1", "--slide", "0"],
        "task_revise",
    ),
    ("quiz", "generate_quiz", [], "task_quiz"),
    ("flashcards", "generate_flashcards", [], "task_flash"),
    ("infographic", "generate_infographic", [], "task_info"),
    ("data-table", "generate_data_table", ["Compare key concepts"], "task_dtable"),
    ("report", "generate_report", [], "task_report"),
]


@pytest.mark.parametrize("cmd,method,extra_args,task_id", HAPPY_PATH_CASES)
def test_generate_happy_path_json_snapshot(
    cmd: str,
    method: str,
    extra_args: list[str],
    task_id: str,
    authed_invoke: Callable[..., Result],
) -> None:
    """JSON output for the 10 generate commands is exactly
    ``{"task_id": <id>, "status": "pending"}`` on a no-wait happy path."""
    result = authed_invoke(
        ["generate", cmd, "--json", "-n", "nb_123", *extra_args],
        configure=_attach_async_return(method, {"task_id": task_id, "status": "processing"}),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"task_id": task_id, "status": "pending"}


@pytest.mark.parametrize("cmd,method,extra_args,task_id", HAPPY_PATH_CASES)
def test_generate_happy_path_text_snapshot(
    cmd: str,
    method: str,
    extra_args: list[str],
    task_id: str,
    authed_invoke: Callable[..., Result],
) -> None:
    """Text output for the 10 generate commands is exactly ``Started: <task_id>\\n``
    on a no-wait happy path."""
    result = authed_invoke(
        ["generate", cmd, "-n", "nb_123", *extra_args],
        configure=_attach_async_return(method, {"task_id": task_id, "status": "processing"}),
    )
    assert result.exit_code == 0, result.output
    assert result.output == f"Started: {task_id}\n"


# ---------------------------------------------------------------------------
# Mind-map happy path (its own output shape: full result echoed)
# ---------------------------------------------------------------------------


def test_generate_mind_map_json_snapshot(authed_invoke: Callable[..., Result]) -> None:
    """Mind-map JSON output is the full result dict pretty-printed."""
    payload = {
        "note_id": "n1",
        "mind_map": {"name": "Root", "children": [{"a": 1}, {"b": 2}]},
    }
    result = authed_invoke(
        ["generate", "mind-map", "--json", "-n", "nb_123"],
        configure=_attach_async_return("generate_mind_map", payload),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload


def test_generate_mind_map_text_snapshot(authed_invoke: Callable[..., Result]) -> None:
    """Mind-map text output is the 3-line ``Note ID / Root / Children`` block."""
    payload = {
        "note_id": "n1",
        "mind_map": {"name": "Root", "children": [{"a": 1}, {"b": 2}]},
    }
    result = authed_invoke(
        ["generate", "mind-map", "-n", "nb_123"],
        configure=_attach_async_return("generate_mind_map", payload),
    )
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Mind map generated:\n  Note ID: n1\n  Root: Root\n  Children: 2 nodes\n"
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_generate_audio_failure_none_result_json(
    authed_invoke: Callable[..., Result],
) -> None:
    """When the generation API returns ``None`` the CLI emits a structured JSON
    error and exits non-zero."""
    result = authed_invoke(
        ["generate", "audio", "--json", "-n", "nb_123"],
        configure=_attach_async_return("generate_audio", None),
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": True,
        "code": "GENERATION_FAILED",
        "message": "Audio generation failed",
    }


def test_generate_audio_failure_none_result_text(
    authed_invoke: Callable[..., Result],
) -> None:
    """When the generation API returns ``None`` text mode prints the
    failure message to stderr and exits non-zero."""
    result = authed_invoke(
        ["generate", "audio", "-n", "nb_123"],
        configure=_attach_async_return("generate_audio", None),
    )
    assert result.exit_code == 1
    # ``output_error`` writes via ``safe_echo(err=True)``. stderr is the
    # canonical sink; ``output`` captures stderr when stderr is unbound.
    assert "Audio generation failed" in result.output


def test_generate_audio_rate_limit_retry_exhausted_json(
    authed_invoke: Callable[..., Result],
) -> None:
    """Retry-exhausted rate-limit (retry=0) returns the RATE_LIMITED JSON
    payload and exits non-zero."""
    rate_limited = GenerationStatus(
        task_id="task_rl",
        status="failed",
        error_code="RATE_LIMIT_EXCEEDED",
        error="rate limited",
    )
    result = authed_invoke(
        ["generate", "audio", "--json", "-n", "nb_123"],
        configure=_attach_async_return("generate_audio", rate_limited),
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": True,
        "code": "RATE_LIMITED",
        "message": "Audio generation rate limited by Google.",
    }


def test_generate_audio_rate_limit_retry_exhausted_text(
    authed_invoke: Callable[..., Result],
) -> None:
    """Retry-exhausted rate-limit (retry=0) text mode prints the rate-limit
    message AND the hint, then exits non-zero."""
    rate_limited = GenerationStatus(
        task_id="task_rl",
        status="failed",
        error_code="RATE_LIMIT_EXCEEDED",
        error="rate limited",
    )
    result = authed_invoke(
        ["generate", "audio", "-n", "nb_123"],
        configure=_attach_async_return("generate_audio", rate_limited),
    )
    assert result.exit_code == 1
    assert result.output == (
        "Audio generation rate limited by Google.\n"
        "Daily quota may be exceeded. Try again in 1-24 hours, "
        "or use --retry N to retry automatically.\n"
    )


# ---------------------------------------------------------------------------
# Video / cinematic-video usage-error text
# ---------------------------------------------------------------------------


def test_video_cinematic_rejects_style_prompt(
    authed_invoke: Callable[..., Result],
) -> None:
    """``--style-prompt`` is rejected when the video format is cinematic."""
    result = authed_invoke(
        [
            "generate",
            "video",
            "--format",
            "cinematic",
            "--style-prompt",
            "foo",
            "-n",
            "nb_123",
        ],
    )
    assert result.exit_code == 2
    assert "--style-prompt cannot be used with cinematic video" in result.output


def test_video_style_custom_requires_style_prompt(
    authed_invoke: Callable[..., Result],
) -> None:
    """``--style custom`` requires ``--style-prompt`` for non-cinematic video."""
    result = authed_invoke(["generate", "video", "--style", "custom", "-n", "nb_123"])
    assert result.exit_code == 2
    assert "--style custom requires --style-prompt" in result.output


def test_video_style_prompt_requires_style_custom(
    authed_invoke: Callable[..., Result],
) -> None:
    """``--style-prompt`` requires ``--style custom`` for non-cinematic video."""
    result = authed_invoke(["generate", "video", "--style-prompt", "foo", "-n", "nb_123"])
    assert result.exit_code == 2
    assert "--style-prompt requires --style custom" in result.output


def test_cinematic_video_alias_rejects_non_cinematic_format(
    authed_invoke: Callable[..., Result],
) -> None:
    """``generate cinematic-video --format explainer`` is rejected with a
    targeted message that points users at ``generate video``."""
    result = authed_invoke(
        [
            "generate",
            "cinematic-video",
            "--format",
            "explainer",
            "-n",
            "nb_123",
        ],
    )
    assert result.exit_code == 2
    assert "--format must be 'cinematic' for the cinematic-video subcommand" in result.output


# ---------------------------------------------------------------------------
# Report "smart-custom" coercion and --append warning
# ---------------------------------------------------------------------------


def test_report_description_triggers_custom_format(
    authed_invoke: Callable[..., Result],
) -> None:
    """A bare description on ``generate report`` (with default --format
    briefing-doc) is smart-detected as a custom report — the API call gets
    ``ReportFormat.CUSTOM`` and the description as ``custom_prompt``."""
    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"task_id": "task_report_custom", "status": "processing"}

    def _configure(mock_client: Any) -> None:
        mock_client.artifacts.generate_report = AsyncMock(side_effect=_capture)

    result = authed_invoke(
        ["generate", "report", "My custom prompt", "-n", "nb_123"],
        configure=_configure,
    )
    assert result.exit_code == 0, result.output
    # ReportFormat.CUSTOM == "custom" by enum string identity.
    assert captured["report_format"].value == "custom"
    assert captured["custom_prompt"] == "My custom prompt"
    assert captured["extra_instructions"] is None


def test_report_custom_with_append_emits_warning(
    authed_invoke: Callable[..., Result],
) -> None:
    """``--append`` is a no-op when ``--format custom`` is in effect; the CLI
    emits a stderr warning and clears the append payload before calling the
    API."""
    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"task_id": "task_report_custom_w", "status": "processing"}

    def _configure(mock_client: Any) -> None:
        mock_client.artifacts.generate_report = AsyncMock(side_effect=_capture)

    result = authed_invoke(
        [
            "generate",
            "report",
            "--format",
            "custom",
            "--append",
            "extra",
            "desc",
            "-n",
            "nb_123",
        ],
        configure=_configure,
    )
    assert result.exit_code == 0, result.output
    assert (
        "Warning: --append has no effect with --format custom. "
        "Use the description argument instead." in result.output
    )
    # Warning side-effect: append is suppressed (CLI sets it to None).
    assert captured["extra_instructions"] is None


# ---------------------------------------------------------------------------
# Group-level help (regression guard on subcommand listing)
# ---------------------------------------------------------------------------


def test_generate_help_lists_all_ten_kinds(runner: CliRunner) -> None:
    """``notebooklm generate --help`` lists every leaf subcommand by name.

    Lock-in protects against accidental command drop during refactor.
    """
    result = runner.invoke(cli, ["generate", "--help"])
    assert result.exit_code == 0
    expected_subcommands = [
        "audio",
        "video",
        "cinematic-video",
        "slide-deck",
        "revise-slide",
        "quiz",
        "flashcards",
        "infographic",
        "data-table",
        "mind-map",
        "report",
    ]
    for name in expected_subcommands:
        assert name in result.output, f"missing subcommand {name!r}: {result.output}"
