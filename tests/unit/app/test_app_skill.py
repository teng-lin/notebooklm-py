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

import io
import re
import zipfile
from pathlib import Path
from unittest.mock import patch

from notebooklm._app.skill import (
    SCOPES,
    SKILL_ARCHIVE_DIRNAME,
    SKILL_ARCHIVE_ENTRY,
    SKILL_ENTRY,
    TARGET_CREATE,
    TARGET_OVERWRITE,
    TARGET_UP_TO_DATE,
    TARGETS,
    add_version_comment,
    build_skill_archive_bytes,
    classify_target,
    get_installed_content,
    get_scope_root,
    get_skill_path,
    get_skill_version,
    iter_targets,
    remove_empty_parents,
    report_mixed_no_clobber_up_to_date,
    stamp_skill_files,
)

# ---------------------------------------------------------------------------
# get_skill_version (MOVED from TestSkillVersionExtraction)
#
# ``get_skill_version`` takes the target's install *directory* and reads its
# ``SKILL_ENTRY`` (``SKILL.md``) file internally.
# ---------------------------------------------------------------------------


def test_get_skill_version_extracts_version(tmp_path: Path) -> None:
    skill_dir = tmp_path / "notebooklm"
    skill_dir.mkdir()
    (skill_dir / SKILL_ENTRY).write_text(
        "---\nname: test\n---\n<!-- notebooklm-py v1.2.3 -->\n# Test"
    )

    assert get_skill_version(skill_dir) == "1.2.3"


def test_get_skill_version_no_version(tmp_path: Path) -> None:
    skill_dir = tmp_path / "notebooklm"
    skill_dir.mkdir()
    (skill_dir / SKILL_ENTRY).write_text("# Test\nNo version here")

    assert get_skill_version(skill_dir) is None


def test_get_skill_version_file_not_exists(tmp_path: Path) -> None:
    assert get_skill_version(tmp_path / "nonexistent") is None


def test_get_skill_version_dir_exists_but_entry_missing(tmp_path: Path) -> None:
    """An installed directory with no ``SKILL.md`` inside reports no version."""
    skill_dir = tmp_path / "notebooklm"
    skill_dir.mkdir()
    (skill_dir / "references").mkdir()

    assert get_skill_version(skill_dir) is None


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
    """A stamped entry file is readable back by ``get_skill_version`` (paired contract)."""
    stamped = add_version_comment("---\nname: nb\n---\n# Body", "3.4.5")
    skill_dir = tmp_path / "notebooklm"
    skill_dir.mkdir()
    (skill_dir / SKILL_ENTRY).write_text(stamped, encoding="utf-8")
    assert get_skill_version(skill_dir) == "3.4.5"


# ---------------------------------------------------------------------------
# stamp_skill_files (net-new direct coverage)
# ---------------------------------------------------------------------------


def test_stamp_skill_files_stamps_only_the_entry_file() -> None:
    """Only ``SKILL_ENTRY`` gets the version comment; other files pass through."""
    files = {
        "SKILL.md": "---\nname: nb\n---\n# Body",
        "references/setup.md": "setup body",
        "scripts/nlm": "#!/bin/sh\n",
    }

    stamped = stamp_skill_files(files, "1.2.3")

    assert stamped["SKILL.md"] == add_version_comment(files["SKILL.md"], "1.2.3")
    assert "<!-- notebooklm-py v1.2.3 -->" in stamped["SKILL.md"]
    assert stamped["references/setup.md"] == "setup body"
    assert stamped["scripts/nlm"] == "#!/bin/sh\n"


def test_stamp_skill_files_preserves_the_key_set() -> None:
    """Stamping never adds or drops files from the tree."""
    files = {"SKILL.md": "body", "scripts/nlm": "launcher", "references/a.md": "a"}

    assert set(stamp_skill_files(files, "9.9.9")) == set(files)


def test_stamp_skill_files_empty_tree_stays_empty() -> None:
    assert stamp_skill_files({}, "1.0.0") == {}


# ---------------------------------------------------------------------------
# get_installed_content (net-new direct coverage)
# ---------------------------------------------------------------------------


def test_get_installed_content_reads_entry_file(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        skill_dir = get_skill_path("claude", "user")
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_ENTRY).write_text("# Installed body", encoding="utf-8")

        assert get_installed_content("claude", "user") == "# Installed body"


def test_get_installed_content_missing_directory_returns_none(tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        assert get_installed_content("claude", "user") is None


def test_get_installed_content_directory_without_entry_returns_none(tmp_path: Path) -> None:
    """The directory exists (e.g. only references/ was written) but SKILL.md is absent."""
    with patch.object(Path, "home", return_value=tmp_path):
        skill_dir = get_skill_path("agents", "user")
        skill_dir.mkdir(parents=True)

        assert get_installed_content("agents", "user") is None


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
#
# ``classify_target``'s third argument is now the full stamped file tree
# (``{relative_posix_path: content}``), and the target path it classifies is
# a *directory* -- ``TARGET_UP_TO_DATE`` requires every stamped file to exist
# with byte-identical content AND no extra installed files.
# ---------------------------------------------------------------------------


def test_classify_target_create_when_missing(tmp_path: Path) -> None:
    with patch.object(Path, "cwd", return_value=tmp_path):
        status, path = classify_target("agents", "project", {"SKILL.md": "stamped body"})
    assert status == TARGET_CREATE
    assert path == tmp_path / TARGETS["agents"].relative_path
    assert not path.exists()


def test_classify_target_up_to_date_when_identical(tmp_path: Path) -> None:
    path = tmp_path / TARGETS["claude"].relative_path
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("stamped body", encoding="utf-8")
    (path / "references").mkdir()
    (path / "references" / "setup.md").write_text("setup body", encoding="utf-8")
    stamped_files = {"SKILL.md": "stamped body", "references/setup.md": "setup body"}

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", stamped_files)

    assert status == TARGET_UP_TO_DATE
    assert resolved == path


def test_classify_target_overwrite_when_differing(tmp_path: Path) -> None:
    path = tmp_path / TARGETS["claude"].relative_path
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("old body", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", {"SKILL.md": "stamped body"})

    assert status == TARGET_OVERWRITE
    assert resolved == path


def test_classify_target_overwrite_when_extra_file_present(tmp_path: Path) -> None:
    """A stale leftover file outside the stamped set forces OVERWRITE."""
    path = tmp_path / TARGETS["claude"].relative_path
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("stamped body", encoding="utf-8")
    (path / "stale.md").write_text("leftover from an old skill version", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", {"SKILL.md": "stamped body"})

    assert status == TARGET_OVERWRITE
    assert resolved == path


def test_classify_target_overwrite_when_expected_file_missing(tmp_path: Path) -> None:
    """A stamped file that has not been written yet also forces OVERWRITE."""
    path = tmp_path / TARGETS["claude"].relative_path
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("stamped body", encoding="utf-8")
    stamped_files = {"SKILL.md": "stamped body", "references/setup.md": "setup body"}

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", stamped_files)

    assert status == TARGET_OVERWRITE
    assert resolved == path


def test_classify_target_overwrite_when_path_is_a_file(tmp_path: Path) -> None:
    """A stray file occupying the target directory's path can't be diffed -- OVERWRITE.

    Mirrors an old single-file skill install colliding with the new
    directory-shaped target path.
    """
    path = tmp_path / TARGETS["claude"].relative_path
    path.parent.mkdir(parents=True)
    path.write_text("blocker", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", {"SKILL.md": "stamped body"})

    assert status == TARGET_OVERWRITE
    assert resolved == path


def test_classify_target_up_to_date_with_empty_stamped_tree(tmp_path: Path) -> None:
    """An existing empty target directory matches an (edge-case) empty stamped tree."""
    path = tmp_path / TARGETS["claude"].relative_path
    path.mkdir(parents=True)

    with patch.object(Path, "cwd", return_value=tmp_path):
        status, resolved = classify_target("claude", "project", {})

    assert status == TARGET_UP_TO_DATE
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


# ---------------------------------------------------------------------------
# build_skill_archive_bytes (Claude-uploadable skill archive)
# ---------------------------------------------------------------------------

_STAMPED_FILES = {
    "SKILL.md": "---\nname: notebooklm\ndescription: test\n---\n<!-- notebooklm-py v1.2.3 -->\n# Body\n",
    "references/setup.md": "# Setup\n",
    "scripts/nlm": "#!/usr/bin/env bash\necho hi\n",
}


def test_build_skill_archive_bytes_roundtrip() -> None:
    data = build_skill_archive_bytes(_STAMPED_FILES)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {
            f"{SKILL_ARCHIVE_DIRNAME}/{rel_path}" for rel_path in _STAMPED_FILES
        }
        extracted = archive.read(SKILL_ARCHIVE_ENTRY).decode("utf-8")
    assert extracted == _STAMPED_FILES[SKILL_ENTRY]
    # Frontmatter must stay the first bytes for upload validation.
    assert extracted.startswith("---\n")


def test_build_skill_archive_bytes_is_deterministic() -> None:
    assert build_skill_archive_bytes(_STAMPED_FILES) == build_skill_archive_bytes(_STAMPED_FILES)


def test_build_skill_archive_bytes_entries_are_deflated() -> None:
    # ZipFile's archive-level compression default does NOT apply to an
    # explicit ZipInfo; guard against a silent ZIP_STORED regression.
    data = build_skill_archive_bytes(_STAMPED_FILES)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED


def test_build_skill_archive_bytes_pins_entry_metadata() -> None:
    data = build_skill_archive_bytes(_STAMPED_FILES)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(SKILL_ARCHIVE_ENTRY)
    assert info.date_time == (2020, 1, 1, 0, 0, 0)
    assert info.create_system == 3
    assert info.external_attr == 0o100644 << 16


def test_build_skill_archive_bytes_marks_scripts_executable() -> None:
    data = build_skill_archive_bytes(_STAMPED_FILES)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(f"{SKILL_ARCHIVE_DIRNAME}/scripts/nlm")
    assert info.external_attr == 0o100755 << 16


def test_skill_archive_entry_lives_in_archive_dirname() -> None:
    assert f"{SKILL_ARCHIVE_DIRNAME}/SKILL.md" == SKILL_ARCHIVE_ENTRY


def test_skill_archive_dirname_matches_repo_frontmatter_name() -> None:
    # Upload validation requires the zip-root folder name to equal the
    # SKILL.md frontmatter ``name`` (R3 tripwire).
    skill_md = Path(__file__).resolve().parents[3] / "plugin" / "skills" / "notebooklm" / "SKILL.md"
    frontmatter = skill_md.read_text(encoding="utf-8").split("---", 2)[1]
    match = re.search(r"^name:\s*(.*)$", frontmatter, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == SKILL_ARCHIVE_DIRNAME
