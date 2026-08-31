"""Unit tests for sharing types and API."""

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import ShareAccess, SharePermission, ShareViewLevel
from notebooklm.types import SharedUser, ShareStatus

#: The full ``GET_SHARE_STATUS`` payload as CAPTURED, copied verbatim from the
#: response body recorded in ``tests/cassettes/web/cli_share_status.yaml`` and
#: re-confirmed byte-identical in shape on 10/10 notebooks in a 2026-08 live
#: sweep. Not hand-authored: the older fixtures in this module stop at three
#: elements, which is exactly why slots 2-3 went unread until #2130.
LIVE_SHARE_STATUS_ROW: list[Any] = [
    [["owner@example.com", 1, [], ["Owner", "https://avatar=s512"]]],
    None,
    1000,
    True,
    None,
    None,
    [3, True, True],
    False,
]


class TestShareStatusCapacityAndPolicyFields:
    """``maxIndividualsShareLimit`` / ``isPublicSharingAllowed`` decoding (#2130).

    Both slots are populated on every live response and were read by nobody; the
    parser docstring described slot 2 as the bare literal ``1000`` without naming
    it. The absent-slot cases below are not hypothetical — the pinned golden
    capture ``tests/fixtures/rpc_golden/GET_SHARE_STATUS.json`` is a real
    three-element response.
    """

    @pytest.mark.parametrize("data", [True, 1, 1.2])
    def test_truthy_non_list_preserves_structured_decode_error(self, data: object) -> None:
        """The row extraction preserves the legacy first-slot decode ordering."""
        with pytest.raises(UnknownRPCMethodError):
            ShareStatus.from_api_response(data, "nb-1")  # type: ignore[arg-type]

    def test_decodes_both_fields_from_the_captured_row(self):
        """The live 8-slot shape yields the real cap and the real policy gate."""
        status = ShareStatus.from_api_response(LIVE_SHARE_STATUS_ROW, "nb-1")

        assert status.max_individuals_share_limit == 1000
        assert status.is_public_sharing_allowed is True
        # Additive: the fields this parser already read are untouched.
        assert status.is_public is False
        assert len(status.shared_users) == 1

    def test_short_response_reports_no_claim_rather_than_zero(self):
        """A 3-element response (the pinned golden capture's real shape).

        The distinction that matters: ``None`` is "the backend said nothing",
        not a cap of ``0`` (which would read as "you may add no collaborators")
        and not a policy denial.
        """
        status = ShareStatus.from_api_response(
            [[["owner@example.com", 1, [], ["Owner", None]]], None, 1000], "nb-1"
        )

        assert status.max_individuals_share_limit == 1000
        assert status.is_public_sharing_allowed is None

    def test_fields_absent_entirely_stay_none(self):
        status = ShareStatus.from_api_response([[], None], "nb-1")

        assert status.max_individuals_share_limit is None
        assert status.is_public_sharing_allowed is None

    def test_policy_denial_is_distinguishable_from_no_claim(self):
        """``False`` and ``None`` must not collapse — they have opposite meanings.

        A caller gating a "make public" attempt has to be able to tell "the
        tenant forbids this" from "this response did not say".
        """
        denied = ShareStatus.from_api_response(
            [[], None, 1000, False, None, None, [3, True, True], False], "nb-1"
        )
        silent = ShareStatus.from_api_response([[], None, 1000], "nb-1")

        assert denied.is_public_sharing_allowed is False
        assert silent.is_public_sharing_allowed is None
        assert denied.is_public_sharing_allowed is not silent.is_public_sharing_allowed

    def test_null_slots_decode_as_no_claim(self):
        """Explicit ``null`` in either slot is absence, not a value."""
        status = ShareStatus.from_api_response([[], None, None, None], "nb-1")

        assert status.max_individuals_share_limit is None
        assert status.is_public_sharing_allowed is None

    def test_boolean_in_the_limit_slot_is_rejected(self):
        """``bool`` is an ``int`` subclass — ``True`` must not decode as a cap of 1.

        Without the explicit ``bool`` exclusion this returns ``True``, and a
        bulk-share caller budgeting against it would stop after one user.
        """
        status = ShareStatus.from_api_response([[], None, True, True], "nb-1")

        assert status.max_individuals_share_limit is None

    @pytest.mark.parametrize("drifted", [1, "true", "yes", [True]])
    def test_non_boolean_in_the_policy_slot_is_rejected(self, drifted: Any):
        """A truthy non-bool is drift, not a policy verdict, and must not coerce."""
        status = ShareStatus.from_api_response([[], None, 1000, drifted], "nb-1")

        assert status.is_public_sharing_allowed is None

    def test_string_in_the_limit_slot_is_rejected(self):
        status = ShareStatus.from_api_response([[], None, "1000", True], "nb-1")

        assert status.max_individuals_share_limit is None

    @pytest.mark.asyncio
    async def test_set_view_level_preserves_the_decoded_fields(
        self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response
    ):
        """``set_view_level`` must not discard what ``get_status`` just decoded.

        It re-fetches the status and overrides only ``view_level``. That rebuild
        used to list six fields explicitly, so the cap and the policy gate came
        back ``None`` — reporting "the backend made no claim" about values the
        backend had, in the same call, just stated. The REST
        ``POST /share/view-level`` route and the MCP view-level-only branch of
        ``share_set_access`` both project this object, so the nulls reached
        users on two adapters while every suite stayed green.
        """
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.RENAME_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, LIVE_SHARE_STATUS_ROW).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_view_level("nb_123", ShareViewLevel.CHAT_ONLY)

        # The point of the call still holds.
        assert status.view_level == ShareViewLevel.CHAT_ONLY
        # ...and nothing else was dropped on the way.
        assert status.max_individuals_share_limit == 1000
        assert status.is_public_sharing_allowed is True

    def test_denied_predicate_fires_only_on_an_explicit_false(self):
        """``is_public_sharing_denied`` must not fire on the unknown case.

        This is the whole reason the property exists: the idiomatic
        ``not status.is_public_sharing_allowed`` is ``True`` for ``None`` too,
        so it reports a denial the backend never made.
        """
        denied = ShareStatus.from_api_response([[], None, 1000, False], "nb-1")
        allowed = ShareStatus.from_api_response([[], None, 1000, True], "nb-1")
        silent = ShareStatus.from_api_response([[], None, 1000], "nb-1")

        assert denied.is_public_sharing_denied is True
        assert allowed.is_public_sharing_denied is False
        assert silent.is_public_sharing_denied is False
        # The trap the property exists to replace: the naive spelling gets the
        # silent case wrong, while the property gets it right.
        assert not silent.is_public_sharing_allowed
        assert silent.is_public_sharing_denied is False

    def test_malformed_slot_warns_but_absent_slot_is_silent(self, caplog):
        """Drift is reported; genuine absence is not.

        Absence and drift share the ``None`` representation, so without a log
        line a backend shape change in either slot would be completely
        invisible — and shape drift is this client's #1 breakage class.
        """
        import logging

        from notebooklm._web.rows import sharing as sharing_rows

        sharing_rows._warned_malformed_share_slots.clear()

        with caplog.at_level(logging.WARNING, logger="notebooklm._types.sharing"):
            ShareStatus.from_api_response([[], None, "1000", "yes"], "nb-1")
        assert "maxIndividualsShareLimit" in caplog.text
        assert "isPublicSharingAllowed" in caplog.text

        # A short response is normal, not drift, and must stay quiet.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="notebooklm._types.sharing"):
            ShareStatus.from_api_response([[], None], "nb-1")
        assert caplog.text == ""

        # An explicit null is absence too.
        with caplog.at_level(logging.WARNING, logger="notebooklm._types.sharing"):
            ShareStatus.from_api_response([[], None, None, None], "nb-1")
        assert caplog.text == ""

    def test_malformed_slot_warn_cache_is_bounded(self):
        """The warn-once cache must not grow with the number of distinct payloads.

        It sits on a decode path that runs on every share-status read, so keying
        it on the value would leak memory in any long-lived process (the REST
        server, an MCP session). Keying on the *type* bounds it by construction:
        feeding 500 distinct malformed values adds one entry per (field, type).
        """
        from notebooklm._web.rows import sharing as sharing_rows

        sharing_rows._warned_malformed_share_slots.clear()

        for i in range(500):
            ShareStatus.from_api_response([[], None, f"cap-{i}", f"gate-{i}"], "nb-1")

        # Two fields, one type (str) each — not 1000 entries.
        assert sharing_rows._warned_malformed_share_slots == {
            ("maxIndividualsShareLimit", "str"),
            ("isPublicSharingAllowed", "str"),
        }

        # A genuinely different failure mode is still reported once more, so
        # bounding the cache did not cost the signal it exists to carry.
        ShareStatus.from_api_response([[], None, [1], [2]], "nb-1")
        assert ("maxIndividualsShareLimit", "list") in sharing_rows._warned_malformed_share_slots
        assert len(sharing_rows._warned_malformed_share_slots) == 4

    def test_malformed_slot_warns_once_per_failure_mode(self, caplog):
        """A polled notebook must not re-emit the same drift line every decode.

        "Once" is per ``(field, type)`` — the granularity the bounded cache
        keys on — not per distinct value.
        """
        import logging

        from notebooklm._web.rows import sharing as sharing_rows

        sharing_rows._warned_malformed_share_slots.clear()

        with caplog.at_level(logging.WARNING, logger="notebooklm._types.sharing"):
            for _ in range(5):
                ShareStatus.from_api_response([[], None, "1000", True], "nb-1")

        assert caplog.text.count("maxIndividualsShareLimit") == 1

    def test_unnamed_trailing_slots_are_not_surfaced(self):
        """Tags 7-8 are populated live but deliberately undecoded (#2130).

        The mobile ``GetProjectDetailsResponse`` declares only tags 2-4, so
        nothing names them. This pins the decision: if a future change starts
        exposing them, it must come with a name and a wire-contract entry.
        """
        status = ShareStatus.from_api_response(LIVE_SHARE_STATUS_ROW, "nb-1")

        # An exact field-set comparison, deliberately not a name heuristic or a
        # search for the raw value. Both of those are escapable: a slot exposed
        # under any name that does not contain "tag", or stored decomposed
        # (``can_invite=data[6][1]``) rather than verbatim, would slip past.
        # Pinning the whole set fails for ANY new field regardless of name or
        # shape, which is the actual intent — a new field is then a deliberate
        # act that updates this list and, for a wire slot, adds the naming
        # evidence to the wire contract.
        assert set(vars(status)) == {
            "notebook_id",
            "is_public",
            "access",
            "view_level",
            "shared_users",
            "share_url",
            "max_individuals_share_limit",
            "is_public_sharing_allowed",
        }


class TestSharedUser:
    """Tests for SharedUser dataclass."""

    def test_from_api_response_full(self):
        """Test parsing with all fields present."""
        data = ["user@example.com", 3, [], ["Test User", "https://avatar.url"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "user@example.com"
        assert user.permission == SharePermission.VIEWER
        assert user.display_name == "Test User"
        assert user.avatar_url == "https://avatar.url"

    def test_from_api_response_editor(self):
        """Test parsing editor permission."""
        data = ["editor@example.com", 2, [], ["Editor Name", "https://editor.avatar"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "editor@example.com"
        assert user.permission == SharePermission.EDITOR
        assert user.display_name == "Editor Name"

    def test_from_api_response_owner(self):
        """Test parsing owner permission."""
        data = ["owner@example.com", 1, [], ["Owner Name", "https://owner.avatar"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "owner@example.com"
        assert user.permission == SharePermission.OWNER

    def test_from_api_response_minimal(self):
        """Test parsing with minimal fields."""
        data = ["user@example.com", 2, []]
        user = SharedUser.from_api_response(data)

        assert user.email == "user@example.com"
        assert user.permission == SharePermission.EDITOR
        assert user.display_name is None
        assert user.avatar_url is None

    def test_from_api_response_unknown_permission(self):
        """Test parsing with unknown permission value defaults to VIEWER."""
        data = ["user@example.com", 99, []]
        user = SharedUser.from_api_response(data)

        assert user.permission == SharePermission.VIEWER

    def test_from_api_response_malformed_permission(self):
        """Test parsing with malformed permission value defaults to VIEWER."""
        data = ["user@example.com", {"permission": 3}, []]
        user = SharedUser.from_api_response(data)

        assert user.permission == SharePermission.VIEWER

    def test_from_api_response_empty(self):
        """Test parsing with empty data."""
        data = []
        user = SharedUser.from_api_response(data)

        assert user.email == ""
        assert user.permission == SharePermission.VIEWER

    def test_from_api_response_malformed_email_warns(self, caplog):
        """A present-but-non-str email slot fabricates ``""`` LOUDLY (#1485).

        The degrade is kept (a raising entry parser would abort the whole
        shared-user list), but the fabricated empty email now leaves a
        WARNING with a bounded payload preview instead of silently flowing a
        non-string into ``SharedUser.email``.
        """
        import logging

        data = [12345, 2]
        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            user = SharedUser.from_api_response(data)

        assert user.email == ""
        assert any(
            r.levelno == logging.WARNING and "email slot malformed" in r.message
            for r in caplog.records
        )

    def test_from_api_response_null_email_is_silent_empty(self, caplog):
        """A ``None`` email slot is absence, not drift — silent ``""`` degrade."""
        import logging

        data = [None, 2]
        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            user = SharedUser.from_api_response(data)

        assert user.email == ""
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_from_api_response_partial_user_info(self):
        """Test parsing with partial user info (only name, no avatar)."""
        data = ["user@example.com", 3, [], ["Just Name"]]
        user = SharedUser.from_api_response(data)

        assert user.display_name == "Just Name"
        assert user.avatar_url is None


class TestShareStatus:
    """Tests for ShareStatus dataclass."""

    def test_from_api_response_public(self):
        """Test parsing public notebook."""
        data = [
            [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
            [True],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-123")

        assert status.notebook_id == "notebook-123"
        assert status.is_public is True
        assert status.access == ShareAccess.ANYONE_WITH_LINK
        assert status.view_level == ShareViewLevel.FULL_NOTEBOOK
        assert len(status.shared_users) == 1
        assert status.shared_users[0].email == "owner@example.com"
        assert status.share_url == "https://notebook.google.com/notebook/notebook-123"

    def test_from_api_response_private(self):
        """Test parsing private/restricted notebook."""
        data = [
            [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
            [False],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-456")

        assert status.notebook_id == "notebook-456"
        assert status.is_public is False
        assert status.access == ShareAccess.RESTRICTED
        assert status.share_url is None

    def test_from_api_response_multiple_users(self):
        """Test parsing with multiple shared users."""
        data = [
            [
                ["owner@example.com", 1, [], ["Owner", "https://owner.avatar"]],
                ["editor@example.com", 2, [], ["Editor", "https://editor.avatar"]],
                ["viewer@example.com", 3, [], ["Viewer", "https://viewer.avatar"]],
            ],
            [True],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-789")

        assert len(status.shared_users) == 3
        assert status.shared_users[0].permission == SharePermission.OWNER
        assert status.shared_users[1].permission == SharePermission.EDITOR
        assert status.shared_users[2].permission == SharePermission.VIEWER

    def test_from_api_response_empty_users(self):
        """Test parsing with no users."""
        data = [[], [False], 1000]
        status = ShareStatus.from_api_response(data, "notebook-empty")

        assert status.shared_users == []
        assert status.is_public is False

    def test_from_api_response_empty_is_public(self):
        """Test parsing when is_public list is empty."""
        data = [[], [], 1000]
        status = ShareStatus.from_api_response(data, "notebook-empty")

        assert status.is_public is False
        assert status.access == ShareAccess.RESTRICTED


def _legacy_shared_user_from_api_response(data: list[Any]) -> dict[str, Any]:
    """Verbatim copy of the PRE-DRAIN ``SharedUser.from_api_response`` decode.

    Mirrors the hand-rolled ``data[i]`` reads that the ``safe_index`` migration
    replaced, so the differential test below can prove byte-for-byte parity on
    present / empty / too-short / malformed inputs. Returns only the decoded
    fields (no warning side-effect) as a dict.
    """
    email = ""
    if data:
        raw_email = data[0]
        if isinstance(raw_email, str):
            email = raw_email
    perm_value = data[1] if len(data) > 1 else 3
    try:
        permission = SharePermission(perm_value)
    except (TypeError, ValueError):
        permission = SharePermission.VIEWER

    display_name = None
    avatar_url = None
    if len(data) > 3 and isinstance(data[3], list):
        user_info = data[3]
        display_name = user_info[0] if user_info else None
        avatar_url = user_info[1] if len(user_info) > 1 else None

    return {
        "email": email,
        "permission": permission,
        "display_name": display_name,
        "avatar_url": avatar_url,
    }


def _legacy_share_status_from_api_response(data: list[Any], notebook_id: str) -> dict[str, Any]:
    """Verbatim copy of the PRE-DRAIN ``ShareStatus.from_api_response`` decode.

    Only the positionally-decoded fields (``is_public`` and the parsed
    shared-user count) are returned — enough to assert parity against the
    ``safe_index`` migration without re-deriving the URL/access logic, which
    flows deterministically from ``is_public``.
    """
    users: list[Any] = []
    if data and isinstance(data[0], list):
        for user_data in data[0]:
            if isinstance(user_data, list):
                users.append(user_data)

    is_public = False
    public_block = data[1] if len(data) > 1 and isinstance(data[1], list) else None
    if public_block:
        is_public = bool(public_block[0])

    return {"is_public": is_public, "user_count": len(users)}


# Inputs spanning present / empty / too-short / malformed shapes. The
# ``safe_index`` migration must decode each identically to the legacy logic
# above — soft reads stay soft (each ``safe_index`` sits after the guard that
# proves its slot present, so it never raises on these).
_SHARED_USER_DIFFERENTIAL_INPUTS: list[Any] = [
    ["user@example.com", 3, [], ["Name", "https://avatar"]],  # full
    ["user@example.com", 2, []],  # minimal (no user_info slot)
    ["user@example.com", 3, [], ["Just Name"]],  # partial user_info (no avatar)
    ["user@example.com", 99, []],  # unknown permission
    ["user@example.com", {"k": 1}, []],  # malformed (unhashable) permission
    [],  # empty
    [None, 2],  # null email slot
    [12345, 2],  # malformed (non-str) email slot
    ["only-email"],  # too-short (no permission slot)
    ["e", 2, [], []],  # empty user_info list
    ["e", 2, [], "not-a-list"],  # slot 3 present but non-list
]

_SHARE_STATUS_DIFFERENTIAL_INPUTS: list[Any] = [
    [[["owner@example.com", 1, [], ["Owner", "a"]]], [True], 1000],  # public, 1 user
    [[["o@e.com", 1, []], ["e@e.com", 2, []]], [False], 1000],  # private, 2 users
    [[], [False], 1000],  # empty users
    [[], [], 1000],  # empty is_public block
    [[], [True], 1000],  # public, no users
    [],  # fully empty payload
    [[]],  # only users slot present (too-short)
    ["not-a-list", [True]],  # users slot non-list
    [[], "not-a-list", 1000],  # is_public slot non-list
    [[None, "x"], [True]],  # a non-list user entry is skipped
]


class TestSharingDecodeDifferential:
    """``safe_index`` migration preserves the legacy positional-decode semantics."""

    @pytest.mark.parametrize("data", _SHARED_USER_DIFFERENTIAL_INPUTS)
    def test_shared_user_matches_legacy(self, data: Any) -> None:
        legacy = _legacy_shared_user_from_api_response(data)
        actual = SharedUser.from_api_response(data)
        assert actual.email == legacy["email"]
        assert actual.permission == legacy["permission"]
        assert actual.display_name == legacy["display_name"]
        assert actual.avatar_url == legacy["avatar_url"]

    @pytest.mark.parametrize("data", _SHARE_STATUS_DIFFERENTIAL_INPUTS)
    def test_share_status_matches_legacy(self, data: Any) -> None:
        legacy = _legacy_share_status_from_api_response(data, "nb_diff")
        actual = ShareStatus.from_api_response(data, "nb_diff")
        assert actual.is_public == legacy["is_public"]
        assert len(actual.shared_users) == legacy["user_count"]


class TestShareEnums:
    """Tests for share-related enums."""

    def test_share_access_values(self):
        """Test ShareAccess enum values."""
        assert ShareAccess.RESTRICTED.value == 0
        assert ShareAccess.ANYONE_WITH_LINK.value == 1

    def test_share_view_level_values(self):
        """Test ShareViewLevel enum values."""
        assert ShareViewLevel.FULL_NOTEBOOK.value == 0
        assert ShareViewLevel.CHAT_ONLY.value == 1

    def test_share_permission_values(self):
        """Test SharePermission enum values."""
        assert SharePermission.OWNER.value == 1
        assert SharePermission.EDITOR.value == 2
        assert SharePermission.VIEWER.value == 3
        assert SharePermission._REMOVE.value == 4

    def test_share_access_is_int_enum(self):
        """Test ShareAccess can be used as int."""
        assert int(ShareAccess.RESTRICTED) == 0
        assert int(ShareAccess.ANYONE_WITH_LINK) == 1

    def test_share_view_level_is_int_enum(self):
        """Test ShareViewLevel can be used as int."""
        assert int(ShareViewLevel.FULL_NOTEBOOK) == 0
        assert int(ShareViewLevel.CHAT_ONLY) == 1


class TestSharingAPIValidation:
    """Tests for SharingAPI input validation."""

    @pytest.mark.asyncio
    async def test_add_user_rejects_owner_permission(self):
        """Test that add_user rejects OWNER permission."""
        from unittest.mock import AsyncMock

        from notebooklm._web.sharing import WebSharingAPI
        from tests._fixtures.fake_core import make_fake_core

        mock_core = make_fake_core(rpc_call=AsyncMock())
        api = WebSharingAPI(mock_core)

        with pytest.raises(ValueError, match="Cannot assign OWNER permission"):
            await api.add_user("nb_123", "test@example.com", SharePermission.OWNER)

        # Verify no RPC call was made
        mock_core.rpc_executor.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_user_rejects_remove_permission(self):
        """Test that add_user rejects _REMOVE permission."""
        from unittest.mock import AsyncMock

        from notebooklm._web.sharing import WebSharingAPI
        from tests._fixtures.fake_core import make_fake_core

        mock_core = make_fake_core(rpc_call=AsyncMock())
        api = WebSharingAPI(mock_core)

        with pytest.raises(ValueError, match="Use remove_user"):
            await api.add_user("nb_123", "test@example.com", SharePermission._REMOVE)

        mock_core.rpc_executor.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_user_accepts_editor_permission(self):
        """Test that add_user accepts EDITOR permission."""
        from unittest.mock import AsyncMock

        from notebooklm._web.sharing import WebSharingAPI
        from tests._fixtures.fake_core import make_fake_core

        # Return empty list for share call, then mock get_status
        mock_core = make_fake_core(
            rpc_call=AsyncMock(
                side_effect=[
                    [],  # SHARE_NOTEBOOK response
                    [  # GET_SHARE_STATUS response
                        [["test@example.com", 2, [], ["Test", "https://avatar"]]],
                        [False],
                        1000,
                    ],
                ]
            )
        )
        api = WebSharingAPI(mock_core)

        status = await api.add_user("nb_123", "test@example.com", SharePermission.EDITOR)

        assert mock_core.rpc_executor.rpc_call.call_count == 2
        assert len(status.shared_users) == 1
        assert status.shared_users[0].permission == SharePermission.EDITOR

    @pytest.mark.asyncio
    async def test_add_user_accepts_viewer_permission(self):
        """Test that add_user accepts VIEWER permission (default)."""
        from unittest.mock import AsyncMock

        from notebooklm._web.sharing import WebSharingAPI
        from tests._fixtures.fake_core import make_fake_core

        mock_core = make_fake_core(
            rpc_call=AsyncMock(
                side_effect=[
                    [],  # SHARE_NOTEBOOK response
                    [  # GET_SHARE_STATUS response
                        [["test@example.com", 3, [], ["Test", "https://avatar"]]],
                        [False],
                        1000,
                    ],
                ]
            )
        )
        api = WebSharingAPI(mock_core)

        # Use default permission (VIEWER)
        status = await api.add_user("nb_123", "test@example.com")

        assert mock_core.rpc_executor.rpc_call.call_count == 2
        assert status.shared_users[0].permission == SharePermission.VIEWER


class TestShareStatusDefaultValues:
    """Test ShareStatus default values and edge cases."""

    def test_default_view_level_is_full_notebook(self):
        """ShareStatus defaults view_level to FULL_NOTEBOOK."""
        data = [[], [True], 1000]
        status = ShareStatus.from_api_response(data, "nb_123")
        assert status.view_level == ShareViewLevel.FULL_NOTEBOOK

    def test_share_url_format(self):
        """Test share URL is correctly formatted."""
        data = [[], [True], 1000]
        status = ShareStatus.from_api_response(data, "abc-123-xyz")
        assert status.share_url == "https://notebook.google.com/notebook/abc-123-xyz"

    def test_share_url_quotes_notebook_id(self):
        """Reserved characters in notebook IDs must be percent-encoded."""
        data = [[], [True], 1000]
        status = ShareStatus.from_api_response(data, "foo bar/baz?x")

        assert status.share_url == "https://notebook.google.com/notebook/foo%20bar%2Fbaz%3Fx"
        assert "foo bar/baz?x" not in status.share_url

    def test_share_url_none_when_private(self):
        """Test share URL is None when notebook is private."""
        data = [[], [False], 1000]
        status = ShareStatus.from_api_response(data, "nb_123")
        assert status.share_url is None

    def test_shared_users_is_mutable_list(self):
        """Test that shared_users default is a mutable list."""
        data = [[], [False], 1000]
        status1 = ShareStatus.from_api_response(data, "nb_1")
        status2 = ShareStatus.from_api_response(data, "nb_2")

        # Modifying one should not affect the other
        status1.shared_users.append(
            SharedUser(email="test@example.com", permission=SharePermission.VIEWER)
        )
        assert len(status1.shared_users) == 1
        assert len(status2.shared_users) == 0


class TestSetUsers:
    """Tests for SharingAPI.set_users()."""

    @pytest.mark.asyncio
    async def test_set_users_sends_mixed_grants_in_one_request(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """A bulk grant shares per-call notification settings and refreshes once."""
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.GET_SHARE_STATUS,
                [
                    [
                        ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                        ["viewer@example.com", 3, [], ["Viewer", "https://viewer"]],
                        ["editor@example.com", 2, [], ["Editor", "https://editor"]],
                    ],
                    [False],
                    1000,
                ],
            ).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_users(
                "nb_123",
                [
                    ("viewer@example.com", SharePermission.VIEWER),
                    ("editor@example.com", SharePermission.EDITOR),
                ],
                notify=False,
                welcome_message="Welcome, team!",
            )

        requests = httpx_mock.get_requests()
        assert len(requests) == 2
        assert requests[0].url.params["rpcids"] == RPCMethod.SHARE_NOTEBOOK.value
        assert requests[1].url.params["rpcids"] == RPCMethod.GET_SHARE_STATUS.value
        assert rpc_request_params(requests[0]) == [
            [
                [
                    "nb_123",
                    [
                        ["viewer@example.com", None, SharePermission.VIEWER.value],
                        ["editor@example.com", None, SharePermission.EDITOR.value],
                    ],
                    None,
                    [0, "Welcome, team!"],
                ]
            ],
            0,
            None,
            [2],
        ]
        assert [user.email for user in status.shared_users] == [
            "owner@example.com",
            "viewer@example.com",
            "editor@example.com",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("grants", "message"),
        [
            ([], "at least one user"),
            (
                [("owner@example.com", SharePermission.OWNER)],
                "Cannot assign OWNER permission",
            ),
            (
                [("removed@example.com", SharePermission._REMOVE)],
                r"Use remove_user\(\) instead",
            ),
            (
                [
                    ("dup@example.com", SharePermission.VIEWER),
                    ("dup@example.com", SharePermission.EDITOR),
                ],
                "Duplicate email in grants",
            ),
        ],
    )
    async def test_set_users_rejects_invalid_grants(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        grants,
        message,
    ):
        """Empty, owner, remove, and duplicate grants fail before an RPC is sent.

        The duplicate case is not cosmetic: a batch repeating one grantee comes
        back **successful** from the backend while that user's permission stays
        unchanged (confirmed live), so there is no first-wins or last-wins rule to
        implement — only a request worth refusing to send.
        """
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match=message):
                await client.sharing.set_users("nb_123", grants)

        assert httpx_mock.get_requests() == []

    @pytest.mark.asyncio
    async def test_case_variant_emails_are_not_treated_as_duplicates(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """Duplicate detection is exact, and deliberately so.

        The live probe covered the *same* address twice, not case variants. RFC 5321
        makes the local part case-sensitive (only the domain is not), so folding case
        here could reject two addresses the server may treat as distinct identities —
        a client-side hard error standing in for a backend rule nobody has observed.
        This pins the narrower behaviour so a future "improvement" to `casefold()`
        has to argue with evidence.
        """
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, [[], [False], 1000]).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.set_users(
                "nb_123",
                [
                    ("Dup@example.com", SharePermission.VIEWER),
                    ("dup@example.com", SharePermission.EDITOR),
                ],
                notify=False,
            )

        assert rpc_request_params(httpx_mock.get_requests()[0])[0][0][1] == [
            ["Dup@example.com", None, SharePermission.VIEWER.value],
            ["dup@example.com", None, SharePermission.EDITOR.value],
        ]

    @pytest.mark.asyncio
    async def test_set_users_notifies_by_default(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """``notify`` defaults to True — every other test passes it explicitly.

        Without this, flipping the new method's default to False would go unnoticed:
        a brand-new method's default is not covered by the stable-API baseline.
        """
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, [[], [False], 1000]).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.set_users("nb_123", [("u@example.com", SharePermission.VIEWER)])

        assert rpc_request_params(httpx_mock.get_requests()[0])[1] == 1

    @pytest.mark.asyncio
    async def test_add_user_forwards_its_welcome_message(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """The singular wrapper must not drop the caller's welcome message.

        ``add_user`` now delegates to ``set_users``; nothing else asserts that the
        message survives the hop, so dropping it would have been silent.
        """
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, [[], [False], 1000]).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.add_user(
                "nb_123",
                "u@example.com",
                SharePermission.VIEWER,
                notify=False,
                welcome_message="Come look at this",
            )

        assert rpc_request_params(httpx_mock.get_requests()[0])[0][0][3] == [
            0,
            "Come look at this",
        ]

    @pytest.mark.asyncio
    async def test_set_users_upserts_an_existing_grantee(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """Re-sending an email that already has access replaces its permission.

        This is the naming contract: the operation is an upsert, so ``set_users``
        sends the same entry shape whether the grantee is new or existing, and
        the caller gets the changed permission back.
        """
        httpx_mock.add_response(content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode())
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.GET_SHARE_STATUS,
                [
                    [
                        ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                        ["existing@example.com", 2, [], ["Existing", "https://e"]],
                    ],
                    [False],
                    1000,
                ],
            ).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_users(
                "nb_123",
                [("existing@example.com", SharePermission.EDITOR)],
                notify=False,
            )

        assert rpc_request_params(httpx_mock.get_requests()[0])[0][0][1] == [
            ["existing@example.com", None, SharePermission.EDITOR.value]
        ]
        promoted = next(u for u in status.shared_users if u.email == "existing@example.com")
        assert promoted.permission == SharePermission.EDITOR

    @pytest.mark.asyncio
    async def test_add_user_and_update_user_send_the_same_wire_shape(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """The singular wrappers differ only in ``notify``, not in the operation.

        ``update_user`` adds an absent user and ``add_user`` updates a present one
        — both are the same upsert — so pinning their entry blocks equal is what
        stops a future change from re-splitting them into distinct operations.
        """
        for _ in range(2):
            httpx_mock.add_response(
                content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode()
            )
            httpx_mock.add_response(
                content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, [[], [False], 1000]).encode()
            )

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.add_user(
                "nb_123", "u@example.com", SharePermission.EDITOR, notify=False
            )
            await client.sharing.update_user("nb_123", "u@example.com", SharePermission.EDITOR)

        added, updated = (
            rpc_request_params(r)
            for r in httpx_mock.get_requests()
            if r.url.params["rpcids"] == RPCMethod.SHARE_NOTEBOOK.value
        )
        assert added == updated

    @pytest.mark.asyncio
    async def test_removal_keeps_its_own_message_block(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        rpc_request_params,
    ):
        """Grants and removals disagree on the message flag; both shapes are pinned.

        A grant with no welcome message sends ``[1, ""]``; a removal sends
        ``[0, ""]``. They share ``_share_params``, so without this test the next
        person to "unify" the flag would silently rewrite the removal payload.
        """
        for _ in range(2):
            httpx_mock.add_response(
                content=build_rpc_response(RPCMethod.SHARE_NOTEBOOK, []).encode()
            )
            httpx_mock.add_response(
                content=build_rpc_response(RPCMethod.GET_SHARE_STATUS, [[], [False], 1000]).encode()
            )

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.set_users(
                "nb_123", [("u@example.com", SharePermission.VIEWER)], notify=False
            )
            await client.sharing.remove_user("nb_123", "u@example.com")

        grant, removal = (
            rpc_request_params(r)
            for r in httpx_mock.get_requests()
            if r.url.params["rpcids"] == RPCMethod.SHARE_NOTEBOOK.value
        )
        assert grant[0][0][3] == [1, ""]
        assert removal[0][0][3] == [0, ""]
