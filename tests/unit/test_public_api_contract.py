"""Static-shape conformance for the public return/error contract (ADR-0019).

This is the Tier-1 enforcement floor from ADR-0019: a parametrised
*static-shape* conformance check over the whole public client surface. It walks
``inspect.signature(...)`` return annotations (resolved through
``typing.get_type_hints`` so PEP 563 string annotations are honoured) across
every public namespace and asserts the return-shape rules the contract fixes:

* every namespace exposing ``get_or_none`` returns an ``Optional`` type;
* every ``delete`` returns ``None``;
* no public ``get_or_none`` is annotated non-``Optional``;
* every public ``get`` is either non-``Optional`` (the target end state) **or**
  carried in the reason-tagged :data:`GET_OPTIONAL_EXEMPTIONS` allowlist below.

Public ``get()`` methods must stay non-Optional after #1247;
``GET_OPTIONAL_EXEMPTIONS`` is an empty regression sentinel.

This walk is deliberately independent of ``scripts/audit_public_api_compat.py``.
The two former coverage holes in that comparator are now closed (issue #1378:
it audits the ``mind_maps`` namespace and flags ``changed-return`` breaks), but
the comparator answers a different question — "did the public surface break
*against the previous release*" — whereas this walk asserts the absolute
return-shape *rules* against today's surface with no backend or git baseline.
Namespaces are enumerated explicitly here (including ``mind_maps``) so the
divergence that originally occurred (``mind_maps.get() -> MindMap | None``, added
without the deprecation warning) is a signature smell this catches directly.
"""

from __future__ import annotations

import inspect
import sys
import types
import typing
from collections.abc import Callable

import pytest

# Every public client namespace, enumerated explicitly (ADR-0019 Tier-1 requires
# the walk cover the whole surface, including ``mind_maps``, even though the
# compat collector now covers it too, because this test asserts absolute
# return-shape rules rather than release-to-release diffs). Imported from the
# private implementation modules rather than constructing a live
# ``NotebookLMClient`` so the walk needs no auth, event loop, or network.
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat.api import ChatAPI
from notebooklm._labels import LabelsAPI
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._notebooks import NotebooksAPI
from notebooklm._notes import NotesAPI
from notebooklm._research import ResearchAPI
from notebooklm._settings import SettingsAPI
from notebooklm._sharing import SharingAPI
from notebooklm._sources import SourcesAPI

# Attribute name on ``NotebookLMClient`` -> the API class that backs it. Keyed by
# the public attribute so a failure names the surface a caller actually touches.
NAMESPACES: dict[str, type] = {
    "notebooks": NotebooksAPI,
    "sources": SourcesAPI,
    "artifacts": ArtifactsAPI,
    "notes": NotesAPI,
    "mind_maps": MindMapsAPI,
    "labels": LabelsAPI,
    "chat": ChatAPI,
    "research": ResearchAPI,
    "sharing": SharingAPI,
    "settings": SettingsAPI,
}

# The six namespaces that expose the resource-lookup surface (``get`` /
# ``get_or_none`` / ``delete``). ``chat``/``research``/``sharing``/``settings``
# are intentionally absent — they expose none of the three. Pinned so a rename or
# removal that makes a method silently undiscoverable fails loudly rather than
# shrinking the parametrisation to a still-green subset.
LOOKUP_NAMESPACES = frozenset({"notebooks", "sources", "artifacts", "notes", "mind_maps", "labels"})

# Empty as of #1247: every namespace ``get()`` now returns a non-Optional type
# and raises its ``*NotFoundError`` on a miss. The set can never gain an entry.
GET_OPTIONAL_EXEMPTIONS: dict[str, str] = {}


def _method(namespace: str, name: str) -> Callable[..., object]:
    """Return the unbound ``name`` method of ``namespace``'s backing API class."""
    return getattr(NAMESPACES[namespace], name)


def _resolve_return(fn: Callable[..., object]) -> object:
    """Resolve a callable's return annotation, honouring PEP 563 strings.

    ``inspect.signature(...).return_annotation`` yields a *string* under
    ``from __future__ import annotations`` (as several API modules use), so the
    annotation is resolved to a real type.

    Only the *return* annotation is resolved — not the whole signature. A bare
    ``typing.get_type_hints(fn)`` would evaluate every parameter annotation too,
    so a future ``get``/``delete`` parameter typed with a ``TYPE_CHECKING``-only
    import would raise ``NameError`` here even though this walk only cares about
    the return type.
    """
    raw = fn.__annotations__.get("return", inspect.Signature.empty)
    if not isinstance(raw, str):
        # Already a real object: either ``empty``, or a non-PEP-563 module where
        # ``-> None`` is the literal ``None`` singleton. Normalise ``None`` to
        # ``type(None)`` to match ``typing.get_type_hints`` (so ``_is_none``'s
        # identity check holds regardless of the defining module's PEP 563 use).
        return type(None) if raw is None else raw

    # Resolve the string against the defining module's namespace, evaluating
    # only this one annotation (a stand-in callable carrying just ``return``).
    stub: Callable[..., object] = lambda: None  # noqa: E731 - throwaway resolver
    stub.__annotations__ = {"return": raw}
    globalns = getattr(sys.modules.get(fn.__module__, None), "__dict__", {})
    return typing.get_type_hints(stub, globalns=globalns)["return"]


def _require_return(namespace: str, method: str) -> object:
    """Resolve a method's return annotation, failing loud if it is un-annotated.

    A public lookup method with no return annotation cannot be contract-checked
    (and a bare ``empty`` would otherwise read as "not Optional" / "not None"),
    so this raises an actionable assertion rather than silently passing.
    """
    annotation = _resolve_return(_method(namespace, method))
    assert annotation is not inspect.Signature.empty, (
        f"{namespace}.{method} has no return annotation; a public lookup must be "
        "explicitly typed so it can be contract-checked."
    )
    return annotation


def _is_optional(annotation: object) -> bool:
    """Return ``True`` when ``annotation`` is ``Optional[...]`` (a union with ``None``)."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return False


def _is_none(annotation: object) -> bool:
    """Return ``True`` when ``annotation`` denotes ``None`` (``NoneType``)."""
    # ``_resolve_return`` normalises a ``-> None`` annotation to ``type(None)``
    # (the ``None`` *singleton* that a non-PEP-563 module yields is mapped over),
    # so this identity check is correct for annotations from that helper.
    return annotation is type(None)


def _has_varargs(fn: Callable[..., object]) -> bool:
    """Return ``True`` when ``fn`` declares ``*args`` / ``*ids`` positional varargs."""
    return any(
        param.kind is inspect.Parameter.VAR_POSITIONAL
        for param in inspect.signature(fn).parameters.values()
    )


# Namespaces that actually expose each method, computed once. These power the
# parametrisations; ``test_lookup_surface_is_pinned`` asserts each set equals
# ``LOOKUP_NAMESPACES`` so a silently-shrunk set can never pass unnoticed.
_GET_OR_NONE_NAMESPACES = sorted(n for n, c in NAMESPACES.items() if hasattr(c, "get_or_none"))
_GET_NAMESPACES = sorted(n for n, c in NAMESPACES.items() if hasattr(c, "get"))
_DELETE_NAMESPACES = sorted(n for n, c in NAMESPACES.items() if hasattr(c, "delete"))


@pytest.mark.parametrize(
    ("method_name", "discovered"),
    [
        ("get", _GET_NAMESPACES),
        ("get_or_none", _GET_OR_NONE_NAMESPACES),
        ("delete", _DELETE_NAMESPACES),
    ],
)
def test_lookup_surface_is_pinned(method_name: str, discovered: list[str]) -> None:
    """The exact set of namespaces exposing each lookup method is pinned.

    Guards against a namespace silently dropping out of the walk (e.g. a rename
    that makes ``hasattr(cls, method)`` go quietly false), which would otherwise
    shrink the parametrisation to a still-green subset.
    """
    # ADR-0019: every lookup namespace exposes all three of get/get_or_none/
    # delete, so the three discovered sets must each equal the same constant.
    assert set(discovered) == LOOKUP_NAMESPACES, (
        f"namespaces exposing {method_name!r} = {sorted(discovered)}, "
        f"expected {sorted(LOOKUP_NAMESPACES)}"
    )


@pytest.mark.parametrize("namespace", _GET_OR_NONE_NAMESPACES)
def test_get_or_none_returns_optional(namespace: str) -> None:
    """Every namespace exposing ``get_or_none`` annotates it ``Optional`` (ADR-0019)."""
    annotation = _require_return(namespace, "get_or_none")
    assert _is_optional(annotation), (
        f"{namespace}.get_or_none must return Optional[...]; got {annotation!r}"
    )


@pytest.mark.parametrize("namespace", _DELETE_NAMESPACES)
def test_delete_returns_none(namespace: str) -> None:
    """Every public ``delete`` is an idempotent no-payload command -> ``None`` (ADR-0019)."""
    annotation = _require_return(namespace, "delete")
    assert _is_none(annotation), f"{namespace}.delete must return None; got {annotation!r}"


@pytest.mark.parametrize("namespace", _GET_NAMESPACES)
def test_get_is_non_optional_or_exempt(namespace: str) -> None:
    """Public ``get`` stays non-``Optional`` after #1247."""
    annotation = _require_return(namespace, "get")
    if _is_optional(annotation):
        assert namespace in GET_OPTIONAL_EXEMPTIONS, (
            f"{namespace}.get is Optional but not in GET_OPTIONAL_EXEMPTIONS; "
            "add a reason-tagged exemption or make it non-Optional (#1247)."
        )
    else:
        assert namespace not in GET_OPTIONAL_EXEMPTIONS, (
            f"{namespace}.get is now non-Optional; remove its stale "
            "GET_OPTIONAL_EXEMPTIONS entry (#1247 progress)."
        )


@pytest.mark.parametrize("namespace", sorted(GET_OPTIONAL_EXEMPTIONS))
def test_get_optional_exemptions_are_live(namespace: str) -> None:
    """Every exemption names a real Optional ``get`` (no stale allowlist entries)."""
    cls = NAMESPACES.get(namespace)
    assert cls is not None, f"unknown namespace in exemptions: {namespace}"
    assert hasattr(cls, "get"), f"{namespace} exemption names a namespace without get()"
    assert GET_OPTIONAL_EXEMPTIONS[namespace].strip(), (
        f"{namespace} exemption must carry a non-empty reason"
    )
    annotation = _resolve_return(_method(namespace, "get"))
    assert _is_optional(annotation), (
        f"GET_OPTIONAL_EXEMPTIONS lists {namespace}.get as Optional, but it is "
        f"now {annotation!r}; remove the stale exemption (#1247)."
    )


def test_get_optional_exemptions_is_empty() -> None:
    """#1247 has landed: every namespace get() is non-Optional, so the
    exemption set must stay empty — it can shrink to empty but never regain a
    member (re-adding one re-introduces an Optional get())."""
    assert GET_OPTIONAL_EXEMPTIONS == {}, (
        "GET_OPTIONAL_EXEMPTIONS must stay empty after #1247: every namespace "
        f"get() raises *NotFoundError and is non-Optional. Found: {GET_OPTIONAL_EXEMPTIONS}"
    )


def test_mind_maps_delete_exposes_kind_parameter() -> None:
    """``mind_maps.delete`` keeps its kind-dispatch parameter (ADR-0019 Tier-2).

    ``mind_maps.delete(..., kind=...)`` is irreducibly per-namespace (it is
    kind-dispatched), so the generic ``ResourceAPI[T]`` base was rejected and
    ``delete`` stays per-namespace. This pins the ``kind`` parameter and its
    optional-keyword shape (``kind=None`` enables the omitted-kind auto-detect
    path) so a future consolidation cannot erase or harden it.
    """
    params = inspect.signature(MindMapsAPI.delete).parameters
    assert "kind" in params, "mind_maps.delete must keep its `kind` dispatch parameter"
    kind = params["kind"]
    assert kind.kind is inspect.Parameter.KEYWORD_ONLY, "`kind` must stay keyword-only"
    assert kind.default is None, "`kind` must default to None (omitted-kind auto-detect)"


@pytest.mark.parametrize("namespace", sorted(NAMESPACES))
@pytest.mark.parametrize("method_name", ["get", "get_or_none", "delete"])
def test_lookup_methods_have_no_varargs(method_name: str, namespace: str) -> None:
    """No public lookup/delete method uses a ``*args``/``*ids`` varargs signature.

    A ``*ids`` base would erase the per-namespace public signatures (the
    namespaces differ in arity); ADR-0019 Tier-2 rejected that base for exactly
    this reason. This asserts the explicit, typed signatures are preserved.
    """
    fn = getattr(NAMESPACES[namespace], method_name, None)
    if fn is None:
        pytest.skip(f"{namespace} does not expose {method_name}")
    assert not _has_varargs(fn), (
        f"{namespace}.{method_name} must not use *args/*ids varargs; "
        "keep an explicit, per-namespace typed signature (ADR-0019 Tier-2)."
    )
