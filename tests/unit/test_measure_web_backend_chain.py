"""P9 exit-boundary pins for ``scripts/measure_web_backend_chain.py``.

The script re-measures the P9 entry record and terminal exit shape in
``docs/plan/2026-08-13-semantic-backend-refactor.md``. These tests pin only the
structural relationships that must continue to hold after the decomposition;
they are not a second copy of the table.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.registry import WEB_SUPPORTED_OPERATIONS

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


def test_chain_shape_matches_terminal_exit(measurements: dict[str, Any]) -> None:
    # P9.4c terminal shape: the chain and its abstract seams are gone.
    assert measurements["mro_depth"] == len(WebRpcBackend.__mro__) - 1
    assert measurements["chain"][0] == "WebRpcBackend"
    assert measurements["chain"][-1] == WebRpcBackend.__mro__[-2].__name__
    assert measurements["super_calls"] == 0
    assert measurements["ancestor_state_attributes"] == {}
    assert measurements["abstract_seams"] == []


def test_registry_and_ledger_counts(measurements: dict[str, Any]) -> None:
    handler_names = measurements["registry_handler_names"]
    binding_rows = len(measurements["registry_binding_rows"])
    # Every executable disposition is a binding row after P9.4c.
    assert handler_names == 0
    assert handler_names + binding_rows == len(WEB_SUPPORTED_OPERATIONS)
    assert binding_rows >= 4
    assert (
        measurements["leaf_handlers_by_code"] + len(measurements["composite_handlers_by_code"])
        == handler_names
    )
    assert (
        measurements["ledger_single_native"] + measurements["ledger_multi_native"]
        == handler_names + binding_rows
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
    assert f'"mro_depth": {len(WebRpcBackend.__mro__) - 1}' in out
