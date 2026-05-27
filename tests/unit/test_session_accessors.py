"""ADR-014 Rule 3 Stage A: Session.collaborators + late-bound accessors (Wave 6).

Three typed accessors that let ``NotebookLMClient.__init__`` wire feature APIs
with the collaborators they actually depend on, instead of passing the whole
``Session``:

* ``collaborators`` — the ``SessionCollaborators`` bundle from
  ``build_collaborators``.
* ``session_transport`` — late-bound; constructed after the bundle via
  ``build_session_transport``.
* ``rpc_executor`` — late-bound; lazily constructed by ``_get_rpc_executor``.

Per Stage B (Wave 7 follow-up), all three accessors are deleted when
``build_collaborators`` ownership moves to ``NotebookLMClient``. The lint
guard in ``tests/_lint/test_client_composition.py`` (added in plan Wave 13)
restricts reads of these accessors to ``client.py`` + ``_session.py`` +
``tests/`` to prevent them from becoming a discoverability hub.
"""

from __future__ import annotations

import pytest

from notebooklm._rpc_executor import RpcExecutor
from notebooklm._session import Session
from notebooklm._session_init import SessionCollaborators
from notebooklm._session_transport import SessionTransport
from notebooklm.auth import AuthTokens


def _make_session() -> Session:
    return Session(
        AuthTokens(
            cookies={"SID": "sid"},
            csrf_token="csrf",
            session_id="sid",
        )
    )


def test_collaborators_accessor_returns_bundle() -> None:
    """``Session.collaborators`` exposes the ``SessionCollaborators`` dataclass."""
    session = _make_session()

    coll = session.collaborators

    assert isinstance(coll, SessionCollaborators)
    # All 8 SessionCollaborators fields validated per _session_init.py:92-109.
    # The plan uses these exact names (note: ``reqid``, NOT ``reqid_counter``).
    # Round-1 reviewer finding on PR #1069 (gemini + coderabbit): the prior
    # 6-of-8 coverage left ``cookie_persistence`` and ``poll_registry``
    # un-asserted — any future drift in those fields would slip through.
    assert coll.metrics is session._metrics_obj
    assert coll.drain_tracker is session._drain_tracker
    assert coll.reqid is session._reqid
    assert coll.auth_coord is session._auth_coord
    assert coll.kernel is session._kernel
    assert coll.lifecycle is session._lifecycle
    assert coll.cookie_persistence is session.cookie_persistence
    assert coll.poll_registry is session.poll_registry


def test_session_transport_accessor_returns_concrete_transport() -> None:
    """``Session.session_transport`` exposes the late-bound transport."""
    session = _make_session()

    assert isinstance(session.session_transport, SessionTransport)
    assert session.session_transport is session._transport


def test_rpc_executor_accessor_returns_lazy_executor_singleton() -> None:
    """``Session.rpc_executor`` returns the lazy executor and caches it."""
    session = _make_session()

    executor1 = session.rpc_executor
    executor2 = session.rpc_executor

    assert isinstance(executor1, RpcExecutor)
    assert executor1 is executor2, "rpc_executor must be stable across calls"


def test_collaborators_accessor_does_not_construct_new_bundle() -> None:
    """The accessor is read-only — the same bundle instance every call."""
    session = _make_session()

    assert session.collaborators is session.collaborators


def test_collaborators_field_name_is_reqid_not_reqid_counter() -> None:
    """Plan naming-note pin: the dataclass field is ``reqid``, not ``reqid_counter``.

    Catches any downstream code that wires features with ``coll.reqid_counter``
    (which would fail at runtime). Wave 7 / Task 4.1 feature wiring depends on
    this exact field name.
    """
    session = _make_session()

    assert hasattr(session.collaborators, "reqid"), (
        "SessionCollaborators must carry 'reqid' (not 'reqid_counter')"
    )
    assert not hasattr(session.collaborators, "reqid_counter"), (
        "Wave 7 feature wiring assumes the field is named 'reqid'; "
        "if 'reqid_counter' is added too, downstream feature constructors will "
        "silently pick the wrong attribute."
    )


@pytest.mark.parametrize(
    "accessor_name",
    ["collaborators", "session_transport", "rpc_executor"],
)
def test_accessor_is_read_only_property(accessor_name: str) -> None:
    """Each accessor is a property (not a settable attribute).

    Per the plan: the lint guard in tests/_lint/test_client_composition.py
    treats these as the *only* legitimate way Wave 7+ wires features. Making
    them properties enforces the read-only intent at the class level.
    """
    attr = getattr(Session, accessor_name)
    assert isinstance(attr, property), (
        f"Session.{accessor_name} must be a @property (got {type(attr).__name__})"
    )
