"""Settings and suggestion codec rows (P9.3 settings/suggestions domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NOTEBOOK_SUGGEST_PROMPTS`` is the input-defaulting member (gate table §3.17):
since P9.4b a *deferred-product* :class:`CustomBinding` row whose handler
resolves ``source_ids is None`` through its ``GET_NOTEBOOK`` spec and then
issues the ``SUGGEST_PROMPTS`` read through the row-scoped invoker.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, CustomBinding, NativeCallSpec, RowInvoker
from ..._deadline import RuntimeDeadline
from ..._operations import Operation
from ..._records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    NotebookSuggestPromptsInput,
    NotebookSuggestPromptsResult,
)
from ...rpc import RPCMethod
from ..codec import settings as settings_codec
from ..codec import suggestions as suggestions_codec
from ..codec.source_ids import (
    SourceIdDiagnostics,
    decode_notebook_source_ids,
    encode_notebook_source_read,
)

SETTINGS_GET = CodecBinding(
    definition=SETTINGS_GET_DEF,
    encode=settings_codec.encode_settings_get,
    decode=settings_codec.decode_settings_get,
    native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS),
)

SETTINGS_GET_LIMITS = CodecBinding(
    definition=SETTINGS_GET_LIMITS_DEF,
    encode=settings_codec.encode_settings_get_limits,
    decode=settings_codec.decode_settings_get_limits,
    native=NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS),
)

SETTINGS_SET_LANGUAGE = CodecBinding(
    definition=SETTINGS_SET_LANGUAGE_DEF,
    encode=settings_codec.encode_settings_set_language,
    decode=settings_codec.decode_settings_set_language,
    native=NativeCallSpec.constant(RPCMethod.SET_USER_SETTINGS),
)

ARTIFACT_SUGGEST_REPORTS = CodecBinding(
    definition=ARTIFACT_SUGGEST_REPORTS_DEF,
    encode=suggestions_codec.encode_artifact_suggest_reports,
    decode=suggestions_codec.decode_artifact_suggest_reports,
    native=NativeCallSpec.constant(RPCMethod.GET_SUGGESTED_REPORTS),
)


_SOURCES = "sources"
_SUGGEST = "suggest"


async def _suggest_prompts(
    value: NotebookSuggestPromptsInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> NotebookSuggestPromptsResult:
    source_ids = value.source_ids
    if source_ids is None:
        notebook = await invoke.call(
            _SOURCES, encode_notebook_source_read(value.notebook_id), deadline=deadline
        )
        source_ids = decode_notebook_source_ids(
            notebook, notebook_id=value.notebook_id, diagnostics=SourceIdDiagnostics.GUARDED
        )
    raw = await invoke.call(
        _SUGGEST,
        suggestions_codec.encode_notebook_suggest_prompts(value, source_ids=source_ids),
        deadline=deadline,
    )
    return suggestions_codec.decode_prompt_suggestions(raw)


NOTEBOOK_SUGGEST_PROMPTS = CustomBinding(
    definition=NOTEBOOK_SUGGEST_PROMPTS_DEF,
    handler=_suggest_prompts,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.SUGGEST_PROMPTS, key=_SUGGEST),
    ),
    justification=(
        "Input-defaulting member kept adapter-owned under P9.2 contract 1; hoisting needs a "
        "resolved-input primitive per family (gate table §3.17)."
    ),
    category="deferred-product",
)

SETTINGS_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        SETTINGS_GET.definition.key: SETTINGS_GET,
        SETTINGS_GET_LIMITS.definition.key: SETTINGS_GET_LIMITS,
        SETTINGS_SET_LANGUAGE.definition.key: SETTINGS_SET_LANGUAGE,
        ARTIFACT_SUGGEST_REPORTS.definition.key: ARTIFACT_SUGGEST_REPORTS,
        NOTEBOOK_SUGGEST_PROMPTS.definition.key: NOTEBOOK_SUGGEST_PROMPTS,
    }
)

__all__ = [
    "ARTIFACT_SUGGEST_REPORTS",
    "NOTEBOOK_SUGGEST_PROMPTS",
    "SETTINGS_GET",
    "SETTINGS_GET_LIMITS",
    "SETTINGS_ROWS",
    "SETTINGS_SET_LANGUAGE",
]
