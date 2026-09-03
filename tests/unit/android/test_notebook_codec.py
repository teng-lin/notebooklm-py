"""Edge-case coverage for the Android notebook codec's partial-shape branches.

``tests/unit/android/test_notebook_source_reads.py`` pins the populated
``GetProject`` decode and ``test_chat.py`` drives the chat-settings read
through ``AndroidChatAPI``. These cases call the codec directly to reach the
shapes a healthy backend does not send: a metadata block without a creation
time, an advanced-settings block that is only half present or carries a value
outside the public enums, and a guide response with nothing in it.

The chat-settings branches are all *refusals*: an incomplete or unrecognized
settings block has to read as "unknown" so a caller's read-modify-write cycle
cannot silently overwrite a real server-side setting with a guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._android.codecs.notebooks import (
    decode_notebook_guide,
    decode_project,
    message_to_known_dict,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)
from notebooklm.exceptions import DecodingError
from notebooklm.types import ChatGoal, ChatResponseLength, SharePermission

METHOD_ID = "test-method"
NB = "notebook-1"


def _project(**kwargs: Any) -> Any:
    """Build the wire projection that carries the ``advanced_settings`` branch."""
    return wire_notebooks_pb2.WireProjectWithAdvancedSettings(id=NB, **kwargs)


def _settings_project(
    *,
    goal: int | None = None,
    custom_prompt: str = "",
    response_length: int | None = None,
) -> Any:
    project = _project()
    if goal is not None:
        project.advanced_settings.goal_settings.goal = goal
        project.advanced_settings.goal_settings.custom_prompt = custom_prompt
    if response_length is not None:
        project.advanced_settings.response_style_settings.response_length = response_length
    return project


# ---------------------------------------------------------------------------
# decode_project: metadata
# ---------------------------------------------------------------------------


def test_project_metadata_without_a_create_time_still_yields_the_role() -> None:
    """The two metadata fields are independent; one missing must not drop both."""
    project = _project()
    project.metadata.user_role = read_pb2.PROJECT_ROLE_READER

    decoded = decode_project(project, method_id=METHOD_ID)

    assert decoded.created_at is None
    assert decoded.role is SharePermission.VIEWER


# ---------------------------------------------------------------------------
# decode_project: chat settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "response_length"),
    [
        pytest.param(int(ChatGoal.DEFAULT), None, id="goal-only"),
        pytest.param(None, int(ChatResponseLength.DEFAULT), id="response-style-only"),
    ],
)
def test_half_populated_advanced_settings_read_as_unknown(
    goal: int | None, response_length: int | None
) -> None:
    """A settings block missing either arm cannot be completed by guessing.

    Reporting the present half plus a default for the absent one would let a
    read-modify-write configure call overwrite the real server-side value. The
    refusal is doubly held: the absent arm reads back as wire ``0``, which is
    not a member of either public enum, so the enum guard would refuse it too.
    """
    decoded = decode_project(
        _settings_project(goal=goal, response_length=response_length),
        method_id=METHOD_ID,
        include_chat_settings=True,
    )

    assert decoded.chat_settings is None


@pytest.mark.parametrize(
    ("goal", "response_length"),
    [
        pytest.param(99, int(ChatResponseLength.DEFAULT), id="unknown-goal"),
        pytest.param(int(ChatGoal.DEFAULT), 99, id="unknown-response-length"),
    ],
)
def test_advanced_settings_outside_the_public_enums_read_as_unknown(
    goal: int, response_length: int
) -> None:
    """A value the public enums do not model is drift, not a decode failure.

    ``chat_settings`` is one field of a notebook read, so a new backend enum
    member must not take the whole ``GetProject`` projection down with it.
    """
    decoded = decode_project(
        _settings_project(goal=goal, response_length=response_length),
        method_id=METHOD_ID,
        include_chat_settings=True,
    )

    assert decoded.chat_settings is None
    # The rest of the projection is unaffected.
    assert decoded.id == NB


def test_a_custom_prompt_left_beside_a_non_custom_goal_is_dropped() -> None:
    """The backend keeps the last custom prompt after a switch away from CUSTOM.

    Surfacing it would make ``ChatSettings`` claim a prompt that is not in
    effect, and a read-modify-write would then re-apply it.
    """
    decoded = decode_project(
        _settings_project(
            goal=int(ChatGoal.DEFAULT),
            custom_prompt="a prompt that no longer applies",
            response_length=int(ChatResponseLength.LONGER),
        ),
        method_id=METHOD_ID,
        include_chat_settings=True,
    )

    assert decoded.chat_settings is not None
    assert decoded.chat_settings.goal is ChatGoal.DEFAULT
    assert decoded.chat_settings.response_length is ChatResponseLength.LONGER
    assert decoded.chat_settings.custom_prompt is None


def test_a_custom_goal_without_its_prompt_reads_as_unknown() -> None:
    """CUSTOM without the prompt text is an unusable pair, so it is refused."""
    decoded = decode_project(
        _settings_project(
            goal=int(ChatGoal.CUSTOM),
            custom_prompt="",
            response_length=int(ChatResponseLength.DEFAULT),
        ),
        method_id=METHOD_ID,
        include_chat_settings=True,
    )

    assert decoded.chat_settings is None


def test_a_project_without_the_settings_field_reads_as_the_documented_default() -> None:
    """``Project`` itself has no ``advanced_settings``; that absence is the default.

    The wire projection exists only to preserve the branch, so a response
    decoded from the exact schema must still answer the chat-settings read.
    """
    decoded = decode_project(
        read_pb2.Project(id=NB),
        method_id=METHOD_ID,
        include_chat_settings=True,
    )

    assert decoded.chat_settings is not None
    assert decoded.chat_settings.goal is ChatGoal.DEFAULT
    assert decoded.chat_settings.response_length is ChatResponseLength.DEFAULT


# ---------------------------------------------------------------------------
# message_to_known_dict
# ---------------------------------------------------------------------------


def test_message_to_known_dict_wraps_a_non_message_as_bounded_drift() -> None:
    """The raw-render helper feeds a debug surface; it may not leak an internal type."""
    with pytest.raises(DecodingError, match="Could not render Android protobuf response") as raised:
        message_to_known_dict(object(), method_id=METHOD_ID)

    assert raised.value.method_id == METHOD_ID


# ---------------------------------------------------------------------------
# decode_notebook_guide
# ---------------------------------------------------------------------------


def test_guide_response_without_a_guide_is_an_empty_description() -> None:
    """A notebook with no guide yet answers with absence, not an error."""
    decoded = decode_notebook_guide(
        wire_notebooks_pb2.WireGenerateNotebookGuideResponse(),
        method_id=METHOD_ID,
    )

    assert decoded.summary == ""
    assert decoded.suggested_topics == []


def test_a_guide_without_suggested_topics_keeps_its_summary() -> None:
    """The topics block is optional; its absence must not discard the summary."""
    response = wire_notebooks_pb2.WireGenerateNotebookGuideResponse()
    response.notebook_guide.summary.text_summary = "What this notebook covers"

    decoded = decode_notebook_guide(response, method_id=METHOD_ID)

    assert decoded.summary == "What this notebook covers"
    assert decoded.suggested_topics == []
