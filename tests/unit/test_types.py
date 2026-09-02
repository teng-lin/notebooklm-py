"""Unit tests for types module dataclasses and parsing."""

import dataclasses
import os
import pickle
import time
import warnings
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from notebooklm.types import (
    Artifact,
    ArtifactType,
    AskResult,
    ChatGoal,
    ChatMode,
    ChatReference,
    ChatResponseLength,
    ConversationTurn,
    GenerationStatus,
    Note,
    Notebook,
    NotebookDescription,
    ReportSuggestion,
    SharePermission,
    Source,
    SourceFulltext,
    SourceType,
    UnknownTypeWarning,
    _is_valid_artifact_url,
)


class TestArtifactUrlValidation:
    """Test the canonical artifact URL validation helper."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://example.com/audio.mp3", True),
            ("http://example.com/video.mp4", True),
            ("example.com/audio.mp3", False),
            ("ftp://example.com/file.mp3", False),
            ("", False),
            (None, False),
            (123, False),
            (["https://example.com"], False),
        ],
    )
    def test_url_validation(self, value, expected):
        assert _is_valid_artifact_url(value) is expected


class TestTimestampParsing:
    def test_datetime_from_timestamp_valid_value(self):
        """Timestamp helper should preserve valid epoch-second values."""
        from notebooklm.types import _datetime_from_timestamp

        ts = 1704067200
        parsed = _datetime_from_timestamp(ts)

        assert parsed is not None
        assert parsed.timestamp() == ts

    def test_datetime_from_timestamp_is_tz_aware_utc(self):
        """Decoded datetimes are tz-aware UTC, not naive host-local time (#1519).

        The bug: a naive ``datetime.fromtimestamp(value)`` rendered the epoch in
        the host's local zone, so the public ``created_at`` mis-stated the absolute
        instant and serialized differently per box. The decoder must return a
        tz-aware value pinned to UTC and equal to the correct absolute instant.

        Red-first: against the unfixed (naive) decoder ``parsed.tzinfo`` is ``None``,
        so the ``tzinfo is not None`` assertion fails (and the offset-aware equality
        would compare unequal — a naive value never ``==`` an aware one).
        """
        from notebooklm.types import _datetime_from_timestamp

        # 1768311605 == 2026-01-13T13:40:05+00:00 (the issue's illustrative instant).
        parsed = _datetime_from_timestamp(1768311605)

        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)
        assert parsed == datetime(2026, 1, 13, 13, 40, 5, tzinfo=timezone.utc)

    @pytest.mark.parametrize("tz_name", ["UTC", "America/New_York", "Asia/Kolkata"])
    def test_datetime_from_timestamp_host_independent(self, tz_name):
        """The decoded instant is identical regardless of the host timezone (#1519).

        Exercises the original failure mode directly: under ``America/New_York`` a
        notebook created 13:40:05 UTC used to serialize as the offset-less
        ``08:40:05``. With the fix every host yields the same tz-aware UTC value.
        ``time.tzset`` only honours ``$TZ`` on POSIX, so skip elsewhere.
        """
        if not hasattr(time, "tzset"):
            pytest.skip("time.tzset is POSIX-only; cannot exercise host-TZ swap")

        from notebooklm.types import _datetime_from_timestamp

        # Swap the process-wide zone, then restore the host's real $TZ + tz table
        # in ``finally`` so the swap never leaks into later tests in this process.
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = tz_name
        time.tzset()
        try:
            parsed = _datetime_from_timestamp(1768311605)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

        assert parsed == datetime(2026, 1, 13, 13, 40, 5, tzinfo=timezone.utc)
        assert parsed.isoformat() == "2026-01-13T13:40:05+00:00"

    def test_datetime_from_timestamp_oserror(self, monkeypatch):
        """Platform-specific timestamp errors should normalize to None."""
        from unittest.mock import MagicMock

        from notebooklm._types import common as _common
        from notebooklm.types import _datetime_from_timestamp

        mock_datetime = MagicMock()
        mock_datetime.fromtimestamp.side_effect = OSError("timestamp out of range")
        monkeypatch.setattr(_common, "datetime", mock_datetime)

        parsed = _datetime_from_timestamp(1704067200)

        assert parsed is None
        # Pinned to UTC so the rendered instant is host-independent (#1519).
        mock_datetime.fromtimestamp.assert_called_once_with(1704067200, tz=timezone.utc)

    @pytest.mark.parametrize("value", ["bad", None, float("inf"), float("-inf")])
    def test_datetime_from_timestamp_invalid_value(self, value):
        """Invalid or out-of-range timestamp values should normalize to None."""
        from notebooklm.types import _datetime_from_timestamp

        assert _datetime_from_timestamp(value) is None


_PUBLIC_MOVABLE_CLASSES = [
    "AccountLimits",
    "Artifact",
    "ArtifactType",
    "AskResult",
    "ChatMode",
    "ChatReference",
    "CitedSourceSelection",
    "ClientMetricsSnapshot",
    "ConnectionLimits",
    "ConversationTurn",
    "GenerationState",
    "GenerationStatus",
    "MindMap",
    "MindMapKind",
    "Note",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "ReportSuggestion",
    "RelevantChunk",
    "RpcTelemetryEvent",
    "SharedUser",
    "ShareStatus",
    "Source",
    "SourceFulltext",
    "SourceSummary",
    "SourceType",
    "SuggestedTopic",
]


@pytest.mark.parametrize("name", _PUBLIC_MOVABLE_CLASSES)
def test_public_movable_types_keep_notebooklm_types_module(name):
    """Moved public dataclasses/enums must preserve inspection and pickle identity."""
    import notebooklm.types as public_types

    assert getattr(public_types, name).__module__ == "notebooklm.types"


def test_artifact_note_chat_and_sharing_types_are_facade_reexports():
    """T13.3 domain types live in private modules while notebooklm.types stays public."""
    import notebooklm.types as public_types
    from notebooklm._types import artifacts, chat, notes, sharing

    assert public_types.Artifact is artifacts.Artifact
    assert public_types.ArtifactType is artifacts.ArtifactType
    assert public_types.GenerationState is artifacts.GenerationState
    assert public_types.GenerationStatus is artifacts.GenerationStatus
    assert public_types.ReportSuggestion is artifacts.ReportSuggestion
    assert public_types.Note is notes.Note
    assert public_types.ConversationTurn is chat.ConversationTurn
    assert public_types.ChatReference is chat.ChatReference
    assert public_types.AskResult is chat.AskResult
    assert public_types.ChatMode is chat.ChatMode
    assert public_types.SharedUser is sharing.SharedUser
    assert public_types.ShareStatus is sharing.ShareStatus


def test_artifact_private_helper_seams_are_facade_reexports():
    """Artifact helper functions and warning state remain live notebooklm.types aliases."""
    import notebooklm.types as public_types
    from notebooklm._types import artifacts

    assert public_types._warned_artifact_types is artifacts._warned_artifact_types
    assert public_types._is_valid_artifact_url is artifacts._is_valid_artifact_url
    assert public_types._extract_artifact_url is artifacts._extract_artifact_url
    assert public_types._extract_audio_artifact_url is artifacts._extract_audio_artifact_url
    assert public_types._extract_video_artifact_url is artifacts._extract_video_artifact_url
    assert public_types._extract_infographic_artifact_url is (
        artifacts._extract_infographic_artifact_url
    )
    assert public_types._extract_slide_deck_artifact_url is (
        artifacts._extract_slide_deck_artifact_url
    )


def test_representative_public_dataclasses_pickle_round_trip():
    """Representative public dataclasses/enums keep pickle compatibility through T13 moves."""
    from notebooklm.rpc.types import ArtifactStatus, ArtifactTypeCode
    from notebooklm.types import (
        AccountLimits,
        Artifact,
        AskResult,
        ChatReference,
        CitedSourceSelection,
        ClientMetricsSnapshot,
        ConnectionLimits,
        ConversationTurn,
        GenerationState,
        GenerationStatus,
        Notebook,
        NotebookDescription,
        NotebookMetadata,
        ReportSuggestion,
        RpcTelemetryEvent,
        ShareAccess,
        SharedUser,
        SharePermission,
        ShareStatus,
        ShareViewLevel,
        Source,
        SourceFulltext,
        SourceStatus,
        SourceSummary,
        SourceType,
        SuggestedTopic,
    )

    source_summary = SourceSummary(kind=SourceType.PDF, title="doc.pdf")
    notebook = Notebook(id="nb_1", title="Notebook")
    chat_reference = ChatReference(source_id="src_1", citation_number=1, cited_text="quoted")
    shared_user = SharedUser(email="reader@example.com", permission=SharePermission.VIEWER)
    instances = [
        AccountLimits(notebook_limit=10, source_limit=50, raw_limits=("raw",), tier=2),
        Artifact(
            id="artifact_1",
            title="Audio",
            _artifact_type=ArtifactTypeCode.AUDIO.value,
            status=ArtifactStatus.COMPLETED,
            url="https://example.com/audio.mp3",
        ),
        AskResult(
            answer="Answer",
            conversation_id="conversation_1",
            turn_number=1,
            is_follow_up=False,
            references=[chat_reference],
            raw_response="raw",
        ),
        CitedSourceSelection(
            sources=[{"url": "https://example.com"}],
            cited_url_count=1,
            matched_url_source_count=1,
        ),
        ClientMetricsSnapshot(rpc_calls_started=2, rpc_calls_succeeded=1),
        ConnectionLimits(max_connections=10, max_keepalive_connections=5),
        ConversationTurn(query="Question", answer="Answer", turn_number=1),
        GenerationStatus(task_id="task_1", status="completed", url="https://example.com/file"),
        Note(id="note_1", notebook_id="nb_1", title="Note", content="Body"),
        NotebookDescription(
            summary="Summary",
            suggested_topics=[SuggestedTopic(question="Q?", prompt="Ask Q")],
        ),
        NotebookMetadata(notebook=notebook, sources=[source_summary]),
        ReportSuggestion(title="Briefing", description="Desc", prompt="Prompt"),
        RpcTelemetryEvent(method="GET_NOTEBOOK", status="success", elapsed_seconds=0.01),
        ShareStatus(
            notebook_id="nb_1",
            is_public=True,
            access=ShareAccess.ANYONE_WITH_LINK,
            view_level=ShareViewLevel.FULL_NOTEBOOK,
            shared_users=[shared_user],
            share_url="https://notebooklm.google.com/notebook/nb_1",
        ),
        Source(
            id="src_1",
            title="Source",
            url="https://example.com",
            _type_code=2,
            status=SourceStatus.READY.value,
        ),
        source_summary,
        SourceFulltext(
            source_id="src_1",
            title="Source",
            content="Full indexed text",
            _type_code=2,
            url="https://example.com",
            char_count=17,
        ),
    ]

    for instance in instances:
        assert pickle.loads(pickle.dumps(instance)) == instance

    for enum_member in [
        SourceType.PDF,
        ArtifactType.AUDIO,
        ChatMode.DEFAULT,
        GenerationState.COMPLETED,
    ]:
        assert pickle.loads(pickle.dumps(enum_member)) is enum_member


def _notebook_meta(*, user_role=1, has_sharing=False):
    """Build a ``data[5]`` metadata block in the live 16-slot shape.

    Copied from a real ``LIST_NOTEBOOKS`` response (see
    ``tests/cassettes/web/notebooks_list.yaml``): slot 0 is ``userRole``, slot 1 is
    the "has any sharing" flag, slot 5 is the last-modified instant, slot 8 the
    creation instant and slot 12 ``isPublic``.
    """
    return [
        user_role,
        has_sharing,
        True,
        None,
        None,
        [1768311605, 661578000],
        1,
        False,
        [1768174413, 819385000],
        None,
        None,
        None,
        has_sharing,
        True,
        1,
        False,
    ]


class TestNotebook:
    @pytest.mark.parametrize("data", [True, 1, 1.2])
    def test_truthy_non_list_preserves_type_error(self, data: object) -> None:
        """The decoder extraction must preserve malformed top-level behavior."""
        with pytest.raises(TypeError):
            Notebook.from_api_response(data)  # type: ignore[arg-type]

    def test_from_api_response_basic(self):
        """Test parsing basic notebook data."""
        data = ["My Notebook", [], "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.id == "nb_123"
        assert notebook.title == "My Notebook"
        assert notebook.sources_count == 0
        assert notebook.is_owner is True
        assert notebook.emoji == "📓"

    def test_from_api_response_decodes_project_metadata(self):
        data = [
            "Project Metadata",
            None,
            "nb_project",
            "🧬",
            None,
            None,
            None,
            [[2, "You are a science tutor"], [4]],
            [False],
            [True, True, False],
            None,
            [["session-1"], ["session-2"]],
        ]

        notebook = Notebook.from_api_response(data, include_chat_settings=True)

        assert notebook.emoji == "🧬"
        assert notebook.premium_features is not None
        assert notebook.premium_features.can_edit_advanced_settings is True
        assert notebook.premium_features.can_edit_guidebook_config is True
        assert notebook.premium_features.can_view_analytics is False
        assert [session.id for session in notebook.chat_sessions] == ["session-1", "session-2"]
        assert notebook.chat_settings is not None
        assert notebook.chat_settings.goal is ChatGoal.CUSTOM
        assert notebook.chat_settings.response_length is ChatResponseLength.LONGER
        assert notebook.chat_settings.custom_prompt == "You are a science tutor"

    def test_from_api_response_null_chat_config_is_default(self):
        data = ["Defaults", None, "nb_default", None, None, None, None, None]
        notebook = Notebook.from_api_response(data, include_chat_settings=True)

        assert notebook.chat_settings is not None
        assert notebook.chat_settings.goal is ChatGoal.DEFAULT
        assert notebook.chat_settings.response_length is ChatResponseLength.DEFAULT
        assert notebook.chat_settings.custom_prompt is None

    def test_list_projection_does_not_claim_default_chat_settings(self):
        list_data = ["Configured", None, "nb-1", None, None, None, None, None]
        get_data = [
            "Configured",
            None,
            "nb-1",
            None,
            None,
            None,
            None,
            [[2, "Configured persona"], [4]],
        ]

        listed = Notebook.from_api_response(list_data)
        fetched = Notebook.from_api_response(get_data, include_chat_settings=True)

        assert listed.chat_settings is None
        assert fetched.chat_settings is not None
        assert fetched.chat_settings.goal is ChatGoal.CUSTOM
        assert fetched.chat_settings.custom_prompt == "Configured persona"

    def test_from_api_response_counts_sources(self):
        """Test parsing notebook source count from embedded source entries."""
        data = ["My Notebook", [["src_1"], ["src_2"], ["src_3"]], "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.sources_count == 3

    def test_from_api_response_none_sources_count_defaults_to_zero(self):
        """Test parsing notebook source count when source entries are absent."""
        data = ["My Notebook", None, "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.sources_count == 0

    def test_from_api_response_with_timestamp(self):
        """Test parsing notebook with timestamp.

        ``created_at`` is read from ``meta[8]`` (the creation slot), so the
        creation epoch is placed there. ``meta[5]`` carries the distinct
        ``lastViewedTime`` slot.
        """
        created_ts = 1704067200  # 2024-01-01 00:00:00 UTC
        viewed_ts = 1704153600  # 2024-01-02 00:00:00 UTC
        data = [
            "Timestamped Notebook",
            [],
            "nb_456",
            "📘",
            None,
            # meta[5] = lastViewedTime slot, meta[8] = creation slot
            [None, None, None, None, None, [viewed_ts, 0], None, None, [created_ts, 0]],
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.id == "nb_456"
        assert notebook.created_at is not None
        # Check timestamp value rather than year (timezone-independent)
        assert notebook.created_at.timestamp() == created_ts
        assert notebook.last_viewed_at is not None
        assert notebook.last_viewed_at.timestamp() == viewed_ts
        # The deprecated alias mirrors it (#2126).
        assert notebook.modified_at == notebook.last_viewed_at

    def test_from_api_response_created_and_viewed_not_swapped(self):
        """``created_at`` is ``meta[8]`` and ``last_viewed_at`` is ``meta[5]``.

        Regression pin for the swapped-slots bug: the metadata block exposes the
        creation instant at ``data[5][8][0]`` (pinned across create / share /
        rename / read) and ``lastViewedTime`` at ``data[5][5][0]``. The pre-fix
        code read ``meta[5]`` for ``created_at`` and so surfaced a view time as
        the creation time.
        """
        created_ts = 1767921609  # 2026-01-09 — earlier (true creation)
        viewed_ts = 1768963937  # 2026-01-21 — later (last view)
        data = [
            "Swap Probe Notebook",
            [],
            "nb_swap",
            "📓",
            None,
            [None, None, None, None, None, [viewed_ts, 1], None, None, [created_ts, 2]],
        ]
        notebook = Notebook.from_api_response(data)

        # created_at == the meta[8] instant (NOT meta[5])
        assert notebook.created_at is not None
        assert notebook.created_at.timestamp() == created_ts
        # last_viewed_at == the meta[5] instant
        assert notebook.last_viewed_at is not None
        assert notebook.last_viewed_at.timestamp() == viewed_ts
        assert notebook.modified_at == notebook.last_viewed_at
        # The two are distinct and ordered created < viewed (sanity).
        assert notebook.created_at < notebook.last_viewed_at

    def test_from_api_response_short_meta_leaves_both_timestamps_none(self):
        """A ``meta`` block too short to carry the slots soft-degrades to None.

        ``meta[8]`` is absent (len 6) so ``created_at`` is None; ``meta[5]`` is
        also degraded here (None payload) so ``last_viewed_at`` is None too.
        """
        data = [
            "Short Meta Notebook",
            [],
            "nb_short",
            "📓",
            None,
            [None, None, None, None, None, None],  # len 6: meta[8] absent, meta[5] None
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None
        assert notebook.last_viewed_at is None
        assert notebook.modified_at is None

    def test_from_api_response_short_meta_keeps_viewed_but_not_created(self):
        """A ``meta`` block carrying ``meta[5]`` but too short for ``meta[8]``.

        Locks the length-guard policy: ``created_at`` (``meta[8]``) soft-degrades
        to None rather than falling back to ``meta[5]`` (which would re-introduce
        the swap bug), while ``last_viewed_at`` (``meta[5]``) is still populated.
        """
        viewed_ts = 1768311605
        data = [
            "View-Only Notebook",
            [],
            "nb_viewonly",
            "📓",
            None,
            [None, None, None, None, None, [viewed_ts, 0]],  # len 6: meta[5] set, meta[8] absent
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None
        assert notebook.last_viewed_at is not None
        assert notebook.last_viewed_at.timestamp() == viewed_ts
        assert notebook.modified_at == notebook.last_viewed_at

    def test_from_api_response_missing_meta_leaves_both_timestamps_none(self):
        """No metadata block at all → both timestamps None."""
        data = ["No Meta Notebook", [], "nb_nometa", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None
        assert notebook.last_viewed_at is None
        assert notebook.modified_at is None

    def test_from_api_response_strips_thought_prefix(self):
        """Test that 'thought\\n' prefix is stripped from title."""
        data = ["thought\nActual Title", [], "nb_789", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.title == "Actual Title"

    @pytest.mark.parametrize(
        ("user_role", "has_sharing", "expected_role", "expected_is_owner"),
        [
            # Owner of a private notebook — the pre-#2125 code got this right.
            (1, False, SharePermission.OWNER, True),
            # Owner who shared the notebook with a colleague. The old code read
            # the has-sharing slot and reported is_owner=False here; this is the
            # headline regression #2125 fixes.
            (1, True, SharePermission.OWNER, True),
            # Collaborator with edit rights (proto WRITER).
            (2, True, SharePermission.EDITOR, False),
            # Collaborator with read-only rights (proto READER). Observed live
            # in tests/cassettes/web/notebooks_list.yaml ("Jane Austen").
            (3, True, SharePermission.VIEWER, False),
        ],
    )
    def test_from_api_response_decodes_user_role(
        self, user_role, has_sharing, expected_role, expected_is_owner
    ):
        """``meta[0]`` (``userRole``) drives ``role`` and therefore ``is_owner``.

        The metadata block is the full 16-slot shape captured from a live
        ``LIST_NOTEBOOKS`` response, so the positional descents are exercised
        against a realistic row rather than a hand-shortened one.
        """
        data = [
            "A Notebook",
            [],
            "nb_role",
            "📓",
            None,
            _notebook_meta(user_role=user_role, has_sharing=has_sharing),
        ]

        notebook = Notebook.from_api_response(data)

        assert notebook.role is expected_role
        assert notebook.is_owner is expected_is_owner

    @pytest.mark.parametrize("raw_role", [None, 0, 4, 99, True, False, "1", []])
    def test_from_api_response_unmapped_role_degrades_to_none(self, raw_role):
        """Absent / unrecognized ``userRole`` codes report an unknown role.

        ``is_owner`` keeps its historical optimistic default so a malformed row
        soft-degrades instead of mislabelling every entry. ``True``/``False`` are
        included because ``bool`` is an ``int`` subclass — the neighbouring
        has-sharing slot's shape must not be misread as ``OWNER``.
        """
        data = [
            "A Notebook",
            [],
            "nb_role",
            "📓",
            None,
            _notebook_meta(user_role=raw_role),
        ]

        notebook = Notebook.from_api_response(data)

        assert notebook.role is None
        assert notebook.is_owner is True

    @pytest.mark.parametrize("raw_role", [0, 4, 99, True, False, "1", []])
    def test_from_api_response_unmapped_role_warns(self, caplog, raw_role):
        """A present-but-unmapped ``userRole`` is drift, so it degrades LOUDLY.

        This WARNING is the tripwire for the repo's #1 breakage class (Google
        changing the wire shape). ``True``/``False`` matter most: ``bool`` is an
        ``int`` subclass, so a slot slip onto the neighbouring has-sharing flag
        would otherwise decode as a confident ``OWNER`` (#1485 policy).
        """
        import logging

        data = ["A Notebook", [], "nb_role", "📓", None, _notebook_meta(user_role=raw_role)]
        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            notebook = Notebook.from_api_response(data)

        assert notebook.role is None
        assert any(
            r.levelno == logging.WARNING and "userRole slot unmapped" in r.message
            for r in caplog.records
        ), f"no drift WARNING for raw_role={raw_role!r}"

    def test_from_api_response_absent_role_is_silent(self, caplog):
        """A ``None`` slot / missing meta block is absence, not drift — no WARNING."""
        import logging

        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            null_slot = Notebook.from_api_response(
                ["A Notebook", [], "nb_role", "📓", None, _notebook_meta(user_role=None)]
            )
            no_meta = Notebook.from_api_response(["No Meta", [], "nb_nometa", "📓"])

        assert null_slot.role is None
        assert no_meta.role is None
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_from_api_response_ignores_has_sharing_slot(self):
        """``meta[1]`` no longer participates in the ownership decision.

        Pre-#2125 this slot *was* the ownership signal, so pinning both
        polarities against a fixed OWNER role is the regression guard.
        """
        roles = {
            has_sharing: Notebook.from_api_response(
                [
                    "A Notebook",
                    [],
                    "nb_role",
                    "📓",
                    None,
                    _notebook_meta(user_role=1, has_sharing=has_sharing),
                ]
            )
            for has_sharing in (True, False)
        }

        assert roles[True].role is roles[False].role is SharePermission.OWNER
        assert roles[True].is_owner is roles[False].is_owner is True

    def test_is_owner_is_derived_from_role_on_construction(self):
        """``Notebook(role=..., is_owner=...)`` cannot hold an inconsistent pair.

        ``is_owner`` stays a dataclass *field* (the MCP/REST serializer emits
        fields only), so the invariant is enforced in ``__post_init__`` rather
        than by turning it into a property.
        """
        assert Notebook(id="a", title="t", role=SharePermission.OWNER).is_owner is True
        assert Notebook(id="a", title="t", role=SharePermission.EDITOR).is_owner is False
        assert Notebook(id="a", title="t", role=SharePermission.VIEWER).is_owner is False
        # A contradictory explicit boolean is corrected, not preserved.
        assert (
            Notebook(id="a", title="t", role=SharePermission.VIEWER, is_owner=True).is_owner
            is False
        )

    def test_is_owner_tracks_role_reassigned_after_construction(self):
        """``Notebook`` is mutated in place elsewhere, so the invariant must survive it.

        ``_app.notebooks._backfill_created_timestamps`` already assigns to a
        live ``Notebook``, so a construction-only hook would let ``is_owner`` go
        stale the moment anyone assigned ``role``.
        """
        notebook = Notebook(id="a", title="t", role=SharePermission.OWNER)
        assert notebook.is_owner is True

        notebook.role = SharePermission.VIEWER
        assert notebook.is_owner is False

        notebook.role = SharePermission.OWNER
        assert notebook.is_owner is True

        # Clearing the role leaves the last derived value rather than guessing.
        notebook.role = None
        assert notebook.is_owner is True

    def test_is_owner_is_untouched_when_role_is_unknown(self):
        """With no role stated, the caller's explicit ``is_owner`` still wins."""
        assert Notebook(id="a", title="t").is_owner is True
        assert Notebook(id="a", title="t", is_owner=False).is_owner is False

    def test_from_api_response_missing_meta_block_reports_unknown_role(self):
        """A row with no metadata block states no role, and stays is_owner=True."""
        notebook = Notebook.from_api_response(["No Meta", [], "nb_nometa", "📓"])

        assert notebook.role is None
        assert notebook.is_owner is True

    def test_from_api_response_empty_data(self):
        """Test parsing with minimal data."""
        data = []
        notebook = Notebook.from_api_response(data)

        assert notebook.id == ""
        assert notebook.title == ""
        assert notebook.is_owner is True

    def test_from_api_response_invalid_timestamp(self):
        """Test parsing with invalid timestamp data.

        ``created_at`` reads ``meta[8]`` and ``last_viewed_at`` reads
        ``meta[5]``; both invalid payloads soft-degrade to ``None``.
        """
        data = [
            "Notebook",
            [],
            "nb_123",
            "📓",
            None,
            [None, None, None, None, None, ["invalid", 0], None, None, ["invalid", 0]],
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None
        assert notebook.last_viewed_at is None
        assert notebook.modified_at is None

    def test_from_api_response_out_of_range_timestamp(self):
        """Platform timestamp range errors should not escape notebook parsing."""
        data = [
            "Notebook",
            [],
            "nb_123",
            "📓",
            None,
            [None, None, None, None, None, [1704067200, 0], None, None, [1704067200, 0]],
        ]

        # Both the creation slot (meta[8]) and lastViewedTime slot (meta[5]) overflow.
        data[5][8][0] = float("inf")
        data[5][5][0] = float("inf")
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None
        assert notebook.last_viewed_at is None
        assert notebook.modified_at is None

    def test_from_api_response_non_string_title(self):
        """Test parsing when title is not a string."""
        data = [123, [], "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.title == ""

    def test_from_api_response_malformed_id_slot_warns(self, caplog):
        """A present-but-non-str id slot fabricates ``""`` LOUDLY (#1485).

        The degrade itself is kept (a raising row parser would abort
        whole-list parsing), but the fabrication now leaves a WARNING with a
        bounded payload preview instead of being silent.
        """
        import logging

        data = ["My Notebook", [], 12345, "📓"]
        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            notebook = Notebook.from_api_response(data)

        assert notebook.id == ""
        assert any(
            r.levelno == logging.WARNING and "id slot malformed" in r.message
            for r in caplog.records
        )

    def test_from_api_response_null_id_slot_is_silent(self, caplog):
        """A ``None`` id slot is absence, not drift — silent ``""`` degrade."""
        import logging

        data = ["My Notebook", [], None, "📓"]
        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            notebook = Notebook.from_api_response(data)

        assert notebook.id == ""
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_from_api_response_short_row_is_silent(self, caplog):
        """Rows too short to carry the id slot keep the silent degrade."""
        import logging

        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            notebook = Notebook.from_api_response(["Title only"])

        assert notebook.id == ""
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestSource:
    def test_from_api_response_simple_format(self):
        """Test parsing simple flat format."""
        data = ["src_123", "Source Title"]
        source = Source.from_api_response(data)

        assert source.id == "src_123"
        assert source.title == "Source Title"
        assert source.kind == SourceType.UNKNOWN

    def test_from_api_response_nested_format(self):
        """Test parsing medium nested format."""
        data = [
            [
                ["src_456"],
                "Nested Source",
                [None, None, None, None, None, None, None, ["https://example.com"]],
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_456"
        assert source.title == "Nested Source"
        assert source.url == "https://example.com"

    def test_from_api_response_nested_format_with_timestamp(self):
        """Source.from_api_response should preserve creation timestamps when present."""
        ts = 1704067200
        data = [
            [
                ["src_ts"],
                "Timestamped Source",
                [None, None, [ts, 0], None, 5, None, None, ["https://example.com"]],
            ]
        ]
        source = Source.from_api_response(data)

        assert source.created_at is not None
        assert source.created_at.timestamp() == ts

    def test_from_api_response_nested_format_out_of_range_timestamp(self):
        """Source timestamp range errors should produce None rather than raising."""
        data = [
            [
                ["src_ts"],
                "Timestamped Source",
                [None, None, [1704067200, 0], None, 5, None, None, ["https://example.com"]],
            ]
        ]

        data[0][2][2][0] = float("inf")
        source = Source.from_api_response(data)

        assert source.created_at is None

    def test_from_api_response_deeply_nested(self):
        """Test parsing deeply nested format."""
        data = [
            [
                [
                    ["src_789"],
                    "Deep Source",
                    [None, None, None, None, None, None, None, ["https://deep.example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_789"
        assert source.title == "Deep Source"
        assert source.url == "https://deep.example.com"

    def test_from_api_response_youtube_source(self):
        """Test that YouTube sources are parsed with type code 9."""
        data = [
            [
                [
                    ["src_yt"],
                    "YouTube Video",
                    [None, None, None, None, 9, None, None, ["https://youtube.com/watch?v=abc"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.kind == SourceType.YOUTUBE
        assert source.kind == "youtube"  # str enum comparison

    def test_from_api_response_deeply_nested_youtube_url_at_index_5(self):
        """Regression test for issue #265: deeply-nested YouTube payloads store
        the URL at entry[2][5][0]; entry[2][7] is None. from_api_response must
        read the URL from index 5 when index 7 is unpopulated.
        """
        data = [
            [
                [
                    ["src_yt_deep"],
                    "YouTube Video",
                    [
                        None,
                        None,
                        None,
                        None,
                        9,  # YOUTUBE type code
                        [
                            "https://www.youtube.com/watch?v=dcWU-qD8ISQ",
                            "dcWU-qD8ISQ",
                            "john newquist",
                        ],
                        None,
                        None,  # [7] is None for YouTube sources
                    ],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_yt_deep"
        assert source.kind == SourceType.YOUTUBE
        assert source.url == "https://www.youtube.com/watch?v=dcWU-qD8ISQ"

    def test_from_api_response_medium_nested_youtube_url_at_index_5(self):
        """Regression test for issue #265: medium-nested YouTube payloads also
        store the URL at entry[2][5][0] with entry[2][7] = None.
        """
        data = [
            [
                ["src_yt_mid"],
                "YouTube Video",
                [
                    None,
                    None,
                    None,
                    None,
                    9,
                    [
                        "https://www.youtube.com/watch?v=dcWU-qD8ISQ",
                        "dcWU-qD8ISQ",
                        "john newquist",
                    ],
                    None,
                    None,
                ],
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_yt_mid"
        assert source.url == "https://www.youtube.com/watch?v=dcWU-qD8ISQ"
        assert source.kind == SourceType.YOUTUBE

    def test_from_api_response_index_5_empty_list_does_not_crash(self):
        """entry[2][5] == [] must not produce a URL and must not raise."""
        data = [
            [
                [
                    ["src_empty5"],
                    "Weird Source",
                    [None, None, None, None, 9, [], None, None],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_empty5"
        assert source.url is None

    def test_from_api_response_index_5_non_string_first_element(self):
        """entry[2][5][0] that isn't a string must not be used as a URL."""
        data = [
            [
                [
                    ["src_non_str"],
                    "Weird Source",
                    [None, None, None, None, 9, [123, "xyz", "chan"], None, None],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_non_str"
        assert source.url is None

    def test_from_api_response_index_7_still_wins_over_5(self):
        """When both [7] and [5] are populated, [7] takes precedence (matches
        list() behaviour in _sources.py).
        """
        data = [
            [
                [
                    ["src_both"],
                    "Hybrid Source",
                    [
                        None,
                        None,
                        None,
                        None,
                        5,
                        ["https://shouldnt.win/5"],
                        None,
                        ["https://should.win/7"],
                    ],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.url == "https://should.win/7"

    def test_from_api_response_web_page_source(self):
        """Test that web page sources are parsed with type code 5."""
        data = [
            [
                [
                    ["src_web"],
                    "Web Article",
                    [None, None, None, None, 5, None, None, ["https://example.com/article"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.kind == SourceType.WEB_PAGE
        assert source.kind == "web_page"  # str enum comparison

    @pytest.mark.parametrize(
        "type_code,expected_kind",
        [
            (1, SourceType.GOOGLE_DOCS),
            (2, SourceType.GOOGLE_SLIDES),
            (3, SourceType.PDF),
            (4, SourceType.PASTED_TEXT),
            (5, SourceType.WEB_PAGE),
            (6, SourceType.POWERPOINT),
            (8, SourceType.MARKDOWN),
            (9, SourceType.YOUTUBE),
            (10, SourceType.MEDIA),
            (11, SourceType.DOCX),
            (13, SourceType.IMAGE),
            (7, SourceType.GOOGLE_SPREADSHEET),
            (14, SourceType.GOOGLE_DRIVE),
            (16, SourceType.CSV),
            (17, SourceType.EPUB),
        ],
    )
    def test_from_api_response_source_type_codes(self, type_code, expected_kind):
        """Test that source type codes are correctly mapped to SourceType enum."""
        data = [
            [
                [
                    ["src_test"],
                    "Test Source",
                    [None, None, None, None, type_code, None, None, ["https://example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)
        assert source.kind == expected_kind
        # Also verify str comparison works
        assert source.kind == expected_kind.value

    def test_pptx_row_decodes_without_unknown_type_warning(self):
        """A constructed row matching #2137 decodes to POWERPOINT, no warning (#2137).

        Constructed, not captured: the slots come from the field values #2137
        reports (``type_code=6``; the PowerPoint MIME at ``metadata[19]``). The
        MIME is included because the wire carries it and it must NOT re-route
        the code the way the overloaded ``14`` does.

        ``UnknownTypeWarning`` is emitted on ``.kind`` access, not at
        construction, so the property is evaluated INSIDE the filter — and the
        warn-once set is cleared first, otherwise a stale entry would suppress
        the warning and hide a regression.
        """
        from notebooklm.types import _warned_source_types

        pptx_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        metadata: list = [None] * 20
        metadata[4] = 6
        metadata[19] = pptx_mime
        data = [[[["src_pptx"], "Deck.pptx", metadata]]]

        _warned_source_types.discard(6)
        with warnings.catch_warnings():
            warnings.simplefilter("error", UnknownTypeWarning)
            kind = Source.from_api_response(data).kind

        assert kind is SourceType.POWERPOINT

    def test_from_api_response_empty_data_raises(self):
        """Test that empty data raises ValueError."""
        with pytest.raises(ValueError, match="Invalid source data"):
            Source.from_api_response([])

    def test_from_api_response_none_raises(self):
        """Test that None raises ValueError."""
        with pytest.raises(ValueError, match="Invalid source data"):
            Source.from_api_response(None)

    def test_from_api_response_carries_status(self):
        """``from_api_response`` decodes ``status`` from the row's status block.

        Previously the classmethod never set ``status``, so it silently
        fell back to ``SourceStatus.READY`` even when the wire carried a
        PROCESSING/ERROR status block. After unifying on the single
        ``SourceRow``-based construction it now reads the same status the
        listing path does.
        """
        from notebooklm.rpc.types import SourceStatus

        data = [
            [
                ["src_proc"],
                "Processing Source",
                [None, None, [1704067200, 0], None, 5, None, None, ["https://example.com"]],
                [None, SourceStatus.PROCESSING],
            ]
        ]
        source = Source.from_api_response(data)

        assert source.status == SourceStatus.PROCESSING
        assert source.is_processing is True
        assert source.is_ready is False

    def test_from_api_response_deeply_nested_carries_status(self):
        """The deeply-nested dispatch path also decodes the status block.

        ``from_unknown_shape`` unwraps the extra outer list and funnels
        the entry through the same ``from_row`` construction, so the
        decoded status must survive on the deeply-nested shape too.
        """
        from notebooklm.rpc.types import SourceStatus

        entry = [
            ["src_deep_err"],
            "Deep Errored Source",
            [None, None, None, None, 3, None, None, ["https://example.com"]],
            [None, SourceStatus.ERROR],
        ]
        source = Source.from_api_response([[entry]])

        assert source.id == "src_deep_err"
        assert source.status == SourceStatus.ERROR
        assert source.is_error is True

    def test_from_api_response_status_is_unknown_without_block(self):
        """A row without a status block must not assert that the source is ready."""
        from notebooklm.rpc.types import SourceStatus

        # Medium-nested entry with no status block at index 3.
        data = [[["src_no_status"], "No Status", [None, None, None, None, 5]]]
        source = Source.from_api_response(data)

        assert source.status == SourceStatus.UNKNOWN
        assert source.is_ready is False

        # Flat shapes without a status block fail closed too.
        flat = Source.from_api_response(["src_flat", "Flat"])
        assert flat.status == SourceStatus.UNKNOWN
        assert flat.is_ready is False

    def test_from_api_response_matches_listing_path(self):
        """``from_api_response`` and the ``GET_NOTEBOOK`` listing path produce
        identical ``Source`` instances from the same entry — the two parsers
        are now a single source of truth (issue #1205, part 1/5).
        """
        from notebooklm._web.rows.sources import SourceRow
        from notebooklm.rpc import RPCMethod
        from notebooklm.rpc.types import SourceStatus

        # An entry exactly as ``SourceLister._parse_source`` receives it.
        entry = [
            ["src_match"],
            "Matching Source",
            [None, 11, [1704067200, 0], None, 5, None, None, ["https://example.com"]],
            [None, SourceStatus.PROCESSING],
        ]

        # Listing path: ``SourceLister._parse_source`` wraps the entry with
        # ``SourceRow.from_entry`` and funnels it through ``Source.from_row``.
        listing_source = Source.from_row(
            SourceRow.from_entry(entry, method_id=RPCMethod.GET_NOTEBOOK.value)
        )

        # Public classmethod path: the same entry wrapped in the medium-
        # nested envelope that ``ADD_SOURCE``/rename responses carry.
        api_source = Source.from_api_response([entry])

        assert api_source == listing_source
        assert api_source.status == listing_source.status == SourceStatus.PROCESSING
        assert api_source.url == listing_source.url == "https://example.com"
        assert api_source.created_at == listing_source.created_at
        # type code lives at metadata[4] (== 5 → WEB_PAGE) for both paths.
        assert api_source.kind == listing_source.kind == SourceType.WEB_PAGE

    def test_pdf_url_title_derives_basename(self):
        """A direct-PDF URL used as the title falls back to the path basename.

        Regression for #1850: Google leaves the raw request URL in the title
        slot for a link that points straight at a ``.pdf`` (HTML pages get an
        extracted ``<title>``). ``from_row`` derives a display title from the
        URL path basename while leaving ``url`` untouched.
        """
        url = "https://example.com/papers/SomePaper.pdf"
        # Medium-nested entry, type_code 3 (PDF), title == the raw URL.
        entry = [["src_pdf"], url, [None, None, None, None, 3, None, None, [url]]]
        source = Source.from_api_response([entry])

        assert source.title == "SomePaper"
        assert source.url == url
        assert source.kind == SourceType.PDF

    def test_pdf_url_title_derives_on_both_funnels(self):
        """The fallback fires identically on the add and list construction paths."""
        from notebooklm._web.rows.sources import SourceRow
        from notebooklm.rpc import RPCMethod

        url = "https://example.com/reports/Q3%20Report.pdf"
        entry = [["src_pdf2"], url, [None, None, None, None, 3, None, None, [url]]]

        listing_source = Source.from_row(
            SourceRow.from_entry(entry, method_id=RPCMethod.GET_NOTEBOOK.value)
        )
        api_source = Source.from_api_response([entry])

        assert api_source == listing_source
        assert api_source.title == listing_source.title == "Q3 Report"

    def test_pdf_real_title_untouched(self):
        """A PDF whose title is a real string (not a URL) is left alone."""
        entry = [
            ["src_pdf3"],
            "Quarterly Earnings",
            [None, None, None, None, 3, None, None, ["https://example.com/q.pdf"]],
        ]
        source = Source.from_api_response([entry])
        assert source.title == "Quarterly Earnings"

    def test_non_pdf_url_title_untouched(self):
        """The fallback is PDF-only: a web_page whose title is a URL is untouched."""
        url = "https://example.com/page.pdf"
        # type_code 5 == WEB_PAGE, even though the title looks like a .pdf URL.
        entry = [["src_web"], url, [None, None, None, None, 5, None, None, [url]]]
        source = Source.from_api_response([entry])
        assert source.title == url
        assert source.kind == SourceType.WEB_PAGE

    def test_drive_pdf_title_untouched(self):
        """A Drive-hosted PDF (type_code 14 → PDF by MIME) keeps its filename title.

        Drive sources carry no URL and a filename title, so the URL-shape gate
        in the fallback never fires — the #1832 disambiguation and the #1850
        fallback do not collide.
        """
        metadata = [None] * 20
        metadata[4] = 14  # ambiguous native-Sheet / Drive-binary code
        metadata[19] = "application/pdf"  # MIME disambiguates 14 → PDF
        entry = [["src_drive"], "MyReport.pdf", metadata]
        source = Source.from_api_response([entry])

        assert source.kind == SourceType.PDF
        assert source.title == "MyReport.pdf"

    def test_pdf_url_title_is_idempotent(self):
        """Re-parsing an already-derived title (not the source URL) is a no-op."""
        entry = [
            ["src_pdf4"],
            "SomePaper",
            [None, None, None, None, 3, None, None, ["https://example.com/SomePaper.pdf"]],
        ]
        source = Source.from_api_response([entry])
        assert source.title == "SomePaper"

    def test_pdf_explicit_url_shaped_title_preserved(self):
        """A PDF title that is a URL but NOT the source URL is left intact.

        Only the server degradation (title == the source ``url``) is corrected;
        an explicit title set via rename/upload that merely looks like a ``.pdf``
        URL must survive (#1850 review — codex).
        """
        entry = [
            ["src_renamed"],
            "https://example.com/LegalName.pdf",  # deliberate title, a URL string
            [
                None,
                None,
                None,
                None,
                3,
                None,
                None,
                ["https://example.com/actual-source.pdf"],  # different source url
            ],
        ]
        source = Source.from_api_response([entry])
        assert source.title == "https://example.com/LegalName.pdf"

    def test_unknown_type_code_does_not_warn_at_construction(self):
        """Parsing an unknown-typed source must not emit ``UnknownTypeWarning``.

        The PDF title fallback uses a plain type-code lookup (not
        ``_safe_source_type``), so warnings-as-error environments can still
        ``list()``/``get()`` an unknown source; the warning stays at ``.kind``
        access (#1850 review — codex).
        """
        import warnings

        from notebooklm._types.common import UnknownTypeWarning

        # type_code 999 is unmapped, and a title is present so the fallback runs.
        entry = [
            ["src_unknown"],
            "Some Title",
            [None, None, None, None, 999, None, None, ["https://example.com/x"]],
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("error", UnknownTypeWarning)
            source = Source.from_api_response([entry])  # must not raise
        assert source.title == "Some Title"
        # ``.kind`` remains the documented warning point.
        with pytest.warns(UnknownTypeWarning):
            _ = source.kind

    def test_from_api_response_forwards_method_id_to_row(self):
        """``method_id`` threads through to the constructed ``SourceRow`` so
        drift diagnostics name the originating RPC (issue #1242).

        The ADD_SOURCE / rename construction paths pass the real method id;
        without forwarding it the row defaults to ``GET_NOTEBOOK`` and any
        ``safe_index`` drift log is mis-tagged.
        """
        from notebooklm._web.rows.sources import SourceRow
        from notebooklm.rpc import RPCMethod

        captured: dict[str, str | None] = {}
        real_from_unknown_shape = SourceRow.from_unknown_shape

        def _spy(data, *, method_id=None):
            captured["method_id"] = method_id
            return real_from_unknown_shape(data, method_id=method_id)

        entry = [["src_add"], "Added Source", [None, 5, [1704067200, 0]]]
        with patch.object(SourceRow, "from_unknown_shape", staticmethod(_spy)):
            Source.from_api_response([entry], method_id=RPCMethod.ADD_SOURCE.value)

        assert captured["method_id"] == RPCMethod.ADD_SOURCE.value

    def test_from_api_response_default_method_id_is_get_notebook(self):
        """Without an explicit ``method_id`` the row falls back to the
        historical ``GET_NOTEBOOK`` default — preserving prior behavior for
        callers that do not pass it (issue #1242 backward-compat)."""
        from notebooklm._web.rows.sources import SourceRow
        from notebooklm.rpc import RPCMethod

        entry = [["src_default"], "Default", [None, 5, [1704067200, 0]]]
        row = SourceRow.from_unknown_shape([entry])

        assert row.method_id == RPCMethod.GET_NOTEBOOK.value

    def test_from_api_response_drift_tags_real_method_id(self, monkeypatch):
        """A ``safe_index`` drift on an ADD_SOURCE-built row surfaces an
        ``UnknownRPCMethodError`` tagged with ADD_SOURCE, not the default
        ``GET_NOTEBOOK`` (issue #1242).

        ``SourceRow.created_at_raw`` is the adapter's only ``safe_index``
        call site; force it to drift so we can assert the tagged method id
        flows from ``from_api_response``'s ``method_id`` argument.
        """
        from notebooklm._web.rows import sources as web_rows_sources
        from notebooklm.exceptions import UnknownRPCMethodError
        from notebooklm.rpc import RPCMethod

        def _drift(data, *path, method_id, source):
            raise UnknownRPCMethodError(
                "forced drift",
                method_id=method_id,
                path=tuple(path),
                source=source,
            )

        monkeypatch.setattr(web_rows_sources, "safe_index", _drift)

        # metadata[2] is a non-empty list so created_at_raw reaches safe_index.
        entry = [["src_drift"], "Drift", [None, 5, [1704067200, 0]]]
        row = web_rows_sources.SourceRow.from_unknown_shape(
            [entry], method_id=RPCMethod.ADD_SOURCE.value
        )
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            _ = row.created_at_raw

        assert exc_info.value.method_id == RPCMethod.ADD_SOURCE.value

    def test_from_api_response_accepts_unused_notebook_id(self):
        """``notebook_id`` is retained for call-site symmetry / forward-compat
        but does not influence the parsed source (issue #1241).

        It is kept (not dropped) because ``Source.from_api_response`` is
        tracked public surface; ``scripts/audit_public_api_compat.py`` flags
        removing the parameter as a backward-incompatible signature change.
        """
        data = [[["src_nb"], "With Notebook Id", [None, 5, [1704067200, 0]]]]

        without_id = Source.from_api_response(data)
        with_id = Source.from_api_response(data, "nb_ignored")
        with_keyword = Source.from_api_response(data, notebook_id="nb_ignored")

        assert without_id == with_id == with_keyword


class TestSourceTypeCompatMapping:
    """Tests for the _SOURCE_TYPE_COMPAT_MAP backward-compatible mapping."""

    def test_epub_maps_to_text_file(self):
        """Test that EPUB maps to 'text_file' in the compat mapping."""
        from notebooklm.types import _SOURCE_TYPE_COMPAT_MAP

        assert SourceType.EPUB in _SOURCE_TYPE_COMPAT_MAP
        assert _SOURCE_TYPE_COMPAT_MAP[SourceType.EPUB] == "text_file"

    def test_powerpoint_maps_to_text_file(self):
        """POWERPOINT uses the legacy ``text_file`` compatibility label (#2137)."""
        from notebooklm.types import _SOURCE_TYPE_COMPAT_MAP

        assert _SOURCE_TYPE_COMPAT_MAP[SourceType.POWERPOINT] == "text_file"


class TestSourceKindProperty:
    """Tests for the Source.kind property."""

    def test_kind_returns_str_enum(self):
        """Test that kind returns a SourceType str enum."""
        source = Source(id="x", _type_code=3)  # PDF
        assert source.kind == SourceType.PDF
        assert isinstance(source.kind, SourceType)
        assert isinstance(source.kind, str)

    def test_kind_str_comparison(self):
        """Test that kind can be compared with strings."""
        source = Source(id="x", _type_code=5)  # WEB_PAGE
        assert source.kind == "web_page"
        assert source.kind.value == "web_page"
        assert f"Type: {source.kind.value}" == "Type: web_page"

    def test_kind_unknown_for_none_type_code(self):
        """Test that kind returns UNKNOWN for None type code."""
        source = Source(id="x", _type_code=None)
        assert source.kind == SourceType.UNKNOWN

    def test_kind_unknown_for_wire_unknown_type_code_without_warning(self):
        """Backend enum value 0 is UNKNOWN/unset, not schema drift (#2138)."""
        source = Source(id="x", _type_code=0)

        with warnings.catch_warnings():
            warnings.simplefilter("error", UnknownTypeWarning)
            assert source.kind is SourceType.UNKNOWN

    def test_kind_unknown_for_unrecognized_type_code(self):
        """Test that kind returns UNKNOWN for unrecognized type codes."""
        # Clear the warned set to ensure we get the warning
        from notebooklm.types import _warned_source_types

        _warned_source_types.clear()

        source = Source(id="x", _type_code=999)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = source.kind
            assert result == SourceType.UNKNOWN
            assert len(w) == 1
            assert issubclass(w[0].category, UnknownTypeWarning)
            assert "999" in str(w[0].message)

    def test_kind_warning_deduplication(self):
        """Test that warnings for unknown types are deduplicated."""
        from notebooklm.types import _warned_source_types

        _warned_source_types.clear()

        source1 = Source(id="x", _type_code=888)
        source2 = Source(id="y", _type_code=888)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = source1.kind
            _ = source2.kind
            # Only one warning should be emitted for type code 888
            assert len([x for x in w if "888" in str(x.message)]) == 1


class TestArtifact:
    def test_from_api_response_basic(self):
        """Test parsing basic artifact data."""
        data = ["art_123", "Audio Overview", 1, None, 3]
        artifact = Artifact.from_api_response(data)

        assert artifact.id == "art_123"
        assert artifact.title == "Audio Overview"
        assert artifact.kind == ArtifactType.AUDIO
        assert artifact.status == 3

    def test_from_api_response_with_timestamp(self):
        """Test parsing artifact with timestamp."""
        ts = 1704067200
        data = [
            "art_123",
            "Audio",
            1,
            None,
            3,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [ts],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.created_at is not None
        assert artifact.created_at.timestamp() == ts

    def test_from_api_response_out_of_range_timestamp(self):
        """Artifact timestamp range errors should produce None rather than raising."""
        data = [
            "art_123",
            "Audio",
            1,
            None,
            3,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [float("inf")],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.created_at is None

    def test_from_mind_map_out_of_range_timestamp(self):
        """Mind map timestamp range errors should produce None rather than raising."""
        data = [
            "mind_map_123",
            [
                "mind_map_123",
                "{}",
                [1, "user_id", [float("inf"), 0]],
                None,
                "Mind Map",
            ],
        ]
        artifact = Artifact.from_mind_map(data)

        assert artifact is not None
        assert artifact.created_at is None

    def test_from_mind_map_deleted_tombstone_is_silent_none(self, caplog):
        """The recognised ``[id, None, 2]`` tombstone filters silently."""
        import logging

        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            assert Artifact.from_mind_map(["mm_gone", None, 2]) is None

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_from_mind_map_unrecognized_tombstone_warns_and_stays_live(self, caplog):
        """A null content slot WITHOUT the soft-delete sentinel WARNS (#1485).

        This is the deleted-map-leaking-as-live bug class: were Google to
        rotate the sentinel value, every deleted mind map would flow through
        as a live artifact. The historical treat-as-live fallthrough is kept
        (conservative), but it is no longer silent.
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="notebooklm"):
            artifact = Artifact.from_mind_map(["mm_drift", None, 7])

        assert artifact is not None
        assert artifact.id == "mm_drift"
        assert artifact.title == ""
        assert any(
            r.levelno == logging.WARNING and "soft-delete sentinel" in r.message
            for r in caplog.records
        )

    def test_from_api_response_audio_url(self):
        """Completed audio artifacts expose their download URL."""
        data = [
            "art_audio",
            "Audio",
            1,
            None,
            3,
            None,
            [None, None, None, None, None, [["https://audio.example/file.mp4", None, "audio/mp4"]]],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://audio.example/file.mp4"

    def test_from_api_response_video_url_prefers_mp4_quality(self):
        """Video artifacts expose the preferred MP4 download URL."""
        data = [
            "art_video",
            "Video",
            3,
            None,
            3,
            None,
            None,
            None,
            [
                [
                    ["https://video.example/low.webm", 1, "video/webm"],
                    ["https://video.example/high.mp4", 4, "video/mp4"],
                ]
            ],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://video.example/high.mp4"

    def test_from_api_response_video_url_returns_last_mp4_when_no_quality_4(self):
        """When no quality-4 MP4 is present, the last MP4 wins (documents the
        implicit ordering used by both the extractor and download_video)."""
        data = [
            "art_video",
            "Video",
            3,
            None,
            3,
            None,
            None,
            None,
            [
                [
                    ["https://video.example/first.mp4", 2, "video/mp4"],
                    ["https://video.example/middle.webm", 1, "video/webm"],
                    ["https://video.example/last.mp4", 3, "video/mp4"],
                ]
            ],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://video.example/last.mp4"

    def test_from_api_response_video_url_falls_back_to_first_non_mp4(self):
        """With no MP4 in any variant, the first valid URL is returned."""
        data = [
            "art_video",
            "Video",
            3,
            None,
            3,
            None,
            None,
            None,
            [
                [
                    ["https://video.example/a.webm", 1, "video/webm"],
                    ["https://video.example/b.webm", 2, "video/webm"],
                ]
            ],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://video.example/a.webm"

    def test_from_api_response_audio_url_finds_mp4_at_non_zero_position(self):
        """Audio extractor must find an audio/mp4 entry even when it is not the
        first item in the media list — regression against the legacy
        first-item-only check."""
        data = [
            "art_audio",
            "Audio",
            1,
            None,
            3,
            None,
            [
                None,
                None,
                None,
                None,
                None,
                [
                    ["https://audio.example/preview.bin", None, "application/octet-stream"],
                    ["https://audio.example/file.mp4", None, "audio/mp4"],
                ],
            ],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://audio.example/file.mp4"

    def test_from_api_response_audio_url_falls_back_to_first_url_when_no_mp4(self):
        """If no audio/mp4 entry exists, the first valid URL is returned."""
        data = [
            "art_audio",
            "Audio",
            1,
            None,
            3,
            None,
            [
                None,
                None,
                None,
                None,
                None,
                [
                    ["https://audio.example/a.ogg", None, "audio/ogg"],
                    ["https://audio.example/b.wav", None, "audio/wav"],
                ],
            ],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://audio.example/a.ogg"

    def test_from_api_response_infographic_url(self):
        """Infographic artifacts expose their image URL."""
        data = [
            "art_info",
            "Infographic",
            7,
            None,
            3,
            [None, None, [["ignored", ["https://image.example/info.png"]]]],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://image.example/info.png"

    def test_from_api_response_slide_deck_url(self):
        """Slide deck artifacts expose the PDF URL."""
        data = (
            ["art_slides", "Slides", 8, None, 3]
            + [None] * 11
            + [[None, None, None, "https://slides.example/deck.pdf"]]
        )
        artifact = Artifact.from_api_response(data)

        assert artifact.url == "https://slides.example/deck.pdf"

    def test_from_api_response_with_variant(self):
        """Test parsing artifact with variant code (quiz/flashcards)."""
        data = ["art_quiz", "Quiz", 4, None, 3, None, None, None, None, [None, [2]]]
        artifact = Artifact.from_api_response(data)

        assert artifact.kind == ArtifactType.QUIZ
        assert artifact.is_quiz is True
        assert artifact.is_flashcards is False

    def test_from_api_response_flashcards_variant(self):
        """Test parsing flashcards artifact."""
        data = ["art_fc", "Flashcards", 4, None, 3, None, None, None, None, [None, [1]]]
        artifact = Artifact.from_api_response(data)

        assert artifact.kind == ArtifactType.FLASHCARDS
        assert artifact.is_flashcards is True
        assert artifact.is_quiz is False

    def test_is_completed_property(self):
        """Test is_completed property."""
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])
        processing = Artifact.from_api_response(["id", "title", 1, None, 2])

        assert completed.is_completed is True
        assert processing.is_completed is False

    def test_is_processing_property(self):
        """is_processing is code 2 (ARTIFACT_STATUS_PROCESSING) — #2127."""
        processing = Artifact.from_api_response(["id", "title", 1, None, 2])
        pending = Artifact.from_api_response(["id", "title", 1, None, 1])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])

        assert processing.is_processing is True
        assert pending.is_processing is False
        assert completed.is_processing is False

    def test_is_pending_property(self):
        """is_pending is code 1 (ARTIFACT_STATUS_INITIALIZED) — #2127."""
        pending = Artifact.from_api_response(["id", "title", 1, None, 1])
        processing = Artifact.from_api_response(["id", "title", 1, None, 2])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])

        assert pending.is_pending is True
        assert processing.is_pending is False
        assert completed.is_pending is False

    def test_predicate_table_for_every_status_code(self):
        """Every code's full ``is_*`` answer set, pinned as a table (#2127).

        A bare "the two are mutually exclusive" check would survive the very
        transposition this issue fixed — swapping 1 and 2 keeps them exclusive.
        Pinning the whole tuple per code makes the table transposition-sensitive
        instead: before the fix, code 1 answered ``is_processing`` and code 2
        answered ``is_pending``.
        """
        # code -> (is_pending, is_processing, is_completed, is_failed)
        expected = {
            0: (False, False, False, False),  # UNKNOWN
            1: (True, False, False, False),  # INITIALIZED -> queued
            2: (False, True, False, False),  # PROCESSING -> generating
            3: (False, False, True, False),  # READY
            4: (False, False, False, True),  # FAILED
            5: (False, False, False, False),  # SUGGESTED
            6: (False, False, False, False),  # PENDING_REVIEW
        }
        for code, answers in expected.items():
            artifact = Artifact.from_api_response(["id", "title", 1, None, code])
            actual = (
                artifact.is_pending,
                artifact.is_processing,
                artifact.is_completed,
                artifact.is_failed,
            )
            assert actual == answers, f"code {code}"

    def test_is_failed_property(self):
        """Test is_failed property for status=4 (generation failed)."""
        failed = Artifact.from_api_response(["id", "title", 1, None, 4])
        processing = Artifact.from_api_response(["id", "title", 1, None, 2])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])

        assert failed.is_failed is True
        assert processing.is_failed is False
        assert completed.is_failed is False

    def test_status_str_property(self):
        """status_str covers every backend ArtifactStatus code (#2127)."""
        expected = {
            0: "unknown",
            1: "pending",
            2: "in_progress",
            3: "completed",
            4: "failed",
            5: "suggested",
            6: "pending_review",
            99: "unknown",  # unrecognized codes fail closed
        }
        for code, status_str in expected.items():
            artifact = Artifact.from_api_response(["id", "title", 1, None, code])
            assert artifact.status_str == status_str, f"code {code}"

    def test_report_subtype_briefing_doc(self):
        """Test report_subtype for briefing doc."""
        artifact = Artifact.from_api_response(["id", "Briefing Doc: Topic", 2, None, 3])

        assert artifact.report_subtype == "briefing_doc"

    def test_report_subtype_study_guide(self):
        """Test report_subtype for study guide."""
        artifact = Artifact.from_api_response(["id", "Study Guide: Topic", 2, None, 3])

        assert artifact.report_subtype == "study_guide"

    def test_report_subtype_blog_post(self):
        """Test report_subtype for blog post."""
        artifact = Artifact.from_api_response(["id", "Blog Post: Topic", 2, None, 3])

        assert artifact.report_subtype == "blog_post"

    def test_report_subtype_generic(self):
        """Test report_subtype for generic report."""
        artifact = Artifact.from_api_response(["id", "Custom Report", 2, None, 3])

        assert artifact.report_subtype == "report"

    def test_report_subtype_non_report(self):
        """Test report_subtype for non-report artifact."""
        artifact = Artifact.from_api_response(["id", "Audio", 1, None, 3])

        assert artifact.report_subtype is None


class TestExtractArtifactUrlMalformedShapes:
    """Defensive coverage for the URL extractor helpers — every malformed-shape
    branch must return ``None`` instead of raising, so callers (Artifact.url,
    GenerationStatus.url, _is_media_ready, download_audio/video/infographic)
    can rely on the helper as a single source of truth."""

    def test_extract_artifact_url_unknown_type_returns_none(self):
        from notebooklm.types import _extract_artifact_url

        assert _extract_artifact_url(["any", "data"], None) is None
        assert _extract_artifact_url(["any", "data"], 99) is None

    def test_extract_artifact_url_none_type_ignores_row_type_code(self):
        from notebooklm.rpc import ArtifactTypeCode
        from notebooklm.types import _extract_artifact_url

        audio_row = [
            "artifact_id",
            "Audio",
            ArtifactTypeCode.AUDIO.value,
            None,
            3,
            None,
            [None, None, None, None, None, [["https://example.com/audio.mp4", None, "audio/mp4"]]],
        ]

        assert _extract_artifact_url(audio_row, None) is None
        assert (
            _extract_artifact_url(audio_row, ArtifactTypeCode.AUDIO.value)
            == "https://example.com/audio.mp4"
        )

    def test_extract_audio_handles_short_or_non_list_data(self):
        from notebooklm.types import _extract_audio_artifact_url

        assert _extract_audio_artifact_url([1, 2, 3]) is None  # too short
        assert _extract_audio_artifact_url([0] * 6 + ["not_a_list"]) is None  # data[6] not list
        assert _extract_audio_artifact_url([0] * 6 + [[1, 2, 3]]) is None  # data[6] too short
        assert _extract_audio_artifact_url([0] * 6 + [[0] * 5 + ["not_a_list"]]) is None
        assert _extract_audio_artifact_url([0] * 6 + [[0] * 5 + [[]]]) is None  # empty media list

    def test_extract_video_handles_short_or_non_list_data(self):
        from notebooklm.types import _extract_video_artifact_url

        assert _extract_video_artifact_url([1, 2, 3]) is None  # too short
        assert _extract_video_artifact_url([0] * 8 + ["not_a_list"]) is None
        assert _extract_video_artifact_url([0] * 8 + [[]]) is None  # empty data[8]
        assert _extract_video_artifact_url([0] * 8 + [["not_a_list"]]) is None
        assert _extract_video_artifact_url([0] * 8 + [[[None, None, "video/mp4"]]]) is None

    def test_extract_infographic_handles_malformed_data(self):
        from notebooklm.types import _extract_infographic_artifact_url

        assert _extract_infographic_artifact_url([]) is None
        assert _extract_infographic_artifact_url(["not_a_list"]) is None
        assert _extract_infographic_artifact_url([[1]]) is None  # item too short
        assert _extract_infographic_artifact_url([[1, 2, "not_a_list"]]) is None
        assert _extract_infographic_artifact_url([[1, 2, []]]) is None  # empty content
        assert _extract_infographic_artifact_url([[1, 2, [["only_one"]]]]) is None

    def test_extract_slide_deck_handles_short_or_non_string_data(self):
        from notebooklm.types import _extract_slide_deck_artifact_url

        assert _extract_slide_deck_artifact_url([1, 2, 3]) is None  # too short
        assert _extract_slide_deck_artifact_url([0] * 16 + ["not_a_list"]) is None
        assert _extract_slide_deck_artifact_url([0] * 16 + [[1, 2, 3]]) is None  # too short
        assert _extract_slide_deck_artifact_url([0] * 16 + [[None, None, None, 12345]]) is None
        assert (
            _extract_slide_deck_artifact_url([0] * 16 + [[None, None, None, "ftp://bad"]]) is None
        )


class TestArtifactKindProperty:
    """Tests for the Artifact.kind property."""

    def test_kind_returns_str_enum(self):
        """Test that kind returns an ArtifactType str enum."""
        artifact = Artifact(id="x", title="Test", _artifact_type=1, status=3)
        assert artifact.kind == ArtifactType.AUDIO
        assert isinstance(artifact.kind, ArtifactType)
        assert isinstance(artifact.kind, str)

    def test_kind_str_comparison(self):
        """Test that kind can be compared with strings."""
        artifact = Artifact(id="x", title="Test", _artifact_type=3, status=3)
        assert artifact.kind == "video"
        assert artifact.kind.value == "video"
        assert f"Type: {artifact.kind.value}" == "Type: video"

    @pytest.mark.parametrize(
        "artifact_type,variant,expected_kind",
        [
            (1, None, ArtifactType.AUDIO),
            (2, None, ArtifactType.REPORT),
            (3, None, ArtifactType.VIDEO),
            (4, 1, ArtifactType.FLASHCARDS),
            (4, 2, ArtifactType.QUIZ),
            (5, None, ArtifactType.MIND_MAP),
            (7, None, ArtifactType.INFOGRAPHIC),
            (8, None, ArtifactType.SLIDE_DECK),
            (9, None, ArtifactType.DATA_TABLE),
        ],
    )
    def test_kind_mapping(self, artifact_type, variant, expected_kind):
        """Test that artifact types are correctly mapped to ArtifactType enum."""
        artifact = Artifact(
            id="x", title="Test", _artifact_type=artifact_type, status=3, _variant=variant
        )
        assert artifact.kind == expected_kind

    def test_kind_unknown_for_unrecognized_type(self):
        """Test that kind returns UNKNOWN for unrecognized artifact types."""
        from notebooklm.types import _warned_artifact_types

        _warned_artifact_types.clear()

        artifact = Artifact(id="x", title="Test", _artifact_type=999, status=3)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = artifact.kind
            assert result == ArtifactType.UNKNOWN
            assert len(w) == 1
            assert issubclass(w[0].category, UnknownTypeWarning)
            assert "999" in str(w[0].message)

    def test_kind_unknown_for_unrecognized_quiz_variant(self):
        """Test that kind returns UNKNOWN for unrecognized QUIZ variants."""
        from notebooklm.types import _warned_artifact_types

        _warned_artifact_types.clear()

        artifact = Artifact(id="x", title="Test", _artifact_type=4, status=3, _variant=99)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = artifact.kind
            assert result == ArtifactType.UNKNOWN
            assert len(w) == 1
            assert issubclass(w[0].category, UnknownTypeWarning)


class TestGenerationStatus:
    def test_properties(self):
        """Test all status properties."""
        pending = GenerationStatus(task_id="t1", status="pending")
        in_progress = GenerationStatus(task_id="t2", status="in_progress")
        completed = GenerationStatus(task_id="t3", status="completed")
        failed = GenerationStatus(task_id="t4", status="failed")

        assert pending.is_pending is True
        assert pending.is_in_progress is False

        assert in_progress.is_in_progress is True
        assert in_progress.is_pending is False

        assert completed.is_complete is True
        assert completed.is_failed is False

        assert failed.is_failed is True
        assert failed.is_complete is False

    def test_with_url_and_error(self):
        """Test status with optional fields."""
        status = GenerationStatus(
            task_id="t1",
            status="completed",
            url="https://audio.url",
            error=None,
        )

        assert status.url == "https://audio.url"
        assert status.error is None

    def test_with_metadata(self):
        """Test status with metadata."""
        status = GenerationStatus(
            task_id="t1",
            status="completed",
            metadata={"key": "value"},
        )

        assert status.metadata == {"key": "value"}

    def test_is_rate_limited(self):
        """Test is_rate_limited property detection."""
        # Rate limited via error_code (preferred)
        rate_limited_code = GenerationStatus(
            task_id="",
            status="failed",
            error="Request rejected by API",
            error_code="USER_DISPLAYABLE_ERROR",
        )
        assert rate_limited_code.is_rate_limited is True

        # Rate limited via error message (string matching fallback)
        rate_limited_msg = GenerationStatus(
            task_id="",
            status="failed",
            error="Request rejected by API - may indicate rate limiting or quota exceeded",
        )
        assert rate_limited_msg.is_rate_limited is True

        # Quota exceeded (also rate limited)
        quota_exceeded = GenerationStatus(
            task_id="",
            status="failed",
            error="Quota exceeded for this operation",
        )
        assert quota_exceeded.is_rate_limited is True

        # Other failure (not rate limited)
        other_failure = GenerationStatus(
            task_id="",
            status="failed",
            error="Some unrelated generation error",
        )
        assert other_failure.is_rate_limited is False

        # Failed but no error message
        no_error = GenerationStatus(task_id="", status="failed", error=None)
        assert no_error.is_rate_limited is False

        # Completed status (never rate limited)
        completed = GenerationStatus(task_id="t1", status="completed")
        assert completed.is_rate_limited is False


class TestNotebookDescription:
    def test_from_api_response(self):
        """Test parsing NotebookDescription from dict."""
        data = {
            "summary": "This is a summary.",
            "suggested_topics": [
                {"question": "Q1?", "prompt": "P1"},
                {"question": "Q2?", "prompt": "P2"},
            ],
        }
        desc = NotebookDescription.from_api_response(data)

        assert desc.summary == "This is a summary."
        assert len(desc.suggested_topics) == 2
        assert desc.suggested_topics[0].question == "Q1?"
        assert desc.suggested_topics[0].prompt == "P1"

    def test_from_api_response_empty(self):
        """Test parsing with empty data."""
        data = {}
        desc = NotebookDescription.from_api_response(data)

        assert desc.summary == ""
        assert desc.suggested_topics == []


class TestReportSuggestion:
    def test_from_api_response(self):
        """Test parsing ReportSuggestion."""
        data = {
            "title": "Research Report",
            "description": "A detailed report",
            "prompt": "Write a report",
            "audience_level": 1,
        }
        suggestion = ReportSuggestion.from_api_response(data)

        assert suggestion.title == "Research Report"
        assert suggestion.description == "A detailed report"
        assert suggestion.prompt == "Write a report"
        assert suggestion.audience_level == 1

    def test_from_api_response_defaults(self):
        """Test parsing with missing optional fields."""
        data = {}
        suggestion = ReportSuggestion.from_api_response(data)

        assert suggestion.title == ""
        assert suggestion.audience_level == 2


class TestChatMode:
    def test_enum_values(self):
        """Test ChatMode enum values."""
        assert ChatMode.DEFAULT.value == "default"
        assert ChatMode.LEARNING_GUIDE.value == "learning_guide"
        assert ChatMode.CONCISE.value == "concise"
        assert ChatMode.DETAILED.value == "detailed"


class TestConversationTurn:
    def test_creation(self):
        """Test ConversationTurn creation."""
        turn = ConversationTurn(
            query="What is AI?",
            answer="AI stands for Artificial Intelligence.",
            turn_number=1,
        )

        assert turn.query == "What is AI?"
        assert turn.answer == "AI stands for Artificial Intelligence."
        assert turn.turn_number == 1


class TestAskResult:
    def test_creation(self):
        """Test AskResult creation."""
        result = AskResult(
            answer="The answer is 42.",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
            raw_response="Full raw response",
        )

        assert result.answer == "The answer is 42."
        assert result.conversation_id == "conv_123"
        assert result.turn_number == 1
        assert result.is_follow_up is False
        assert result.raw_response == "Full raw response"

    def test_creation_with_references(self):
        """Test AskResult creation with references."""
        refs = [
            ChatReference(source_id="src-1", citation_number=1),
            ChatReference(source_id="src-2", citation_number=2),
        ]
        result = AskResult(
            answer="Based on [1] and [2]...",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
            references=refs,
        )

        assert len(result.references) == 2
        assert result.references[0].source_id == "src-1"
        assert result.references[1].citation_number == 2

    def test_default_references_empty(self):
        """Test that references defaults to empty list."""
        result = AskResult(
            answer="Answer",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
        )

        assert result.references == []


class TestChatReference:
    def test_creation_minimal(self):
        """Test ChatReference with just source_id."""
        ref = ChatReference(source_id="abc123-def456-789")

        assert ref.source_id == "abc123-def456-789"
        assert ref.citation_number is None
        assert ref.start_char is None
        assert ref.end_char is None

    def test_creation_full(self):
        """Test ChatReference with all fields."""
        ref = ChatReference(
            source_id="abc123-def456-789",
            citation_number=1,
            start_char=100,
            end_char=200,
        )

        assert ref.source_id == "abc123-def456-789"
        assert ref.citation_number == 1
        assert ref.start_char == 100
        assert ref.end_char == 200

    def test_paired_offset_invariant_source_pair_half_populated(self):
        """start_char set without end_char (or vice versa) must raise."""
        with pytest.raises(ValueError, match="start_char/end_char"):
            ChatReference(source_id="x", start_char=5)
        with pytest.raises(ValueError, match="start_char/end_char"):
            ChatReference(source_id="x", end_char=5)

    def test_paired_offset_invariant_fragment_pair_half_populated(self):
        """A half-populated fragment range must raise, under either name.

        The deprecated ``answer_*`` keywords are reconciled onto the canonical
        ``fragment_*`` pair before validation (#2120), so both spellings fail
        with the canonical name in the message.
        """
        with pytest.raises(ValueError, match="fragment_start_char/fragment_end_char"):
            ChatReference(source_id="x", fragment_start_char=5)
        with pytest.raises(ValueError, match="fragment_start_char/fragment_end_char"):
            ChatReference(source_id="x", fragment_end_char=5)
        with pytest.raises(ValueError, match="fragment_start_char/fragment_end_char"):
            ChatReference(source_id="x", answer_start_char=5)
        with pytest.raises(ValueError, match="fragment_start_char/fragment_end_char"):
            ChatReference(source_id="x", answer_end_char=5)

    def test_paired_offset_invariant_inverted_source_range(self):
        """start_char > end_char must raise."""
        with pytest.raises(ValueError, match="> end_char"):
            ChatReference(source_id="x", start_char=10, end_char=5)

    def test_paired_offset_invariant_inverted_fragment_range(self):
        """fragment_start_char > fragment_end_char must raise."""
        with pytest.raises(ValueError, match="> fragment_end_char"):
            ChatReference(source_id="x", fragment_start_char=10, fragment_end_char=5)
        with pytest.raises(ValueError, match="> fragment_end_char"):
            ChatReference(source_id="x", answer_start_char=10, answer_end_char=5)

    def test_paired_offset_invariant_answer_doc_pair(self):
        """The new answer-document range is validated as its own pair (#2120)."""
        with pytest.raises(ValueError, match="answer_anchor_start/answer_anchor_end"):
            ChatReference(source_id="x", answer_anchor_start=5)
        with pytest.raises(ValueError, match="> answer_anchor_end"):
            ChatReference(source_id="x", answer_anchor_start=10, answer_anchor_end=5)

    def test_paired_offset_invariant_valid_constructions(self):
        """All-None pairs, both pairs populated, and zero-width ranges all accepted."""
        # All None pairs.
        ChatReference(source_id="x")
        # Source pair populated.
        ChatReference(source_id="x", start_char=5, end_char=10)
        # Fragment pair populated (under either name).
        ChatReference(source_id="x", fragment_start_char=0, fragment_end_char=3)
        ChatReference(source_id="x", answer_start_char=0, answer_end_char=3)
        # All three pairs populated.
        ChatReference(
            source_id="x",
            start_char=5,
            end_char=10,
            fragment_start_char=0,
            fragment_end_char=3,
            answer_anchor_start=1,
            answer_anchor_end=2,
        )
        # Zero-width ranges (start == end) are valid: many citations are
        # structural anchors that resolve to single-position ranges, and the
        # answer-side anchor is zero-width whenever it marks an insertion
        # point rather than a span (#2120).
        ChatReference(source_id="x", start_char=5, end_char=5)
        ChatReference(source_id="x", fragment_start_char=0, fragment_end_char=0)
        ChatReference(source_id="x", answer_anchor_start=7, answer_anchor_end=7)


class TestSourceFulltext:
    def test_creation(self):
        """Test SourceFulltext creation."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="My Source",
            content="This is the full content of the source.",
            _type_code=5,  # web_page
            url="https://example.com",
            char_count=40,
        )

        assert fulltext.source_id == "src-123"
        assert fulltext.title == "My Source"
        assert fulltext.content == "This is the full content of the source."
        assert fulltext.kind == SourceType.WEB_PAGE
        assert fulltext.url == "https://example.com"
        assert fulltext.char_count == 40

    def test_creation_minimal(self):
        """Test SourceFulltext with minimal fields."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Title",
            content="Content",
        )

        assert fulltext.source_id == "src-123"
        assert fulltext.kind == SourceType.UNKNOWN
        assert fulltext.url is None
        assert fulltext.char_count == 0

    def test_find_citation_context_single_match(self):
        """Test finding a single citation in content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Before text. The citation text appears here. After text.",
        )

        matches = fulltext.find_citation_context("The citation text", context_chars=10)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 13  # Position of "The citation text"
        assert "The citation text" in context

    def test_find_citation_context_multiple_matches(self):
        """Test finding multiple non-overlapping matches."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="First keyword here. Some other text. Second keyword here.",
        )

        matches = fulltext.find_citation_context("keyword", context_chars=5)

        assert len(matches) == 2
        assert matches[0][1] == 6  # Position of first "keyword"
        assert matches[1][1] == 44  # Position of second "keyword"

    def test_find_citation_context_no_match(self):
        """Test when citation is not found."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Some content that doesn't contain the search term.",
        )

        matches = fulltext.find_citation_context("nonexistent")

        assert matches == []

    def test_find_citation_context_empty_cited_text(self):
        """Test with empty cited_text."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Some content here.",
        )

        assert fulltext.find_citation_context("") == []
        assert fulltext.find_citation_context(None) == []  # type: ignore

    def test_find_citation_context_empty_content(self):
        """Test with empty content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="",
        )

        matches = fulltext.find_citation_context("search term")

        assert matches == []

    def test_find_citation_context_long_citation_truncated(self):
        """Test that citations >40 chars are truncated for search."""
        long_citation = "A" * 50  # 50 chars, should be truncated to 40
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Prefix " + "A" * 40 + "B" * 10 + " Suffix",  # Only first 40 As match
        )

        matches = fulltext.find_citation_context(long_citation, context_chars=5)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 7  # Position after "Prefix "
        # Context should use search_text length (40), not cited_text length (50)
        assert len(context) <= 5 + 40 + 5  # context_chars + search_text + context_chars

    def test_find_citation_context_at_start(self):
        """Test citation at the very start of content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Citation at start. Rest of content.",
        )

        matches = fulltext.find_citation_context("Citation at start", context_chars=50)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 0

    def test_find_citation_context_at_end(self):
        """Test citation at the very end of content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Beginning content. Citation at end",
        )

        matches = fulltext.find_citation_context("Citation at end", context_chars=50)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 19


class TestSourceSummary:
    """Tests for SourceSummary dataclass."""

    def test_to_dict_with_all_fields(self):
        """Test serialization with all fields present."""
        from notebooklm.types import SourceSummary, SourceType

        summary = SourceSummary(
            kind=SourceType.PDF,
            title="Test PDF",
            url="https://example.com/test.pdf",
        )

        result = summary.to_dict()
        assert result == {
            "type": "pdf",
            "title": "Test PDF",
            "url": "https://example.com/test.pdf",
        }

    def test_to_dict_with_missing_fields(self):
        """Test serialization with missing optional fields."""
        from notebooklm.types import SourceSummary, SourceType

        summary = SourceSummary(kind=SourceType.PASTED_TEXT)

        result = summary.to_dict()
        assert result == {
            "type": "pasted_text",
            "title": None,
            "url": None,
        }

    def test_to_dict_consistent_schema(self):
        """Test that schema is always consistent (all keys present)."""
        from notebooklm.types import SourceSummary, SourceType

        # All keys should be present even when values are None
        summary1 = SourceSummary(kind=SourceType.PDF, title="test.pdf")
        summary2 = SourceSummary(kind=SourceType.WEB_PAGE, url="https://example.com")

        dict1 = summary1.to_dict()
        dict2 = summary2.to_dict()

        # Both should have the same keys
        assert set(dict1.keys()) == set(dict2.keys())
        assert set(dict1.keys()) == {"type", "title", "url"}


class TestNotebookMetadata:
    """Tests for NotebookMetadata dataclass."""

    def test_to_dict_serialization(self):
        """Test serialization to dictionary format."""
        from datetime import datetime

        from notebooklm.types import Notebook, NotebookMetadata, SourceSummary, SourceType

        notebook = Notebook(
            id="nb_123",
            title="Test Notebook",
            created_at=datetime(2024, 1, 1, 12, 0),
            is_owner=True,
            modified_at=datetime(2024, 1, 2, 9, 30),
            role=SharePermission.OWNER,
        )
        metadata = NotebookMetadata(
            notebook=notebook,
            sources=[
                SourceSummary(kind=SourceType.PDF, title="test.pdf"),
                SourceSummary(kind=SourceType.WEB_PAGE, title="Example", url="https://example.com"),
            ],
        )

        result = metadata.to_dict()
        assert result == {
            "id": "nb_123",
            "title": "Test Notebook",
            "created_at": "2024-01-01T12:00:00",
            "last_viewed_at": "2024-01-02T09:30:00",
            "modified_at": "2024-01-02T09:30:00",
            "is_owner": True,
            "role": "owner",
            "sources": [
                {"type": "pdf", "title": "test.pdf", "url": None},
                {"type": "web_page", "title": "Example", "url": "https://example.com"},
            ],
        }

    def test_properties_proxy_to_notebook(self):
        """Test that properties proxy to the underlying Notebook."""
        from datetime import datetime

        from notebooklm.types import Notebook, NotebookMetadata

        notebook = Notebook(
            id="nb_456",
            title="Proxy Test",
            created_at=datetime(2024, 2, 1),
            is_owner=False,
            modified_at=datetime(2024, 3, 1),
            role=SharePermission.VIEWER,
        )
        metadata = NotebookMetadata(notebook=notebook)

        assert metadata.id == "nb_456"
        assert metadata.title == "Proxy Test"
        assert metadata.created_at == datetime(2024, 2, 1)
        assert metadata.last_viewed_at == datetime(2024, 3, 1)
        assert metadata.is_owner is False
        assert metadata.role is SharePermission.VIEWER

    def test_to_dict_with_none_created_at(self):
        """Test serialization when created_at is None."""
        from notebooklm.types import Notebook, NotebookMetadata

        notebook = Notebook(id="nb_789", title="No Timestamp", created_at=None)
        metadata = NotebookMetadata(notebook=notebook, sources=[])

        result = metadata.to_dict()
        assert result["created_at"] is None
        # An unknown role serializes as ``null``, not the "unknown" label.
        assert result["role"] is None

    def test_empty_sources_list(self):
        """Test metadata with empty sources list."""
        from notebooklm.types import Notebook, NotebookMetadata

        notebook = Notebook(id="nb_empty", title="Empty Notebook")
        metadata = NotebookMetadata(notebook=notebook, sources=[])

        assert len(metadata.sources) == 0
        assert metadata.to_dict()["sources"] == []


class TestLastViewedAtAlias:
    """``modified_at`` -> ``last_viewed_at`` rename and its runway (#2126).

    The wire slot ``meta[5]`` is ``ProjectMetadata.lastViewedTime``, not a
    modification time. These tests pin the two halves of the deprecation runway
    that ``docs/deprecations.md`` promises:

    * ``Notebook.modified_at`` stays a *field* and stays silent — a runtime
      warning on a dataclass field would also fire from ``repr``, ``__eq__``,
      ``dataclasses.replace`` and ``to_jsonable``.
    * ``NotebookMetadata.modified_at`` is a *property*, so it warns exactly once
      per caller access and honors ``NOTEBOOKLM_QUIET_DEPRECATIONS``.
    """

    def test_canonical_field_mirrors_into_the_alias(self):
        """Setting only ``last_viewed_at`` populates the legacy ``modified_at``."""
        viewed = datetime(2026, 8, 12, 10, 0)
        nb = Notebook(id="nb_1", title="N", last_viewed_at=viewed)

        assert nb.last_viewed_at == viewed
        assert nb.modified_at == viewed

    def test_legacy_kwarg_still_seeds_the_canonical_field(self):
        """A pre-rename caller passing ``modified_at=`` keeps working."""
        viewed = datetime(2026, 8, 12, 10, 0)
        nb = Notebook(id="nb_1", title="N", modified_at=viewed)

        assert nb.last_viewed_at == viewed
        assert nb.modified_at == viewed

    def test_legacy_positional_construction_is_unchanged(self):
        """``last_viewed_at`` was appended last, so old positional args still land."""
        created = datetime(2026, 8, 1)
        viewed = datetime(2026, 8, 12)
        nb = Notebook("nb_1", "N", created, 3, True, viewed)

        assert nb.created_at == created
        assert nb.sources_count == 3
        assert nb.last_viewed_at == viewed
        assert nb.modified_at == viewed

    def test_both_names_none_when_neither_supplied(self):
        """The mirror never fabricates a timestamp."""
        nb = Notebook(id="nb_1", title="N")

        assert nb.last_viewed_at is None
        assert nb.modified_at is None

    def test_in_place_assignment_keeps_the_alias_in_step(self):
        """``__setattr__`` mirrors post-construction writes (the backfill path)."""
        nb = Notebook(id="nb_1", title="N")
        assert nb.modified_at is None

        # ``_app.notebooks._backfill_create_timestamps`` writes exactly this way.
        nb.last_viewed_at = datetime(2026, 8, 12, 10, 0)

        assert nb.modified_at == datetime(2026, 8, 12, 10, 0)

    def test_legacy_in_place_write_round_trips(self):
        """A legacy caller writing the OLD name must not silently diverge.

        Without the reverse mirror, ``nb.modified_at = B`` left
        ``last_viewed_at`` at its old value, and the object then serialized two
        different answers: ``to_jsonable`` (all fields) reported ``B`` while the
        CLI's ``notebook_viewed_keys`` (canonical field only) reported the
        pre-write value.
        """
        from notebooklm._app.serialize import to_jsonable
        from notebooklm._app.views import notebook_viewed_keys

        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2020, 1, 1))
        nb.modified_at = datetime(2026, 8, 12, 10, 0)

        assert nb.last_viewed_at == datetime(2026, 8, 12, 10, 0)
        assert to_jsonable(nb)["last_viewed_at"] == to_jsonable(nb)["modified_at"]
        assert notebook_viewed_keys(nb)["modified_at"] == "2026-08-12T10:00:00"

    def test_replace_on_the_canonical_field_carries_the_alias(self):
        """``dataclasses.replace`` keeps the pair in step (the documented path)."""
        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2020, 1, 1))

        replaced = dataclasses.replace(nb, last_viewed_at=datetime(2026, 8, 12))

        assert replaced.last_viewed_at == datetime(2026, 8, 12)
        assert replaced.modified_at == datetime(2026, 8, 12)

    def test_role_invariant_survives_the_alias_mirror(self):
        """The #2125 ``role`` -> ``is_owner`` hook still fires (shared __setattr__)."""
        nb = Notebook(id="nb_1", title="N", role=SharePermission.VIEWER)
        assert nb.is_owner is False

        nb.role = SharePermission.OWNER
        assert nb.is_owner is True

    def test_pre_rename_pickle_restores_with_the_names_in_agreement(self):
        """An old pickle must not restore into the "names disagree" state.

        Unpickling bypasses ``__init__``/``__post_init__``/``__setattr__``, so a
        pickle written before the rename carries ``modified_at`` and no
        ``last_viewed_at`` key. It does NOT raise — ``last_viewed_at`` is a
        dataclass field with a ``None`` class default, so the lookup silently
        falls through to the class. That silence is the hazard: the caller sees
        a populated legacy name beside a ``None`` canonical one. ``__setstate__``
        seeds the canonical field so the alias runway holds here too.
        """
        nb = Notebook(id="nb_1", title="N", modified_at=datetime(2026, 8, 12))
        # Simulate the pre-#2126 payload: the field simply did not exist then.
        del nb.__dict__["last_viewed_at"]

        restored = pickle.loads(pickle.dumps(nb))

        assert restored.last_viewed_at == datetime(2026, 8, 12)
        assert restored.modified_at == restored.last_viewed_at

    def test_pre_chat_sessions_pickle_restores_with_an_empty_list(self):
        """A pre-#2133 pickle gets the default-factory field it never stored."""
        nb = Notebook(id="nb_1", title="N")
        del nb.__dict__["chat_sessions"]

        restored = pickle.loads(pickle.dumps(nb))

        assert restored.chat_sessions == []
        assert repr(restored)
        assert dataclasses.asdict(restored)["chat_sessions"] == []

    def test_current_pickle_round_trip_is_unchanged(self):
        """The ordinary round trip keeps equality — ``__setstate__`` is additive."""
        nb = Notebook(
            id="nb_1",
            title="N",
            last_viewed_at=datetime(2026, 8, 12),
            role=SharePermission.VIEWER,
        )

        restored = pickle.loads(pickle.dumps(nb))

        assert restored == nb
        assert restored.role is SharePermission.VIEWER
        assert restored.is_owner is False

    def test_notebook_field_access_does_not_warn(self):
        """``Notebook.modified_at`` is a docs-only deprecation (a dataclass field)."""
        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12))

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert nb.modified_at == nb.last_viewed_at
            repr(nb)
            assert nb == Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12))

    def test_serializer_emits_both_keys(self):
        """``to_jsonable`` carries the new key and the legacy alias, same value."""
        from notebooklm._app.serialize import to_jsonable

        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12, 10, 0))
        payload = to_jsonable(nb)

        assert payload["last_viewed_at"] == "2026-08-12T10:00:00"
        assert payload["modified_at"] == payload["last_viewed_at"]

    def test_metadata_property_warns_and_returns_the_value(self):
        """``NotebookMetadata.modified_at`` warns, naming the replacement."""
        from notebooklm.types import NotebookMetadata

        viewed = datetime(2026, 8, 12, 10, 0)
        metadata = NotebookMetadata(notebook=Notebook(id="nb_1", title="N", last_viewed_at=viewed))

        with pytest.warns(DeprecationWarning, match="last_viewed_at") as record:
            assert metadata.modified_at == viewed

        assert "lastViewedTime" in str(record[0].message)
        assert "v1.0" in str(record[0].message)

    def test_metadata_property_warning_honors_the_quiet_gate(self):
        """ADR-0018: every project deprecation obeys NOTEBOOKLM_QUIET_DEPRECATIONS."""
        from notebooklm.types import NotebookMetadata

        metadata = NotebookMetadata(
            notebook=Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12))
        )

        with (
            patch.dict(os.environ, {"NOTEBOOKLM_QUIET_DEPRECATIONS": "1"}),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("error", DeprecationWarning)
            assert metadata.modified_at is not None

    def test_replace_on_the_legacy_name_is_a_documented_no_op(self):
        """``replace(nb, modified_at=X)`` cannot win — pinned, because it is odd.

        ``dataclasses.replace`` re-passes every field from the source instance,
        so the canonical ``last_viewed_at`` is supplied too and (being assigned
        last) takes precedence. ``docs/deprecations.md`` and ``__post_init__``
        both document this; without a pin, a refactor that made ``modified_at``
        authoritative would silently flip which argument wins.
        """
        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2020, 1, 1))

        replaced = dataclasses.replace(nb, modified_at=datetime(2026, 8, 12))

        assert replaced.last_viewed_at == datetime(2020, 1, 1)
        assert replaced.modified_at == datetime(2020, 1, 1)

    def test_canonical_name_wins_when_both_are_supplied(self):
        """The tie-break of the whole runway: ``last_viewed_at`` is authoritative."""
        nb = Notebook(
            id="nb_1",
            title="N",
            modified_at=datetime(2020, 1, 1),
            last_viewed_at=datetime(2026, 8, 12),
        )

        assert nb.last_viewed_at == datetime(2026, 8, 12)
        assert nb.modified_at == datetime(2026, 8, 12)

    def test_clearing_one_name_leaves_the_other_stale_by_design(self):
        """Characterization: both mirrors are guarded on ``value is not None``.

        That guard is load-bearing, not an oversight — an unguarded mirror would
        let ``__init__``'s ``last_viewed_at=None`` default wipe out a legacy
        ``Notebook(..., modified_at=X)`` argument, because ``__init__`` assigns
        ``modified_at`` first. Clearing a decoded timestamp is not something any
        caller does, so the hole is accepted; this test exists so that anyone who
        "fixes" the guard sees *this* fail with the reason, rather than
        ``test_legacy_kwarg_still_seeds_the_canonical_field`` failing obscurely.
        """
        nb = Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12))

        nb.last_viewed_at = None

        assert nb.last_viewed_at is None
        assert nb.modified_at == datetime(2026, 8, 12)  # deliberately not mirrored

    def test_metadata_serialization_never_warns_through_to_jsonable(self):
        """The MCP ``notebook_describe`` path must not warn either.

        ``to_dict()`` is the CLI/REST path; ``mcp/tools/notebooks.py`` reaches
        ``NotebookMetadata`` through ``to_jsonable`` instead. The
        "only a caller who *types* the old name gets a warning" claim has to hold
        on both, and ``to_jsonable``'s fields-only rule is what makes it hold —
        so pin it here rather than depending on that rule never changing.
        """
        from notebooklm._app.serialize import to_jsonable
        from notebooklm.types import NotebookMetadata

        metadata = NotebookMetadata(
            notebook=Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12))
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            payload = to_jsonable(metadata)

        assert payload["notebook"]["last_viewed_at"] == payload["notebook"]["modified_at"]

    def test_metadata_to_dict_emits_both_keys_without_warning(self):
        """Serializing reads the canonical field, so it never warns."""
        from notebooklm.types import NotebookMetadata

        metadata = NotebookMetadata(
            notebook=Notebook(id="nb_1", title="N", last_viewed_at=datetime(2026, 8, 12, 10, 0))
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            payload = metadata.to_dict()

        assert payload["last_viewed_at"] == "2026-08-12T10:00:00"
        assert payload["modified_at"] == payload["last_viewed_at"]
