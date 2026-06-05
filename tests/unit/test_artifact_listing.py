"""Direct shape-handling tests for ``ArtifactListingService.list_raw``.

The listing service collapses recognized payload shapes (a wrapped
``[[row, ...]]`` envelope, an already-flat row list, or an empty/None payload)
into a list of rows. A truthy *non-list* payload is schema drift, not an empty
notebook — ``list_raw`` raises ``DecodingError`` so callers can tell a miss from
drift instead of silently collapsing to ``[]`` (#1344).
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._artifact.listing import ArtifactListingService
from notebooklm.exceptions import DecodingError, RPCError


class _FakeRpc:
    """Minimal ``RpcCaller`` returning a fixed payload from ``rpc_call``."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
        return self._payload


async def _list_raw(payload: Any) -> list[Any]:
    return await ArtifactListingService().list_raw("nb_123", rpc=_FakeRpc(payload))


class TestListRawShapeHandling:
    """Recognized shapes yield rows; drift raises (#1344)."""

    @pytest.mark.asyncio
    async def test_wrapped_envelope_unwraps_to_inner_rows(self) -> None:
        rows = [["a", "Audio"], ["b", "Video"]]
        assert await _list_raw([rows]) == rows

    @pytest.mark.asyncio
    async def test_flat_row_list_returned_as_is(self) -> None:
        rows = [["a", "Audio"], ["b", "Video"]]
        assert await _list_raw(rows) == rows

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], False])
    async def test_falsy_payload_is_empty_list(self, payload: Any) -> None:
        # An empty / missing payload is a legitimately empty notebook, not drift.
        assert await _list_raw(payload) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["drift-string", {"oops": 1}, 7])
    async def test_truthy_non_list_payload_raises(self, payload: Any) -> None:
        with pytest.raises(DecodingError):
            await _list_raw(payload)


class TestListArtifactsMindMapSubFetch:
    """The secondary mind-map fetch fails loud on drift but degrades on outage (#1344)."""

    @pytest.mark.asyncio
    async def test_mind_map_drift_propagates(self) -> None:
        async def _empty_raw(_nb: str) -> list[Any]:
            return []

        async def _drifting_mind_maps(_nb: str) -> list[Any]:
            raise DecodingError("drift", method_id="cFji9")

        # Drift in the mind-map sub-fetch must not be masked as "no mind maps".
        with pytest.raises(DecodingError):
            await ArtifactListingService().list_artifacts(
                "nb_123", None, list_raw=_empty_raw, list_mind_maps=_drifting_mind_maps
            )

    @pytest.mark.asyncio
    async def test_transient_mind_map_outage_degrades_to_studio(self) -> None:
        async def _empty_raw(_nb: str) -> list[Any]:
            return []

        async def _unavailable_mind_maps(_nb: str) -> list[Any]:
            raise RPCError("mind-map endpoint temporarily unavailable")

        # A transient outage in the secondary fetch still degrades gracefully:
        # the studio artifacts (here none) are returned rather than raising.
        result = await ArtifactListingService().list_artifacts(
            "nb_123", None, list_raw=_empty_raw, list_mind_maps=_unavailable_mind_maps
        )
        assert result == []
