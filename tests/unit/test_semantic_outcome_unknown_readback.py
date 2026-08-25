"""Deadline uncertainty after a semantic mutation has reached the server.

The ``label.update``, ``collection.update``, and ``source.update`` cases moved to
``tests/unit/test_label_update_workflow.py`` and
the matching P9.2 workflow tests: the semantic services sequence those
workflows now.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._backend import BackendDeadlineExceededError, BackendErrorReason
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import OperationDef
from notebooklm._records import (
    ARTIFACT_RENAME_DEF,
    COLLECTION_CREATE_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    ArtifactRenameInput,
    LabelCreateInput,
    LabelKind,
    SharePermissionLevel,
    ShareViewScope,
    SharingSetPublicInput,
    SharingSetViewLevelInput,
    SharingUpdateUsersInput,
    SharingUserGrant,
)
from notebooklm.exceptions import RPCTimeoutError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend


class _ExpireAfterCallExecutor:
    """Expire the shared clock immediately after one successful native call."""

    def __init__(
        self,
        responses: tuple[object, ...],
        *,
        clock: list[float],
        expire_after: int,
    ) -> None:
        self._responses = list(responses)
        self._clock = clock
        self._expire_after = expire_after
        self.calls: list[RPCMethod] = []

    async def rpc_call(self, method: RPCMethod, _params: list[Any], **_kwargs: Any) -> Any:
        self.calls.append(method)
        response = self._responses.pop(0)
        if len(self.calls) == self._expire_after:
            self._clock[0] = 2.0
        return response


_LABEL_ROW = ["Existing", None, "label-1", ""]
_COLLECTION_ROW = ["Existing", None, "collection-1", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "definition",
        "value",
        "responses",
        "expire_after",
        "dispatched_methods",
        "blocked_method",
    ),
    [
        pytest.param(
            SHARING_SET_PUBLIC_DEF,
            SharingSetPublicInput("nb-1", True),
            (None,),
            1,
            (RPCMethod.SHARE_NOTEBOOK,),
            RPCMethod.GET_SHARE_STATUS,
            id="sharing-visibility-readback",
        ),
        pytest.param(
            SHARING_SET_VIEW_LEVEL_DEF,
            SharingSetViewLevelInput("nb-1", ShareViewScope.CHAT_ONLY),
            (None,),
            1,
            (RPCMethod.RENAME_NOTEBOOK,),
            RPCMethod.GET_SHARE_STATUS,
            id="sharing-view-readback",
        ),
        pytest.param(
            SHARING_UPDATE_USERS_DEF,
            SharingUpdateUsersInput(
                "nb-1",
                (SharingUserGrant("reader@example.com", SharePermissionLevel.VIEWER),),
            ),
            (None,),
            1,
            (RPCMethod.SHARE_NOTEBOOK,),
            RPCMethod.GET_SHARE_STATUS,
            id="sharing-users-readback",
        ),
        pytest.param(
            ARTIFACT_RENAME_DEF,
            ArtifactRenameInput("nb-1", "artifact-1", "Renamed"),
            (None,),
            1,
            (RPCMethod.RENAME_ARTIFACT,),
            RPCMethod.LIST_ARTIFACTS,
            id="artifact-rename-readback",
        ),
        pytest.param(
            COLLECTION_CREATE_DEF,
            LabelCreateInput(LabelKind.COLLECTION, "New"),
            ([None, []], None),
            2,
            (RPCMethod.LIST_LABELS, RPCMethod.CREATE_LABEL),
            RPCMethod.LIST_LABELS,
            id="collection-create-readback",
        ),
    ],
)
async def test_expiry_after_a_write_is_truthfully_unconfirmed(
    definition: OperationDef[Any, Any],
    value: object,
    responses: tuple[object, ...],
    expire_after: int,
    dispatched_methods: tuple[RPCMethod, ...],
    blocked_method: RPCMethod,
) -> None:
    clock = [0.0]
    executor = _ExpireAfterCallExecutor(
        responses,
        clock=clock,
        expire_after=expire_after,
    )
    deadline = RuntimeDeadline(timeout=1.0, started_at=0.0, monotonic=lambda: clock[0])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await build_web_backend(executor).invoke(definition, value, deadline=deadline)

    error = caught.value
    assert executor.calls == list(dispatched_methods)
    assert error.operation is definition.key
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is True
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == blocked_method.value
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert projected.unconfirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "baseline", "blocked_method"),
    [
        pytest.param(
            COLLECTION_CREATE_DEF,
            LabelCreateInput(LabelKind.COLLECTION, "New"),
            [None, []],
            RPCMethod.CREATE_LABEL,
            id="collection-baseline",
        ),
    ],
)
async def test_expiry_after_a_read_only_preflight_remains_confirmed_not_dispatched(
    definition: OperationDef[Any, Any],
    value: object,
    baseline: object,
    blocked_method: RPCMethod,
) -> None:
    clock = [0.0]
    executor = _ExpireAfterCallExecutor((baseline,), clock=clock, expire_after=1)
    deadline = RuntimeDeadline(timeout=1.0, started_at=0.0, monotonic=lambda: clock[0])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await build_web_backend(executor).invoke(definition, value, deadline=deadline)

    error = caught.value
    assert executor.calls == [RPCMethod.LIST_LABELS]
    assert error.operation is definition.key
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == blocked_method.value
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is False
