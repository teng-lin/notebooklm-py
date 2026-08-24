"""Structural pins for ``scripts/measure_web_backend_chain.py``.

The script re-measures the P9 entry record in
``docs/plan/2026-08-13-semantic-backend-refactor.md``. These tests pin only the
rows that must hold today for the P9 decomposition to start from the documented
shape; they are not a second copy of the table.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import scripts.measure_web_backend_chain as measure_script  # noqa: E402
from scripts.measure_web_backend_chain import (  # noqa: E402
    RPC_CALL_KEYWORDS,
    format_markdown,
    main,
    measure,
)


@pytest.fixture(scope="module")
def measurements() -> dict[str, Any]:
    return measure()


def test_chain_shape_matches_entry_record(measurements: dict[str, Any]) -> None:
    assert measurements["mro_depth"] == 11
    assert measurements["chain"][0] == "WebRpcBackend"
    assert measurements["chain"][-1] == "StudioDocumentWebHandlers"
    assert measurements["super_calls"] == 0
    assert measurements["ancestor_state_attributes"] == {}
    assert "_rpc_call" in measurements["abstract_seams"]


def test_registry_and_ledger_counts(measurements: dict[str, Any]) -> None:
    assert measurements["registry_handler_names"] == 82
    assert (
        measurements["leaf_handlers_by_code"] + len(measurements["composite_handlers_by_code"])
        == 82
    )
    assert (
        measurements["ledger_single_native"] + measurements["ledger_multi_native"]
        == measurements["registry_handler_names"]
    )
    assert measurements["cross_class_rpc_calls"] <= measurements["rpc_call_sites"]
    assert set(measurements["rpc_call_keyword_usage"]) == set(RPC_CALL_KEYWORDS)


def test_markdown_follows_plan_row_order(measurements: dict[str, Any]) -> None:
    table = format_markdown(measurements)
    rows = [line for line in table.splitlines() if line.startswith("| ")]
    assert rows[0].startswith("| Measure |")
    assert "`WebRpcBackend.__mro__` depth" in rows[1]
    assert rows[-1].startswith("| Per-file coverage floors on `_web/` |")
    assert len(rows) == 21


def test_main_json_flag_emits_dict(
    measurements: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(measure_script, "measure", lambda: measurements)
    assert main(["--json"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{")
    assert '"mro_depth": 11' in out
