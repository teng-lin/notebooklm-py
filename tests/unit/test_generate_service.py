"""Named migration gate for the retired CLI generation service wrapper."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generation_command_constructs_typed_requests_without_raw_args_bridge() -> None:
    command_path = ROOT / "src/notebooklm/cli/generate_cmd.py"
    tree = ast.parse(command_path.read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {
        "AudioGenerationRequest",
        "SlideDeckGenerationRequest",
        "ReviseSlideGenerationRequest",
        "QuizGenerationRequest",
        "FlashcardsGenerationRequest",
        "InfographicGenerationRequest",
        "DataTableGenerationRequest",
        "MindMapGenerationRequest",
        "ReportGenerationRequest",
    } <= constructed
    # Video and cinematic-video deliberately share the neutral factory so both
    # variants pass through the same cross-field validation before cinematic
    # normalization. The other variants remain constructed directly.
    assert "build_generation_request" in constructed
    assert all(
        not (isinstance(node, ast.Name) and node.id == "raw_args") for node in ast.walk(tree)
    )


def test_legacy_generation_service_wrapper_is_retired() -> None:
    assert not (ROOT / "src/notebooklm/cli/services/generate.py").exists()
    assert not (ROOT / "src/notebooklm/_app/generate_plans.py").exists()
