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
    TARGET_CREATE,
    TARGET_OVERWRITE,
    TARGET_UP_TO_DATE,
    TARGETS,
    add_version_comment,
    build_skill_archive_bytes,
    classify_target,
    get_scope_root,
    get_skill_content_mismatch,
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


def test_get_skill_version_invalid_utf8_is_unknown(tmp_path: Path) -> None:
    """Invalid UTF-8 makes the embedded version unavailable without raising."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_bytes(b"<!-- notebooklm-py v1.2.3 -->\n\xff")

    assert get_skill_version(skill_file) is None


# ---------------------------------------------------------------------------
# get_skill_content_mismatch
# ---------------------------------------------------------------------------


def test_get_skill_content_mismatch_false_for_exact_bytes(tmp_path: Path) -> None:
    """Canonical installed bytes report no drift."""
    skill_file = tmp_path / "SKILL.md"
    canonical = "<!-- notebooklm-py v1.2.3 -->\n# Test"
    skill_file.write_text(canonical, encoding="utf-8")

    assert get_skill_content_mismatch(skill_file, canonical) is False


def test_get_skill_content_mismatch_false_for_windows_crlf(tmp_path: Path) -> None:
    """Platform CRLF translation is equivalent to canonical LF content."""
    skill_file = tmp_path / "SKILL.md"
    canonical = "<!-- notebooklm-py v1.2.3 -->\n# Test\n"
    skill_file.write_bytes(canonical.encode("utf-8").replace(b"\n", b"\r\n"))

    assert get_skill_content_mismatch(skill_file, canonical) is False


def test_get_skill_content_mismatch_true_for_drift(tmp_path: Path) -> None:
    """Differing readable content reports drift."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("locally edited", encoding="utf-8")

    assert get_skill_content_mismatch(skill_file, "canonical") is True


def test_get_skill_content_mismatch_true_for_invalid_utf8(tmp_path: Path) -> None:
    """Malformed installed bytes remain detectable as drift."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_bytes(b"canonical\xff")

    assert get_skill_content_mismatch(skill_file, "canonical") is True


def test_get_skill_content_mismatch_none_when_target_missing(tmp_path: Path) -> None:
    """A missing installed target has unavailable integrity."""
    assert get_skill_content_mismatch(tmp_path / "missing.md", "canonical") is None


def test_get_skill_content_mismatch_none_when_canonical_unavailable(tmp_path: Path) -> None:
    """Missing canonical package content makes comparison unavailable."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("installed", encoding="utf-8")

    assert get_skill_content_mismatch(skill_file, None) is None


def test_get_skill_content_mismatch_none_on_io_failure(tmp_path: Path) -> None:
    """An installed-content read failure makes comparison unavailable."""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("installed", encoding="utf-8")

    with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
        assert get_skill_content_mismatch(skill_file, "canonical") is None


def test_skill_path_probe_failure_returns_unavailable(tmp_path: Path) -> None:
    """An inaccessible target path does not escape version or drift probes."""
    skill_file = tmp_path / "SKILL.md"

    with patch.object(Path, "exists", side_effect=PermissionError("denied")):
        assert get_skill_version(skill_file) is None
        assert get_skill_content_mismatch(skill_file, "canonical") is None


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


# ---------------------------------------------------------------------------
# build_skill_archive_bytes (Claude-uploadable skill archive)
# ---------------------------------------------------------------------------

_STAMPED = "---\nname: notebooklm\ndescription: test\n---\n<!-- notebooklm-py v1.2.3 -->\n# Body\n"


def test_build_skill_archive_bytes_single_entry_roundtrip() -> None:
    data = build_skill_archive_bytes(_STAMPED)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == [SKILL_ARCHIVE_ENTRY]
        extracted = archive.read(SKILL_ARCHIVE_ENTRY).decode("utf-8")
    assert extracted == _STAMPED
    # Frontmatter must stay the first bytes for upload validation.
    assert extracted.startswith("---\n")


def test_build_skill_archive_bytes_is_deterministic() -> None:
    assert build_skill_archive_bytes(_STAMPED) == build_skill_archive_bytes(_STAMPED)


def test_build_skill_archive_bytes_entry_is_deflated() -> None:
    # ZipFile's archive-level compression default does NOT apply to an
    # explicit ZipInfo; guard against a silent ZIP_STORED regression.
    data = build_skill_archive_bytes(_STAMPED)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.getinfo(SKILL_ARCHIVE_ENTRY).compress_type == zipfile.ZIP_DEFLATED


def test_build_skill_archive_bytes_pins_entry_metadata() -> None:
    data = build_skill_archive_bytes(_STAMPED)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo(SKILL_ARCHIVE_ENTRY)
    assert info.date_time == (2020, 1, 1, 0, 0, 0)
    assert info.create_system == 3
    assert info.external_attr == 0o100644 << 16


def test_skill_archive_entry_lives_in_archive_dirname() -> None:
    assert f"{SKILL_ARCHIVE_DIRNAME}/SKILL.md" == SKILL_ARCHIVE_ENTRY


def test_skill_archive_dirname_matches_repo_frontmatter_name() -> None:
    # Upload validation requires the zip-root folder name to equal the
    # SKILL.md frontmatter ``name`` (R3 tripwire).
    skill_md = Path(__file__).resolve().parents[3] / "SKILL.md"
    frontmatter = skill_md.read_text(encoding="utf-8").split("---", 2)[1]
    match = re.search(r"^name:\s*(.*)$", frontmatter, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == SKILL_ARCHIVE_DIRNAME
