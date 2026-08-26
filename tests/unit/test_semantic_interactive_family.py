"""Focused service tests for the neutral P5.3 Quiz/Flashcards family."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._read_services import NotebookReadService
from notebooklm._records import (
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ArtifactGetResult,
    ArtifactListResult,
    ArtifactRecord,
    ArtifactUserStateRecord,
    GenerationStatusRecord,
    InteractiveGenerateInput,
    InteractiveGenerateRequest,
    InteractiveGenerateResult,
    InteractiveMetadataRecord,
)
from notebooklm._studio import (
    InteractiveFamilyService,
    StudioCatalog,
    StudioGenerationInputs,
)
from tests._fixtures.recording_backend import RecordingBackend


def _generation_inputs(backend: RecordingBackend) -> StudioGenerationInputs:
    """The R5.1a resolver every generate family now takes."""
    return StudioGenerationInputs(NotebookReadService(backend))


def _interactive(
    artifact_id: str,
    family: str,
    *,
    status: str = "completed",
    user_state: ArtifactUserStateRecord | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        title=family.title(),
        family=family,
        variant=family,
        status=status,
        generation_prompt="Private study prompt",
        user_state=user_state,
    )


def test_interactive_records_are_frozen_slotted_closed_and_redacted() -> None:
    status = GenerationStatusRecord(
        "task",
        "pending",
        "https://example.invalid/task",
        "Private generation error",
    )
    value = InteractiveGenerateInput(
        "nb",
        ("src",),
        "Private instructions",
        "more",
        "hard",
    )
    result = InteractiveGenerateResult(status)
    metadata = InteractiveMetadataRecord(
        "task",
        "flashcards",
        "completed",
        True,
        "Private prompt",
    )

    assert all(not hasattr(item, "__dict__") for item in (status, value, result, metadata))
    assert all(item == replace(item) for item in (status, value, result, metadata))
    assert ARTIFACT_GENERATE_QUIZ_DEF.key is Operation.ARTIFACT_GENERATE_QUIZ
    assert ARTIFACT_GENERATE_FLASHCARDS_DEF.key is Operation.ARTIFACT_GENERATE_FLASHCARDS
    assert ARTIFACT_GENERATE_QUIZ_DEF.policy is CallPolicy.STATEFUL_START
    assert ARTIFACT_GENERATE_FLASHCARDS_DEF.policy is CallPolicy.STATEFUL_START
    assert "example.invalid" not in repr(status)
    assert "Private" not in repr(status)
    assert "Private" not in repr(value)
    assert "Private" not in repr(metadata)
    with pytest.raises(FrozenInstanceError):
        status.__setattr__("task_id", "changed")


@pytest.mark.asyncio
async def test_generation_methods_record_exact_operation_value_and_deadline() -> None:
    backend = RecordingBackend()
    result = InteractiveGenerateResult(GenerationStatusRecord("task", "pending"))
    backend.set_result(ARTIFACT_GENERATE_QUIZ_DEF, result)
    backend.set_result(ARTIFACT_GENERATE_FLASHCARDS_DEF, result)
    service = InteractiveFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    request = InteractiveGenerateRequest("nb", ("src",), "Focus", "fewer", "hard")
    value = InteractiveGenerateInput("nb", ("src",), "Focus", "fewer", "hard")

    quiz = await service.generate_quiz(request, deadline=deadline)
    flashcards = await service.generate_flashcards(request, deadline=deadline)

    assert quiz == flashcards == result
    assert [item.operation for item in backend.invocations] == [
        Operation.ARTIFACT_GENERATE_QUIZ,
        Operation.ARTIFACT_GENERATE_FLASHCARDS,
    ]
    assert all(item.value == value and item.deadline is deadline for item in backend.invocations)


@pytest.mark.asyncio
async def test_family_catalog_preserves_flashcard_and_quiz_user_state_without_refetch() -> None:
    flashcard_state = ArtifactUserStateRecord(
        "flashcards",
        card_acquisitions=(("0", "acquired"),),
        current_card_index=2,
        hidden_card_indices=(4,),
        last_shown_order=(2, 0, 1),
        current_view="card",
    )
    quiz_state = ArtifactUserStateRecord(
        "unknown",
        raw={"currentQuestionIndex": 3, "userAnswers": {"0": 2}},
    )
    flashcards = _interactive("cards", "flashcards", user_state=flashcard_state)
    quiz = _interactive("quiz", "quiz", user_state=quiz_state)
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_LIST_DEF, ArtifactListResult((flashcards, quiz)))
    backend.set_result(ARTIFACT_GET_DEF, ArtifactGetResult(flashcards))
    service = InteractiveFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))

    listed_cards = await service.list_flashcards("nb")
    listed_quizzes = await service.list_quizzes("nb")
    selected = await service.get("nb", "cards", "flashcards")

    assert listed_cards == (flashcards,)
    assert listed_quizzes == (quiz,)
    assert selected is flashcards
    assert InteractiveFamilyService.metadata(flashcards).user_state == flashcard_state
    assert InteractiveFamilyService.metadata(quiz).user_state == quiz_state
    assert [item.operation for item in backend.invocations] == [
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_GET,
    ]


def test_usable_readiness_does_not_redefine_lifecycle_terminal_state() -> None:
    pending = _interactive("quiz", "quiz", status="pending")
    completed = _interactive("cards", "flashcards", status="completed")

    pending_metadata = InteractiveFamilyService.metadata(pending)
    complete_metadata = InteractiveFamilyService.metadata(completed)

    assert (pending_metadata.lifecycle_status, pending_metadata.usable) == ("pending", False)
    assert (complete_metadata.lifecycle_status, complete_metadata.usable) == (
        "completed",
        True,
    )
