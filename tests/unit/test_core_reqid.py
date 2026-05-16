"""Unit tests for ``ClientCore.next_reqid`` and the deprecation guard on
direct mutation of ``_reqid_counter``.

Covers:
- ``next_reqid()`` returns monotonic, post-increment values.
- Custom ``step`` parameter works.
- ``DeprecationWarning`` is emitted on ``core._reqid_counter = ...`` and on
  ``core._reqid_counter += ...``.
- ``next_reqid()`` itself does NOT emit a ``DeprecationWarning``.
"""

import warnings

import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens


def _make_core() -> ClientCore:
    auth = AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )
    return ClientCore(auth=auth)


@pytest.mark.asyncio
async def test_next_reqid_returns_post_increment_values() -> None:
    """Three successive calls bump by the default step and return new values."""
    core = _make_core()
    assert core._reqid_counter == 100000  # baseline

    first = await core.next_reqid()
    second = await core.next_reqid()
    third = await core.next_reqid()

    assert first == 200000
    assert second == 300000
    assert third == 400000
    # And the property reflects the final state.
    assert core._reqid_counter == 400000


@pytest.mark.asyncio
async def test_next_reqid_custom_step() -> None:
    """A non-default ``step`` parameter is honoured."""
    core = _make_core()
    assert await core.next_reqid(step=1) == 100001
    assert await core.next_reqid(step=7) == 100008
    assert await core.next_reqid(step=1000) == 101008


@pytest.mark.asyncio
async def test_next_reqid_does_not_warn() -> None:
    """The intended API surface must be silent — no ``DeprecationWarning``."""
    core = _make_core()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await core.next_reqid()
        await core.next_reqid()
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations == [], (
        "next_reqid() must not emit DeprecationWarning; "
        f"got {[(w.category.__name__, str(w.message)) for w in deprecations]}"
    )


def test_direct_assignment_warns() -> None:
    """``core._reqid_counter = N`` must emit a ``DeprecationWarning``."""
    core = _make_core()
    with pytest.warns(DeprecationWarning, match="next_reqid"):
        core._reqid_counter = 0
    # Setter still applies the value (backwards compatible).
    assert core._reqid_counter == 0


def test_read_modify_write_warns() -> None:
    """``core._reqid_counter += step`` must warn — this is the existing
    ``_chat.py`` pattern targeted for migration.
    """
    core = _make_core()
    with pytest.warns(DeprecationWarning, match="next_reqid"):
        core._reqid_counter += 100000
    assert core._reqid_counter == 200000


@pytest.mark.asyncio
async def test_next_reqid_rejects_zero_step() -> None:
    """``step=0`` would break uniqueness (two callers see the same value)."""
    core = _make_core()
    with pytest.raises(ValueError, match="step must be positive"):
        await core.next_reqid(step=0)
    # Counter must not have moved.
    assert core._reqid_counter == 100000


@pytest.mark.asyncio
async def test_next_reqid_rejects_negative_step() -> None:
    """``step<0`` would break monotonicity (counter moves backwards)."""
    core = _make_core()
    with pytest.raises(ValueError, match="step must be positive"):
        await core.next_reqid(step=-1)
    assert core._reqid_counter == 100000


@pytest.mark.asyncio
async def test_next_reqid_rejects_non_int_step() -> None:
    """Non-``int`` ``step`` (e.g. ``str``) must raise ``TypeError`` early."""
    core = _make_core()
    with pytest.raises(TypeError, match="step must be int"):
        await core.next_reqid(step="100")  # type: ignore[arg-type]
    assert core._reqid_counter == 100000


@pytest.mark.asyncio
async def test_next_reqid_rejects_bool_step() -> None:
    """``bool`` is a subclass of ``int`` in Python; the guard must still
    reject ``step=True`` to prevent a silent degradation to ``step=1``.
    """
    core = _make_core()
    with pytest.raises(TypeError, match="step must be int"):
        await core.next_reqid(step=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="step must be int"):
        await core.next_reqid(step=False)  # type: ignore[arg-type]
    assert core._reqid_counter == 100000
