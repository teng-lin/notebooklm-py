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
# without depending on pytest's special ``conftest`` import-name resolution or
# unwrapping the autouse fixture.
_spec = importlib.util.spec_from_file_location(
    "tests_root_conftest", Path(__file__).resolve().parents[1] / "conftest.py"
)
if _spec is None or _spec.loader is None:  # pragma: no cover - import wiring guard
    raise ImportError("Could not load tests/conftest.py via importlib")
_root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root_conftest)
_should_use_real_home = _root_conftest._should_use_real_home
_vcr_recording = _root_conftest._vcr_recording

# The canonical record-mode check the root conftest mirrors — loaded the same way
# so the parity test below can assert the two never disagree.
_vcr_spec = importlib.util.spec_from_file_location(
    "tests_vcr_config_for_parity", Path(__file__).resolve().parents[1] / "vcr_config.py"
)
if _vcr_spec is None or _vcr_spec.loader is None:  # pragma: no cover - import wiring guard
    raise ImportError("Could not load tests/vcr_config.py via importlib")
_vcr_config = importlib.util.module_from_spec(_vcr_spec)
_vcr_spec.loader.exec_module(_vcr_config)
_is_vcr_record_mode = _vcr_config._is_vcr_record_mode

# The fixture delegates its decision to this plain function (path to pin, or
# ``None`` to keep the real profile) — directly callable with a fake request.
_isolation_home = _root_conftest._isolation_home


class _FakeNode:
    def __init__(self, markers: set[str]) -> None:
        self._markers = markers

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None


class _FakeRequest:
    def __init__(self, markers: set[str]) -> None:
        self.node = _FakeNode(markers)


def _resolved_home(markers, *, recording, tmp_path, monkeypatch):
    """Run the fixture's decision with a fake request node; return the home it
    would pin (a tmp path) or ``None`` (defer to the real profile)."""
    if recording:
        monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD", "1")
    else:
        monkeypatch.delenv("NOTEBOOKLM_VCR_RECORD", raising=False)
    return _isolation_home(_FakeRequest(markers), tmp_path)


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


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "Yes", "yes", "0", "false", "", "1 ", " 1", "nope"]
)
def test_vcr_recording_matches_canonical(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest's local ``_vcr_recording`` never disagrees with the
    canonical ``vcr_config._is_vcr_record_mode`` it mirrors — otherwise a padded
    value could half-enable recording (real home, but VCR still in replay)."""
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD", value)
    assert _vcr_recording() == _is_vcr_record_mode()


# --- Fixture-wiring regression: the fixture's decision under each mode ----------
# ``_isolation_home`` returns a tmp path to pin (isolated) or ``None`` to leave
# the real profile in place (deferred).


def test_isolation_home_isolates_non_vcr_test_even_under_record_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety property through the real decision path: a stray
    ``NOTEBOOKLM_VCR_RECORD=1`` never un-isolates a non-VCR test."""
    home = _resolved_home(set(), recording=True, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert home is not None and home.endswith("notebooklm-home")


def test_isolation_home_defers_for_vcr_test_in_record_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vcr test in record mode keeps the real profile (``None``) so recording
    resolves real auth."""
    assert (
        _resolved_home({"vcr"}, recording=True, tmp_path=tmp_path, monkeypatch=monkeypatch) is None
    )


def test_isolation_home_isolates_vcr_test_in_replay_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vcr test in REPLAY mode (the CI default) is still isolated."""
    home = _resolved_home({"vcr"}, recording=False, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert home is not None and home.endswith("notebooklm-home")
