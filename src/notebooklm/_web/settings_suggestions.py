"""Web workflow binding for the input-defaulting prompt-suggestion composite.

The settings reads/mutation and report suggestions are ``_web.bindings.settings``
codec rows since P9.3; only ``NOTEBOOK_SUGGEST_PROMPTS`` — a ``GET_NOTEBOOK``-if-
``source_ids``-is-``None`` composite kept adapter-owned under P9.2 contract 1 —
remains a handler here.
"""

from __future__ import annotations

from .._deadline import RuntimeDeadline
from .._notebook_payloads import build_get_notebook_params
from .._operations import Operation
from .._records import (
    NotebookSuggestPromptsInput,
    NotebookSuggestPromptsResult,
)
from ..rpc import RPCMethod
from .codec import suggestions as suggestions_codec
from .labels import LabelSetWebHandlers


class SettingsSuggestionWebHandlers(LabelSetWebHandlers):
    """Prompt-suggestion composite handler mixed into the web backend."""

    async def _notebook_suggest_prompts(
        self,
        value: NotebookSuggestPromptsInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> NotebookSuggestPromptsResult:
        source_ids = value.source_ids
        if source_ids is None:
            notebook = await self._rpc_call(
                RPCMethod.GET_NOTEBOOK,
                build_get_notebook_params(value.notebook_id),
                operation=Operation.NOTEBOOK_SUGGEST_PROMPTS,
                deadline=deadline,
                source_path=f"/notebook/{value.notebook_id}",
            )
            source_ids = suggestions_codec.decode_prompt_source_ids(
                notebook,
                notebook_id=value.notebook_id,
            )
        result = await self._rpc_call(
            RPCMethod.SUGGEST_PROMPTS,
            suggestions_codec.encode_prompt_suggestions(
                value.notebook_id,
                source_ids,
                mode=value.mode,
                query=value.query,
            ),
            operation=Operation.NOTEBOOK_SUGGEST_PROMPTS,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        return suggestions_codec.decode_prompt_suggestions(result)


__all__ = ["SettingsSuggestionWebHandlers"]
