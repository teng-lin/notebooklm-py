from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "select_ci_account.py"
SPEC = importlib.util.spec_from_file_location("select_ci_account", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
selector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selector
SPEC.loader.exec_module(selector)


@pytest.mark.parametrize(
    ("slots", "day", "expected"),
    [
        ("A", "2026-09-02", "A"),
        ("A,B", "1970-01-01", "A"),
        ("A,B", "1970-01-02", "B"),
        ("A,B,C", "1970-01-03", "C"),
        ("A,B,C", "1970-01-04", "A"),
    ],
)
def test_rotation_across_pool_sizes(slots: str, day: str, expected: str) -> None:
    result = selector.select_account(
        enabled_slots=slots,
        lane="nightly-web-ubuntu",
        rotation_day=day,
    )
    assert result["account_slot"] == expected


@pytest.mark.parametrize(
    ("lane", "expected"),
    [
        ("nightly-web-ubuntu", "A"),
        ("nightly-android-macos", "B"),
        ("nightly-readonly-windows", "C"),
        ("rpc-health-web", "C"),
        ("rpc-health-android", "A"),
        ("verify-package", "A"),
    ],
)
def test_every_lane_offset_wraps(lane: str, expected: str) -> None:
    result = selector.select_account(enabled_slots="A,B,C", lane=lane, rotation_day="1970-01-01")
    assert result["account_slot"] == expected


def test_manual_base_preserves_lane_offset() -> None:
    assert (
        selector.select_account(
            enabled_slots="A,B,C",
            lane="nightly-android-macos",
            rotation_day="2026-09-02",
            manual_base="C",
        )["account_slot"]
        == "A"
    )
    assert (
        selector.select_account(
            enabled_slots="A,B,C",
            lane="verify-package",
            rotation_day="2026-09-02",
            manual_base="C",
        )["account_slot"]
        == "C"
    )


@pytest.mark.parametrize(
    "slots",
    [
        None,
        "",
        "A,A",
        "a",
        "D",
        "A, B",
        "A,",
        ",A",
        "A,B,C,A",
        "ABC",
        "B,A",
        "C,B",
        "C,A",
    ],
)
def test_malformed_pool_is_rejected(slots: str | None) -> None:
    with pytest.raises(selector.ConfigurationError):
        selector.select_account(
            enabled_slots=slots,
            lane="nightly-web-ubuntu",
            rotation_day="2026-09-02",
        )


def test_disabled_manual_base_is_rejected() -> None:
    with pytest.raises(selector.ConfigurationError, match="not enabled"):
        selector.select_account(
            enabled_slots="A,B",
            lane="verify-package",
            rotation_day="2026-09-02",
            manual_base="C",
        )


def test_epoch_day_is_timezone_independent(monkeypatch) -> None:
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Pacific/Kiritimati")
        if hasattr(os, "tzset"):
            os.tzset()
        assert selector.utc_epoch_day("1970-01-01") == 0
        assert selector.utc_epoch_day("1970-01-02") == 1
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(os, "tzset"):
            os.tzset()


def test_versioned_four_key_json_and_github_outputs_are_stable(tmp_path, capsys) -> None:
    output = tmp_path / "github-output"
    rc = selector.main(
        [
            "--enabled-slots",
            "A,B",
            "--lane",
            "rpc-health-web",
            "--rotation-day",
            "1970-01-01",
            "--github-output",
            str(output),
            "--json",
        ]
    )
    assert rc == 0
    record = json.loads(capsys.readouterr().out)
    assert record == {
        "account_slot": "A",
        "lane": "rpc-health-web",
        "master_token_secret_name": "NOTEBOOKLM_MASTER_TOKEN_JSON_A",
        "rotation_day": "1970-01-01",
    }
    assert selector.SCHEMA_VERSION == 1
    assert output.read_text().splitlines() == [
        "account_slot=A",
        "master_token_secret_name=NOTEBOOKLM_MASTER_TOKEN_JSON_A",
        "lane=rpc-health-web",
        "rotation_day=1970-01-01",
    ]
    combined = output.read_text() + json.dumps(record)
    assert "refresh_token" not in combined
    assert "master_token_secret_name" in combined
