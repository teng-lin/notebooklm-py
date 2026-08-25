"""P9.2 primitive leaf rows dispatch exactly the composites' natives.

``LABEL_MUTATE`` (input-keyed over the five ``UPDATE_LABEL`` variants),
``LABEL_ALLOCATE`` (one manual ``CREATE_LABEL``) and ``SHARING_MUTATE`` (one
``SHARE_NOTEBOOK`` envelope) are the three foundational rows; P9.2-4 adds
``SOURCE_PATCH_TITLE`` (one ``UPDATE_SOURCE`` title set-op) and P9.2-7 adds
``SHARING_PATCH_VIEW_LEVEL`` (one viewer-scope ``RENAME_NOTEBOOK`` mask), and P10
R3.2 adds ``SOURCE_REGISTER`` (input-keyed over the three ``ADD_SOURCE``
registration variants). All are
``encode → one native call → decode`` rows in ``_web/bindings/primitives.py``.
These tests pin the oracles the hoists rely on: each variant's payload and
keyword set equals what the old composite handlers sent (route, ``allow_null``,
explicit
``False``/``None`` values, ``operation_variant``), contract errors fire before
any wire call, the declared natives match the policy ledger, and failure
projection is what ``invoke()`` produces for every row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm._operations import Operation
from notebooklm._records import (
    LABEL_ALLOCATE_DEF,
    LABEL_MUTATE_DEF,
    SHARING_MUTATE_DEF,
    SOURCE_REGISTER_DEF,
    LabelAllocateInput,
    LabelAllocateResult,
    LabelKind,
    LabelMutateInput,
    LabelMutateResult,
    SharePermissionLevel,
    SharingGrants,
    SharingMutateInput,
    SharingMutateResult,
    SharingUserGrant,
    SharingVisibility,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceRegisterResult,
)
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import primitives as primitive_rows
from notebooklm._web.codec import labels as labels_codec
from notebooklm._web.codec import sharing as sharing_codec
from notebooklm._web.codec import sources as sources_codec
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES, SemanticDeadlineAuthority
from notebooklm._web.policy import (
    SERVICE_OWNED_WORKFLOW_BINDINGS,
    WEB_CALL_POLICY_BINDINGS,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SUPPORTED_OPERATIONS
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
_COLLECTION_OPTS = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1, 3]],
]
_COLLECTION_CREATE_OPTS = labels_codec.build_create_collection_params("x")[0]


def _source_entry(source_id: str, *, title: str, url: str | None = None) -> list[Any]:
    metadata = [None, 11, [1704067200, 0], None, 5, None, None, [url] if url else None]
    return [[source_id], title, metadata, [None, 2]]


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


_BASE_KWARGS = {
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


# --- registry partition ---------------------------------------------------------


def test_primitive_rows_are_supported_direct_rows_with_ledger_parity() -> None:
    converted = {
        Operation.LABEL_MUTATE: primitive_rows.LABEL_MUTATE,
        Operation.LABEL_ALLOCATE: primitive_rows.LABEL_ALLOCATE,
        Operation.SHARING_MUTATE: primitive_rows.SHARING_MUTATE,
        Operation.SOURCE_PATCH_TITLE: primitive_rows.SOURCE_PATCH_TITLE,
        Operation.SOURCE_REGISTER: primitive_rows.SOURCE_REGISTER,
        Operation.SHARING_PATCH_VIEW_LEVEL: primitive_rows.SHARING_PATCH_VIEW_LEVEL,
    }
    assert dict(primitive_rows.PRIMITIVE_ROWS) == converted
    for operation, row in converted.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert operation in WEB_SUPPORTED_OPERATIONS
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.map_error is None
        assert row.forward_disable_internal_retries is False
        declared = {(choice.method, choice.variant) for choice in row.native.choices}
        ledger = {
            (native.method, native.variant)
            for native in WEB_CALL_POLICY_BINDINGS[operation].native_bindings
        }
        assert declared == ledger
    assert not primitive_rows.LABEL_MUTATE.native.is_constant
    assert not primitive_rows.SOURCE_REGISTER.native.is_constant
    assert primitive_rows.LABEL_ALLOCATE.native.is_constant
    assert primitive_rows.SHARING_MUTATE.native.is_constant
    assert primitive_rows.SOURCE_PATCH_TITLE.native.is_constant
    assert primitive_rows.SHARING_PATCH_VIEW_LEVEL.native.is_constant
    # One UPDATE_LABEL / one ADD_SOURCE call per input, the variant chosen from it.
    for keyed in (Operation.LABEL_MUTATE, Operation.SOURCE_REGISTER):
        assert SEMANTIC_DEADLINE_AUTHORITIES[keyed] is SemanticDeadlineAuthority.BRANCH_EXCLUSIVE
    assert Operation.LABEL_ALLOCATE not in SEMANTIC_DEADLINE_AUTHORITIES
    assert Operation.SHARING_MUTATE not in SEMANTIC_DEADLINE_AUTHORITIES
    assert Operation.SOURCE_PATCH_TITLE not in SEMANTIC_DEADLINE_AUTHORITIES
    assert Operation.SHARING_PATCH_VIEW_LEVEL not in SEMANTIC_DEADLINE_AUTHORITIES


def test_source_register_keeps_a_distinct_retry_policy_per_declared_choice() -> None:
    """P10 R3.2's precondition: one keyed leaf, per-``NativeChoice`` policy.

    ``SOURCE_REGISTER`` collapses the source-add family's registration writes
    onto one row.  Two of the five ledger rows it replaces are classified
    differently from the other three, so the check that matters is not "the row
    declares three natives" but "each declared choice still resolves to the
    classification its own operation had".  Both the reviewed ledger and the
    live idempotency registry key on ``(method, variant)`` — the exact pair a
    ``NativeChoice`` carries — so nothing here is flattened.
    """
    expected = {
        "url": IdempotencyPolicy.PROBE_THEN_CREATE,
        "text": IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        "drive": IdempotencyPolicy.PROBE_THEN_CREATE,
    }
    reviewed = {
        native.variant: native.expected_policy
        for native in WEB_CALL_POLICY_BINDINGS[Operation.SOURCE_REGISTER].native_bindings
    }

    assert reviewed == expected
    for choice in primitive_rows.SOURCE_REGISTER.native.choices:
        assert choice.method is RPCMethod.ADD_SOURCE
        live = IDEMPOTENCY_REGISTRY.get_entry(
            choice.method, operation_variant=choice.variant
        ).policy
        assert live is expected[str(choice.variant)]

    # The leaf never widens a per-variant classification: every variant it
    # declares keeps the disposition its product operation reviewed — whether
    # that operation still has a row or has already been hoisted above the port.
    for product in (
        Operation.SOURCE_ADD_URL,
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_TEXT,
        Operation.SOURCE_ADD_DRIVE,
    ):
        ledger_row = WEB_CALL_POLICY_BINDINGS.get(product) or SERVICE_OWNED_WORKFLOW_BINDINGS.get(
            product
        )
        assert ledger_row is not None
        for native in ledger_row.native_bindings:
            if native.method is RPCMethod.ADD_SOURCE:
                assert reviewed[str(native.variant)] is native.expected_policy


# --- LABEL_MUTATE ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "variant", "params", "source_path"),
    [
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, add_member_id="s1"),
            "add_sources",
            [_OPTS, _NB, "l1", [[None, [["s1"]]]]],
            f"/notebook/{_NB}",
            id="label-add",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, remove_member_id="s1"),
            "remove_sources",
            [_OPTS, _NB, "l1", [[None, None, [["s1"]]]]],
            f"/notebook/{_NB}",
            id="label-remove",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, name="New", emoji="\U0001f4c4"),
            None,
            [_OPTS, _NB, "l1", [[["New", "\U0001f4c4"]]]],
            f"/notebook/{_NB}",
            id="label-field",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, name="New", emoji=""),
            None,
            [_OPTS, _NB, "l1", [[["New", ""]]]],
            f"/notebook/{_NB}",
            id="label-rename-carrying-empty-emoji",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.COLLECTION, "c1", add_member_id="n1"),
            "add_notebooks",
            [_COLLECTION_OPTS, None, "c1", [[None, None, None, [["n1"]]], []], 3],
            "/",
            id="collection-add",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.COLLECTION, "c1", remove_member_id="n1"),
            "remove_notebooks",
            [_COLLECTION_OPTS, None, "c1", [[None, None, None, None, [["n1"]]], []], 3],
            "/",
            id="collection-remove",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.COLLECTION, "c1", name="New"),
            None,
            [_COLLECTION_OPTS, None, "c1", [[["New"]]], 3],
            "/",
            id="collection-rename",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.COLLECTION, "c1", name="New", emoji="\U0001f525"),
            None,
            [_COLLECTION_OPTS, None, "c1", [[["New", "\U0001f525"]]], 3],
            "/",
            id="collection-rename-with-emoji",
        ),
    ],
)
@pytest.mark.asyncio
async def test_label_mutate_selects_the_variant_and_sends_the_composite_payload(
    value: LabelMutateInput,
    variant: str | None,
    params: list[Any],
    source_path: str,
) -> None:
    choice = primitive_rows.LABEL_MUTATE.native.select(value)
    assert (choice.method, choice.variant) == (RPCMethod.UPDATE_LABEL, variant)
    payload = labels_codec.encode_label_mutate(value)
    assert payload.params == params
    assert payload.source_path == source_path
    assert payload.allow_null is True

    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)
    result = await backend.invoke(LABEL_MUTATE_DEF, value, deadline=None)

    assert result == LabelMutateResult()
    (call,) = executor.calls
    assert call.method is RPCMethod.UPDATE_LABEL
    assert call.params == params
    assert call.kwargs == {
        **_BASE_KWARGS,
        "source_path": source_path,
        "allow_null": True,
        "operation_variant": variant,
    }


@pytest.mark.parametrize(
    ("value", "match"),
    [
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB),
            "exactly one of a field mask",
            id="no-form",
        ),
        pytest.param(
            LabelMutateInput(
                LabelKind.SOURCE_LABEL, "l1", _NB, add_member_id="s1", remove_member_id="s2"
            ),
            "exactly one of a field mask",
            id="two-forms",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, name="X", add_member_id="s1"),
            "exactly one of a field mask",
            id="field-and-add",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", None, name="X"),
            "notebook",
            id="label-without-scope",
        ),
        pytest.param(
            LabelMutateInput(LabelKind.COLLECTION, "c1", emoji="\U0001f525"),
            "a name is required",
            id="collection-emoji-only",
        ),
    ],
)
@pytest.mark.asyncio
async def test_label_mutate_contract_errors_fire_before_any_wire_call(
    value: LabelMutateInput, match: str
) -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    with pytest.raises(BackendContractError, match=match) as caught:
        await backend.invoke(LABEL_MUTATE_DEF, value, deadline=None)
    assert caught.value.operation is Operation.LABEL_MUTATE
    assert executor.calls == []


# --- LABEL_ALLOCATE -------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_allocate_decodes_the_source_label_echo() -> None:
    echo = [None, [["Papers", None, "l9", "\U0001f4c4"], ["Other", [["s1"]], "l2", ""]]]
    executor = _RecordingExecutor(echo)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        LABEL_ALLOCATE_DEF,
        LabelAllocateInput(LabelKind.SOURCE_LABEL, "Papers", _NB, emoji="\U0001f4c4"),
        deadline=None,
    )

    assert [label.id for label in result.labels] == ["l9", "l2"]
    assert result.labels[0].kind is LabelKind.SOURCE_LABEL
    assert result.labels[0].notebook_id == _NB
    assert result.labels[1].member_ids == ("s1",)
    (call,) = executor.calls
    assert call.method is RPCMethod.CREATE_LABEL
    assert call.params == labels_codec.build_create_label_params(_NB, "Papers", "\U0001f4c4")
    assert call.kwargs == {**_BASE_KWARGS, "source_path": f"/notebook/{_NB}", "allow_null": True}


@pytest.mark.asyncio
async def test_label_allocate_returns_no_echo_for_collections() -> None:
    executor = _RecordingExecutor(None)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        LABEL_ALLOCATE_DEF,
        LabelAllocateInput(LabelKind.COLLECTION, "Research"),
        deadline=None,
    )

    assert result == LabelAllocateResult()
    (call,) = executor.calls
    assert call.method is RPCMethod.CREATE_LABEL
    assert call.params == [_COLLECTION_CREATE_OPTS, None, None, None, None, [["Research"]], 3]
    assert call.kwargs == {**_BASE_KWARGS, "source_path": "/", "allow_null": True}


@pytest.mark.asyncio
async def test_label_allocate_requires_a_notebook_scope_for_source_labels() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    with pytest.raises(BackendContractError, match="notebook"):
        await backend.invoke(
            LABEL_ALLOCATE_DEF,
            LabelAllocateInput(LabelKind.SOURCE_LABEL, "Papers"),
            deadline=None,
        )
    assert executor.calls == []


# --- SHARING_MUTATE -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sharing_mutate_sends_the_visibility_or_grant_envelope() -> None:
    grant = SharingUserGrant("reader@example.com", SharePermissionLevel.VIEWER)
    executor = _RecordingExecutor(None, None, None)
    backend = build_web_backend(executor)

    visibility = await backend.invoke(
        SHARING_MUTATE_DEF,
        SharingMutateInput(_NB, SharingVisibility(public=True)),
        deadline=None,
    )
    grants = await backend.invoke(
        SHARING_MUTATE_DEF,
        SharingMutateInput(
            _NB,
            SharingGrants(grants=(grant,), notify=False, welcome_message="hi"),
        ),
        deadline=None,
    )
    empty_grants = await backend.invoke(
        SHARING_MUTATE_DEF,
        SharingMutateInput(_NB, SharingGrants(grants=(), notify=False)),
        deadline=None,
    )

    assert visibility == SharingMutateResult()
    assert grants == SharingMutateResult()
    assert empty_grants == SharingMutateResult()
    first, second, third = executor.calls
    assert first.method is RPCMethod.SHARE_NOTEBOOK
    assert first.params == sharing_codec.build_share_visibility_params(_NB, True)
    assert first.kwargs == {**_BASE_KWARGS, "source_path": f"/notebook/{_NB}", "allow_null": True}
    assert second.method is RPCMethod.SHARE_NOTEBOOK
    assert second.params == sharing_codec.build_share_grants_params(
        _NB, (grant,), notify=False, welcome_message="hi"
    )
    assert second.kwargs == first.kwargs
    assert third.params == sharing_codec.build_share_grants_params(
        _NB, (), notify=False, welcome_message=""
    )
    assert third.kwargs == first.kwargs


@pytest.mark.asyncio
async def test_sharing_mutate_rejects_a_value_outside_the_closed_union_before_dispatch() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    value = SharingMutateInput(_NB, object())  # type: ignore[arg-type]
    with pytest.raises(BackendContractError, match="SharingVisibility or SharingGrants") as caught:
        await backend.invoke(SHARING_MUTATE_DEF, value, deadline=None)
    assert caught.value.operation is Operation.SHARING_MUTATE
    assert executor.calls == []


# --- SOURCE_REGISTER ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "variant", "params", "allow_null"),
    [
        pytest.param(
            SourceRegisterInput(
                _NB,
                SourceRegisterKind.URL,
                urls=("https://example.com/doc",),
                youtube_flags=(False,),
            ),
            "url",
            [
                [
                    [
                        None,
                        None,
                        ["https://example.com/doc"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                    ]
                ],
                _NB,
                _OPTS,
            ],
            False,
            id="url-single",
        ),
        pytest.param(
            SourceRegisterInput(
                _NB,
                SourceRegisterKind.URL,
                urls=("https://a.example/", "https://youtu.be/x"),
                youtube_flags=(False, True),
            ),
            "url",
            [
                [
                    [
                        None,
                        None,
                        ["https://a.example/"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                    ],
                    [
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        ["https://youtu.be/x"],
                        None,
                        None,
                        1,
                    ],
                ],
                _NB,
                _OPTS,
            ],
            False,
            id="url-true-batch",
        ),
        pytest.param(
            SourceRegisterInput(_NB, SourceRegisterKind.TEXT, title="Pasted", content="body"),
            "text",
            [
                [[None, ["Pasted", "body"], None, 2, None, None, None, None, None, None, 1]],
                _NB,
                _OPTS,
            ],
            False,
            id="text",
        ),
        pytest.param(
            SourceRegisterInput(
                _NB,
                SourceRegisterKind.DRIVE,
                file_id="file-id",
                title="Doc",
                mime_type="application/pdf",
            ),
            "drive",
            [
                [
                    [
                        ["file-id", "application/pdf", 1, "Doc"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                    ]
                ],
                _NB,
                [2],
                [1, None, None, None, None, None, None, None, None, None, [1]],
            ],
            True,
            id="drive",
        ),
    ],
)
@pytest.mark.asyncio
async def test_source_register_selects_the_variant_and_sends_the_family_payload(
    value: SourceRegisterInput,
    variant: str,
    params: list[Any],
    allow_null: bool,
) -> None:
    """One row, three registration payloads, byte-identical to the P9.4b handlers."""
    choice = primitive_rows.SOURCE_REGISTER.native.select(value)
    assert (choice.method, choice.variant) == (RPCMethod.ADD_SOURCE, variant)

    executor = _RecordingExecutor([[_source_entry("src", title="Echoed")]])
    result = await build_web_backend(executor).invoke(SOURCE_REGISTER_DEF, value, deadline=None)

    assert isinstance(result, SourceRegisterResult)
    assert [source.id for source in result.sources] == ["src"]
    (call,) = executor.calls
    assert call.method is RPCMethod.ADD_SOURCE
    assert call.params == params
    assert call.kwargs == {
        **_BASE_KWARGS,
        "source_path": f"/notebook/{_NB}",
        "allow_null": allow_null,
        "operation_variant": variant,
    }


@pytest.mark.asyncio
async def test_source_register_decodes_every_echoed_row_in_wire_order() -> None:
    """A true batch echo stays positional: reconciliation is the workflow's job."""
    executor = _RecordingExecutor(
        [[_source_entry("a", title="A"), _source_entry("b", title="B")]],
    )
    value = SourceRegisterInput(
        _NB,
        SourceRegisterKind.URL,
        urls=("https://a.example/", "https://b.example/"),
        youtube_flags=(False, False),
    )

    result = await build_web_backend(executor).invoke(SOURCE_REGISTER_DEF, value, deadline=None)

    assert [source.id for source in result.sources] == ["a", "b"]


@pytest.mark.asyncio
async def test_source_register_tolerates_the_drive_null_echo() -> None:
    """``allow_null`` is legal on the Drive variant; the leaf reports no rows."""
    executor = _RecordingExecutor(None)
    value = SourceRegisterInput(
        _NB,
        SourceRegisterKind.DRIVE,
        file_id="file-id",
        title="Doc",
        mime_type="application/pdf",
    )

    result = await build_web_backend(executor).invoke(SOURCE_REGISTER_DEF, value, deadline=None)

    assert result == SourceRegisterResult(())


@pytest.mark.parametrize(
    ("value", "match"),
    [
        pytest.param(
            SourceRegisterInput(_NB, SourceRegisterKind.URL),
            "one YouTube discriminator per URL",
            id="url-without-urls",
        ),
        pytest.param(
            SourceRegisterInput(
                _NB, SourceRegisterKind.URL, urls=("https://a.example/",), youtube_flags=()
            ),
            "one YouTube discriminator per URL",
            id="url-flag-count-mismatch",
        ),
        pytest.param(
            SourceRegisterInput(_NB, SourceRegisterKind.TEXT, title="Pasted"),
            "a title and a body",
            id="text-without-body",
        ),
        pytest.param(
            SourceRegisterInput(_NB, SourceRegisterKind.DRIVE, file_id="f"),
            "a file id, a title and a MIME type",
            id="drive-without-title",
        ),
    ],
)
@pytest.mark.asyncio
async def test_source_register_contract_errors_fire_before_any_wire_call(
    value: SourceRegisterInput, match: str
) -> None:
    """A payload its kind cannot carry is rejected ahead of the create."""
    executor = _RecordingExecutor()
    with pytest.raises(BackendContractError, match=match) as caught:
        await build_web_backend(executor).invoke(SOURCE_REGISTER_DEF, value, deadline=None)
    assert caught.value.operation is Operation.SOURCE_REGISTER
    assert executor.calls == []


def test_source_register_variant_is_the_registration_kind() -> None:
    """One field fixes the payload shape and the reviewed retry classification."""
    for kind in SourceRegisterKind:
        value = SourceRegisterInput(_NB, kind)
        assert sources_codec.source_register_variant(value) == kind.value


# --- failure projection ---------------------------------------------------------


@pytest.mark.asyncio
async def test_primitive_server_error_translates_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.UPDATE_LABEL.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            LABEL_MUTATE_DEF,
            LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, add_member_id="s1"),
            deadline=None,
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LABEL_MUTATE
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_primitive_timeout_after_expiry_is_a_dispatched_mutation_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.SHARE_NOTEBOOK.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            SHARING_MUTATE_DEF,
            SharingMutateInput(_NB, SharingVisibility(public=True)),
            deadline=deadline,
        )

    error = caught.value
    assert error.operation is Operation.SHARING_MUTATE
    assert error.outcome_unknown is True  # MUTATION policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.SHARE_NOTEBOOK.value


@pytest.mark.asyncio
async def test_primitive_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            LABEL_ALLOCATE_DEF,
            LabelAllocateInput(LabelKind.COLLECTION, "Research"),
            deadline=deadline,
        )

    assert executor.calls == []
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
