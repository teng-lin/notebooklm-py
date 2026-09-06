"""Public source-batch outcome parity across both backend adapters."""

from __future__ import annotations

from typing import get_args, get_type_hints

from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._sources import SourcesAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.outcomes import SourceBatchItemOutcome


def _return_item_type(owner: type[SourcesAPI]) -> object:
    annotation = get_type_hints(owner.add_urls_batch)["return"]
    container_args = get_args(annotation)
    assert len(container_args) == 1
    return container_args[0]


def test_both_backends_expose_the_public_source_batch_outcome() -> None:
    assert _return_item_type(SourcesAPI) is SourceBatchItemOutcome
    assert _return_item_type(WebSourcesAPI) is SourceBatchItemOutcome
    assert _return_item_type(AndroidSourcesAPI) is SourceBatchItemOutcome


def test_both_backends_override_the_public_capability() -> None:
    assert "add_urls_batch" in WebSourcesAPI.__dict__
    assert "add_urls_batch" in AndroidSourcesAPI.__dict__
