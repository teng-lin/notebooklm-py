"""Pytest-only isolated filesystem fault lane; no live credentials or VCR."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("variant", ["write", "replace"])
def test_refresh_recovers_after_atomic_storage_failure(tmp_path: Path, variant: str) -> None:
    report = tmp_path / "report.json"
    # subprocess.run kills and waits on timeout on POSIX and Windows alike.
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._fault_server.auth_persistence_worker",
            "--variant",
            variant,
            "--directory",
            str(tmp_path),
            "--report",
            str(report),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=35,
        check=False,
    )
    assert report.exists(), "auth persistence worker did not settle and report"
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert child.returncode == 0, evidence
    assert evidence["checks"] and all(evidence["checks"].values())
    assert set(evidence["events"][0]["required_checks"]) == set(evidence["checks"])
    assert any(event["kind"] == "cleanup" for event in evidence["events"])
