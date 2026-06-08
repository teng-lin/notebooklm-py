"""Tests for ``notebooklm._app.skill`` (transport-neutral skill-install core).

These exercise the Click-free skill core directly — version extraction /
stamping, scope/path resolution, the ``create`` / ``up_to_date`` /
``overwrite`` per-target classification, target expansion, and the mixed
``--no-clobber`` reporting decision — with no Click / ``CliRunner``. The
file-write + packaged-source loader stay in ``cli/skill_cmd.py``; the
``CliRunner``-driven install/uninstall/status/show behavior stays in
``tests/unit/cli/test_skill.py``.

The version/comment/source-fallback/reporting cases were MOVED down from
``test_skill.py`` (they already called these functions directly through the
``cli.skill_cmd`` re-export); the classification / path / expansion cases are
net-new direct coverage of the neutral surface.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from notebooklm._app.skill import (
    SCOPES,
    TARGET_CREATE,
    TARGET_OVERWRITE,
    TARGET_UP_TO_DATE,
    TARGETS,
    add_version_comment,
    classify_target,
    get_scope_root,
    get_skill_path,
    get_skill_version,
    iter_targets,
    remove_empty_parents,
    report_mixed_no_clobber_up_to_date,
)

# ---------------------------------------------------------------------------
# get_skill_version (MOVED from TestSkillVersionExtraction)
# ---------------------------------------------------------------------------


def test_get_skill_version_extracts_version(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("---\nname: test\n---\n<!-- notebooklm-py v1.2.3 -->\n# Test")

    assert get_skill_version(skill_file) == "1.2.3"


def test_get_skill_version_no_version(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Test\nNo version here")

    assert get_skill_version(skill_file) is None


def test_get_skill_version_file_not_exists(tmp_path: Path) -> None:
    assert get_skill_version(tmp_path / "nonexistent.md") is None


# ---------------------------------------------------------------------------
# add_version_comment (MOVED from TestAddVersionComment)
# ---------------------------------------------------------------------------


def test_add_version_comment_inserts_after_frontmatter() -> None:
    content = "---\nname: notebooklm\n---\n# Body"
    result = add_version_comment(content, "1.2.3")
    assert result == "---\nname: notebooklm\n---\n<!-- notebooklm-py v1.2.3 -->\n# Body"


def test_add_version_comment_prepends_when_no_frontmatter() -> None:
    content = "# No Frontmatter\nBody text"
    result = add_version_comment(content, "2.0.0")
    assert result == "<!-- notebooklm-py v2.0.0 -->\n# No Frontmatter\nBody text"


def test_add_version_comment_prepends_with_incomplete_frontmatter() -> None:
    content = "---\nbroken frontmatter"
    result = add_version_comment(content, "1.0.0")
    assert result == "<!-- notebooklm-py v1.0.0 -->\n---\nbroken frontmatter"


def test_add_version_comment_roundtrips_with_get_skill_version(tmp_path: Path) -> None:
    """A stamped file is readable back by ``get_skill_version`` (paired contract)."""
    stamped = add_version_comment("---\nname: nb\n---\n# Body", "3.4.5")
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(stamped, encoding="utf-8")
    assert get_skill_version(skill_file) == "3.4.5"


# ---------------------------------------------------------------------------
# report_mixed_no_clobber_up_to_date (MOVED from TestSkillInstallReporting)
# ---------------------------------------------------------------------------


def test_reports_mixed_no_clobber_up_to_date_targets() -> None:
    """No-write mixed --no-clobber state reports synced targets separately."""
    messages: list[str] = []

    report_mixed_no_clobber_up_to_date(
        messages.append,
        skipped_up_to_date=[object()],
        skipped_no_clobber=[object()],
        installed_paths=[],
        failed_targets=[],
    )

    assert messages == ["[green]Up to date[/green] 1 target(s)"]


def test_reporting_skips_message_when_install_wrote_a_target() -> None:
    """The mixed no-write message is suppressed after any install success."""
    messages: list[str] = []

    report_mixed_no_clobber_up_to_date(
        messages.append,
        skipped_up_to_date=[object()],
        skipped_no_clobber=[object()],
        installed_paths=[object()],
        failed_targets=[],
    )

    assert messages == []


def test_reporting_skips_message_when_a_target_failed() -> None:
    """A failed target also suppresses the mixed no-write up-to-date summary."""
    messages: list[str] = []

    report_mixed_no_clobber_up_to_date(
        messages.append,
        skipped_up_to_date=[object()],
        skipped_no_clobber=[object()],
        installed_paths=[],
        failed_targets=[object()],
    )

    assert messages == []


def test_reporting_skips_message_when_nothing_was_no_clobber_skipped() -> None:
    """Without a no-clobber skip there is no 'mixed' state to report."""
    messages: list[str] = []

    report_mixed_no_clobber_up_to_date(
        messages.append,
        skipped_up_to_date=[object()],
        skipped_no_clobber=[],
        installed_paths=[],
        failed_targets=[],
    )

    assert messages == []


def test_reporting_counts_all_up_to_date_targets() -> None:
    """The reported count reflects every up-to-date target, not just one."""
    messages: list[str] = []

    report_mixed_no_clobber_up_to_date(
        messages.append,
        skipped_up_to_date=[object(), object(), object()],
        skipped_no_clobber=[object()],
        installed_paths=[],
        failed_targets=[],
    )

    assert messages == ["[green]Up to date[/green] 3 target(s)"]


# ---------------------------------------------------------------------------
# classify_target (net-new direct coverage)
# ---------------------------------------------------------------------------


def test_classify_target_create_when_missing(tmp_path: Path) -> None:
    with patch.object(Path, "cwd", return_value=tmp_path):
        status, path = classify_target("agents", "project", "stamped body")
    assert status == TARGET_CREATE
    assert path == tmp_path / TARGETS["agents"].relative_path
    assert not path.exists()


def test_classify_target_up_to_date_when_identical(tmp_path: Path) -> None:
    path = tmp_path / TARGETS["claude"].relative_path
    path.parent.mkdir(parents=True)
    path.write_text("stamped body", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", "stamped body")

    assert status == TARGET_UP_TO_DATE
    assert resolved == path


def test_classify_target_overwrite_when_differing(tmp_path: Path) -> None:
    path = tmp_path / TARGETS["claude"].relative_path
    path.parent.mkdir(parents=True)
    path.write_text("old body", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", "stamped body")

    assert status == TARGET_OVERWRITE
    assert resolved == path


# ---------------------------------------------------------------------------
# get_scope_root / get_skill_path / iter_targets (net-new direct coverage)
# ---------------------------------------------------------------------------


def test_get_scope_root_user_uses_home(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        assert get_scope_root("user") == tmp_path


def test_get_scope_root_project_uses_cwd(tmp_path: Path) -> None:
    with patch.object(Path, "cwd", return_value=tmp_path):
        assert get_scope_root("project") == tmp_path


def test_get_skill_path_joins_scope_root_and_relative(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        for target in TARGETS:
            assert get_skill_path(target, "user") == tmp_path / TARGETS[target].relative_path


def test_iter_targets_expands_all_to_every_target() -> None:
    assert iter_targets("all") == list(TARGETS)


def test_iter_targets_passes_through_a_concrete_target() -> None:
    assert iter_targets("claude") == ["claude"]


def test_scopes_catalog_is_user_and_project() -> None:
    assert SCOPES == ("user", "project")


# ---------------------------------------------------------------------------
# remove_empty_parents (net-new direct coverage of the neutral helper)
# ---------------------------------------------------------------------------


def test_remove_empty_parents_cleans_up_to_scope_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skill_path = home / ".claude" / "skills" / "notebooklm" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)

    with patch.object(Path, "home", return_value=home):
        remove_empty_parents(skill_path, "user")

    assert not (home / ".claude" / "skills" / "notebooklm").exists()
    assert not (home / ".claude" / "skills").exists()
    assert home.exists()  # scope root must survive


def test_remove_empty_parents_stops_at_non_empty_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills = home / ".agents" / "skills"
    (skills / "notebooklm").mkdir(parents=True)
    (skills / "other.md").write_text("keep me", encoding="utf-8")

    with patch.object(Path, "home", return_value=home):
        remove_empty_parents(skills / "notebooklm" / "SKILL.md", "user")

    assert skills.exists()  # non-empty, must not be removed


def test_remove_empty_parents_never_removes_scope_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # A skill directly one level inside the scope root (no intermediates).
    skill_path = home / "SKILL.md"

    with patch.object(Path, "home", return_value=home):
        remove_empty_parents(skill_path, "user")

    assert home.exists()
