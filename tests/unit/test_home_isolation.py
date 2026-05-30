"""Tests for the ``NOTEBOOKLM_HOME`` isolation opt-outs (issue #1263).

The autouse ``_isolate_notebooklm_home`` fixture pins ``NOTEBOOKLM_HOME`` at a
per-test tmp dir for reproducibility. Two opt-outs use the developer's real
``~/.notebooklm`` profile instead: ``@pytest.mark.e2e`` tests (always) and
``@pytest.mark.vcr`` tests *while recording* (``NOTEBOOKLM_VCR_RECORD=1``), so a
contributor can record a cassette through pytest instead of a standalone script.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# Load the root conftest by file path to reach its module-level decision helper
# without depending on pytest's ``conftest`` import-name resolution. This is the
# same file-path idiom the conftest itself uses for ``vcr_config`` /
# ``cassette_patterns`` (``tests/`` is not a package).
_spec = importlib.util.spec_from_file_location(
    "tests_root_conftest", Path(__file__).resolve().parents[1] / "conftest.py"
)
assert _spec is not None and _spec.loader is not None
_root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root_conftest)
_should_use_real_home = _root_conftest._should_use_real_home


@pytest.mark.parametrize(
    ("e2e", "vcr", "recording", "expected"),
    [
        # Plain unit/integration test → always isolated, even if someone runs
        # the whole suite with NOTEBOOKLM_VCR_RECORD=1 set (a non-VCR test is
        # never un-isolated, so the real profile is never touched by accident).
        (False, False, False, False),
        (False, False, True, False),
        # VCR test → isolated on replay (the CI default), real home only when
        # actually recording.
        (False, True, False, False),
        (False, True, True, True),
        # E2E test → always the real profile (mints live tokens).
        (True, False, False, True),
        (True, False, True, True),
        (True, True, False, True),
        (True, True, True, True),
    ],
)
def test_should_use_real_home_truth_table(
    e2e: bool, vcr: bool, recording: bool, expected: bool
) -> None:
    assert _should_use_real_home(e2e=e2e, vcr=vcr, recording=recording) is expected


def test_normal_test_home_is_isolated() -> None:
    """A normal (non-e2e, non-vcr) test sees the isolated tmp NOTEBOOKLM_HOME.

    Guards the safety property the fix must preserve: the default path still
    points at a per-test tmp dir, never the developer's real ``~/.notebooklm``.
    """
    home = os.environ.get("NOTEBOOKLM_HOME", "")
    assert home.endswith("notebooklm-home"), home
    assert Path(home) != Path.home() / ".notebooklm"
