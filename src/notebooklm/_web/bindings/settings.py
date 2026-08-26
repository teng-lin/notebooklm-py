"""Settings and suggestion codec rows (P9.3 settings/suggestions domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NOTEBOOK_SUGGEST_PROMPTS`` was the last *deferred-product* row: until P10
R5.1c it resolved ``source_ids is None`` through its own ``GET_NOTEBOOK`` spec.
That read now belongs to ``SuggestionService`` above the port (ADR-0035 P10
addendum D1(a): source-scope defaulting is a service concern), leaving one
native and an ordinary codec row.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec
from ..._operations import Operation
from ..._records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
)
from ...rpc import RPCMethod
from ..codec import settings as settings_codec
from ..codec import suggestions as suggestions_codec

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


NOTEBOOK_SUGGEST_PROMPTS = CodecBinding(
    definition=NOTEBOOK_SUGGEST_PROMPTS_DEF,
    encode=suggestions_codec.encode_notebook_suggest_prompts,
    decode=suggestions_codec.decode_notebook_suggest_prompts,
    native=NativeCallSpec.constant(RPCMethod.SUGGEST_PROMPTS),
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
