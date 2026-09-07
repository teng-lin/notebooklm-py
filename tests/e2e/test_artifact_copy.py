"""Assert the managed reference copy's artifact contract as an E2E check."""

import json
from pathlib import Path

import pytest

from ._artifact_helpers import assert_copied_reference_artifacts
from .conftest import _managed_bindings, requires_auth


@requires_auth
@pytest.mark.asyncio
@pytest.mark.readonly
@pytest.mark.timeout(720)
async def test_copied_reference_artifacts_become_ready(client, read_only_notebook_id):
    if _managed_bindings() is None:
        pytest.skip("Copied artifact contract applies only to managed CI notebooks")
    contract = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "e2e_template_contract.json").read_text(
            encoding="utf-8"
        )
    )["artifacts"]
    await assert_copied_reference_artifacts(
        client,
        read_only_notebook_id,
        required_families=set(contract["required_completed_families"]),
        require_interactive_mind_map=contract["require_interactive_mind_map"],
    )
