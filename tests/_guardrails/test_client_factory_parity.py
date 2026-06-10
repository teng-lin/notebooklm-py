"""Gate: the canonical test factory and the production constructor never diverge.

Rule: a client built by ``tests/_helpers/client_factory.build_client_shell_for_tests``
must carry exactly the same instance-attribute surface (names AND attribute
types) as one built by ``NotebookLMClient(...)``.

Why it matters: the factory used to hand-wire private attributes against
``NotebookLMClient.__new__``. That duplicated wiring drifted twice —
issue #1196 (the open-time upload-semaphore loop reset needed
``_source_uploader``) and issue #1225 (the open-time ChatAPI
conversation-lock reset needed ``chat``) — each time silently stranding
the shell that the whole unit tier builds on. Both paths now run one
shared seam, :func:`notebooklm._client_assembly._assemble_client`, so the
remaining drift vector is wiring added OUTSIDE the seam (e.g. a new
``self.foo = ...`` in ``NotebookLMClient.__init__`` after the delegation
call, or a factory-only attribute). This gate catches exactly that.

How to fix a failure: move the new attribute assignment into
``_assemble_client`` (with a parameter default that preserves production
behavior) instead of setting it in ``__init__`` or in the factory — see
the module docstring of ``notebooklm._client_assembly``.

Per docs/development.md gate conventions the comparison is a pure
function (:func:`_attribute_surface_divergence`) self-tested below
against known-divergent inputs so the gate cannot silently become
vacuous.
"""

from __future__ import annotations

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests


def _attribute_surface_divergence(
    production: dict[str, type],
    factory: dict[str, type],
) -> list[str]:
    """Pure detector: differences between two ``{attr_name: type}`` surfaces.

    Returns a list of human-readable divergence descriptions (empty =
    parity). Checks both directions plus per-attribute type equality, so
    a factory that wires a stand-in object where production wires the
    real collaborator is also caught.
    """
    problems: list[str] = []
    for name in sorted(production.keys() - factory.keys()):
        problems.append(
            f"attribute {name!r} is set by NotebookLMClient.__init__ but missing "
            "on a factory-built shell — move its assignment into "
            "notebooklm._client_assembly._assemble_client (incidents #1196/#1225)"
        )
    for name in sorted(factory.keys() - production.keys()):
        problems.append(
            f"attribute {name!r} is set on a factory-built shell but not by "
            "NotebookLMClient.__init__ — the factory must not wire extras "
            "outside _assemble_client"
        )
    for name in sorted(production.keys() & factory.keys()):
        if production[name] is not factory[name]:
            problems.append(
                f"attribute {name!r} type diverges: production wires "
                f"{production[name].__name__}, factory wires {factory[name].__name__}"
            )
    return problems


def _attribute_surface(client: NotebookLMClient) -> dict[str, type]:
    return {name: type(value) for name, value in vars(client).items()}


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test-sid"},
        csrf_token="test-csrf",
        session_id="test-session",
    )


def test_factory_shell_matches_production_constructor_surface() -> None:
    """The real gate: build through both paths and compare surfaces.

    Both clients are constructed but never opened — construction is pure
    object wiring (no I/O, no event-loop binding; the loop binds at
    ``open()`` time), so this is safe in the no-network unit tier.
    """
    production = NotebookLMClient(_make_auth())
    shell = build_client_shell_for_tests(auth=_make_auth())

    problems = _attribute_surface_divergence(
        _attribute_surface(production),
        _attribute_surface(shell),
    )
    assert problems == [], (
        "build_client_shell_for_tests diverged from NotebookLMClient.__init__ "
        "(the #1196/#1225 drift class):\n  " + "\n  ".join(problems)
    )


def test_shared_wiring_identities_hold_on_both_paths() -> None:
    """Identity pins the surface comparison cannot see.

    The name+type comparison above would miss *same-type rewiring* — e.g.
    a path that builds its ``ChatAPI`` against a privately constructed
    ``NotebooksAPI`` instead of the client's own (the #1225 drift was an
    open-time dependency on exactly this kind of shared wiring). Pin the
    load-bearing identities on BOTH construction paths:

    - ``chat`` resolves source ids through the client's own ``notebooks``;
    - every collaborator consumer shares the one RPC executor;
    - the uploader aliases the client-owned ``AuthTokens`` (ADR-0016's
      Auth Instance Invariant).
    """
    _missing = object()
    for label, client in (
        ("NotebookLMClient(...)", NotebookLMClient(_make_auth())),
        ("build_client_shell_for_tests(...)", build_client_shell_for_tests(auth=_make_auth())),
    ):
        # ``getattr`` with a sentinel so a renamed private storage
        # attribute fails THIS assertion with the contract message
        # instead of an unexplained AttributeError.
        assert getattr(client.chat, "_notebooks", _missing) is client.notebooks, (
            f"{label}: chat must share the client's NotebooksAPI instance "
            "(ChatAPI._notebooks), not a privately constructed one"
        )
        assert getattr(client.notebooks, "_rpc", _missing) is client._rpc_executor, (
            f"{label}: notebooks (NotebooksAPI._rpc) must dispatch through the "
            "client's shared RpcExecutor"
        )
        assert getattr(client._source_uploader, "_auth", _missing) is client._auth, (
            f"{label}: the upload pipeline (SourceUploadPipeline._auth) must alias "
            "the client-owned AuthTokens (ADR-0016 Auth Instance Invariant)"
        )
        assert client.auth is client._auth, (
            f"{label}: the public auth property must alias the client-owned AuthTokens"
        )


# --- detector self-tests (non-vacuity, per docs/development.md) ------------


def test_detector_flags_attribute_missing_on_factory_shell() -> None:
    problems = _attribute_surface_divergence({"chat": object, "x": int}, {"chat": object})
    assert len(problems) == 1
    assert "'x'" in problems[0]
    assert "missing" in problems[0]


def test_detector_flags_factory_only_attribute() -> None:
    problems = _attribute_surface_divergence({"chat": object}, {"chat": object, "y": int})
    assert len(problems) == 1
    assert "'y'" in problems[0]
    assert "not by" in problems[0]


def test_detector_flags_type_divergence() -> None:
    problems = _attribute_surface_divergence({"chat": int}, {"chat": str})
    assert len(problems) == 1
    assert "type diverges" in problems[0]


def test_detector_accepts_parity() -> None:
    surface = {"chat": object, "_auth": dict}
    assert _attribute_surface_divergence(surface, dict(surface)) == []
