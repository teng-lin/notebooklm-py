"""Primitive leaf codec rows introduced by the P9.2 hoists.

A primitive is one native set-op the hoisted product workflows sequence above
the port: ``LABEL_MUTATE`` (one ``UPDATE_LABEL`` call, the variant chosen from
the request's kind and form), ``LABEL_ALLOCATE`` (one manual ``CREATE_LABEL``)
and ``SHARING_MUTATE`` (one ``SHARE_NOTEBOOK`` visibility or grant envelope).
Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec, NativeChoice
from ..._operations import Operation
from ..._records import (
    LABEL_ALLOCATE_DEF,
    LABEL_MUTATE_DEF,
    SHARING_MUTATE_DEF,
    LabelMutateInput,
)
from ...rpc import RPCMethod
from ..codec import labels as labels_codec
from ..codec import sharing as sharing_codec

_MUTATE_FIELD = NativeChoice(RPCMethod.UPDATE_LABEL)
_MUTATE_ADD_SOURCES = NativeChoice(RPCMethod.UPDATE_LABEL, "add_sources")
_MUTATE_REMOVE_SOURCES = NativeChoice(RPCMethod.UPDATE_LABEL, "remove_sources")
_MUTATE_ADD_NOTEBOOKS = NativeChoice(RPCMethod.UPDATE_LABEL, "add_notebooks")
_MUTATE_REMOVE_NOTEBOOKS = NativeChoice(RPCMethod.UPDATE_LABEL, "remove_notebooks")
_MUTATE_CHOICES: Mapping[str | None, NativeChoice[RPCMethod]] = MappingProxyType(
    {
        None: _MUTATE_FIELD,
        "add_sources": _MUTATE_ADD_SOURCES,
        "remove_sources": _MUTATE_REMOVE_SOURCES,
        "add_notebooks": _MUTATE_ADD_NOTEBOOKS,
        "remove_notebooks": _MUTATE_REMOVE_NOTEBOOKS,
    }
)


def _select_mutate(value: LabelMutateInput) -> NativeChoice[RPCMethod]:
    """Pick the one ``UPDATE_LABEL`` variant a mutate request dispatches under."""
    return _MUTATE_CHOICES[labels_codec.label_mutate_variant(value)]


LABEL_MUTATE = CodecBinding(
    definition=LABEL_MUTATE_DEF,
    encode=labels_codec.encode_label_mutate,
    decode=labels_codec.decode_label_mutate,
    native=NativeCallSpec.keyed(
        _select_mutate,
        _MUTATE_FIELD,
        _MUTATE_ADD_SOURCES,
        _MUTATE_REMOVE_SOURCES,
        _MUTATE_ADD_NOTEBOOKS,
        _MUTATE_REMOVE_NOTEBOOKS,
    ),
)

LABEL_ALLOCATE = CodecBinding(
    definition=LABEL_ALLOCATE_DEF,
    encode=labels_codec.encode_label_allocate,
    decode=labels_codec.decode_label_allocate,
    native=NativeCallSpec.constant(RPCMethod.CREATE_LABEL),
)

SHARING_MUTATE = CodecBinding(
    definition=SHARING_MUTATE_DEF,
    encode=sharing_codec.encode_sharing_mutate,
    decode=sharing_codec.decode_sharing_mutate,
    native=NativeCallSpec.constant(RPCMethod.SHARE_NOTEBOOK),
)

PRIMITIVE_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        LABEL_MUTATE.definition.key: LABEL_MUTATE,
        LABEL_ALLOCATE.definition.key: LABEL_ALLOCATE,
        SHARING_MUTATE.definition.key: SHARING_MUTATE,
    }
)

__all__ = ["LABEL_ALLOCATE", "LABEL_MUTATE", "PRIMITIVE_ROWS", "SHARING_MUTATE"]
