"""Artifact generation tests.

All artifact generation tests consolidated here. These tests:
- Use `generation_notebook_id` fixture (auto-created, has content)
- Variant tests are marked @pytest.mark.variants (skip to save quota)

Notebook lifecycle:
- Auto-created on first run if NOTEBOOKLM_GENERATION_NOTEBOOK_ID not set
- Artifacts/notes cleaned BEFORE tests to ensure clean state
- In CI (CI=true): notebook deleted after tests to avoid orphans
- Locally: notebook persists for verification, ID stored in the active profile cache
"""

import pytest

from notebooklm import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    QuizDifficulty,
    QuizQuantity,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)

from ._generation_helpers import generate_note_mind_map
from .conftest import (
    assert_generation_started,
    read_back_option_pair,
    requires_auth,
)

# Live CREATE_ARTIFACT coverage — monitored by the nightly generation coverage
# floor (tests/e2e/conftest.py, #1819): a fully-throttled run where every marked
# generation test skipped and none passed reds the nightly instead of hollow-green.
# INVARIANT: keep this module generation-only — a passing non-generation test here
# would satisfy the floor and mask a genuinely throttled generation surface.
pytestmark = pytest.mark.live_generation


@requires_auth
class TestAudioGeneration:
    """Audio generation tests."""

    @pytest.mark.asyncio
    async def test_generate_audio_default(self, client, generation_notebook_id):
        """Test audio generation with true defaults."""
        result = await client.artifacts.generate_audio(generation_notebook_id)
        assert_generation_started(result)

    @pytest.mark.asyncio
    async def test_generate_audio_brief(self, client, generation_notebook_id):
        """Test audio generation with non-default format to verify param encoding."""
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            audio_format=AudioFormat.BRIEF,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_audio_deep_dive_long(self, client, generation_notebook_id):
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            audio_format=AudioFormat.DEEP_DIVE,
            audio_length=AudioLength.LONG,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_audio_brief_short(self, client, generation_notebook_id):
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            audio_format=AudioFormat.BRIEF,
            audio_length=AudioLength.SHORT,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_audio_critique(self, client, generation_notebook_id):
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            audio_format=AudioFormat.CRITIQUE,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_audio_debate(self, client, generation_notebook_id):
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            audio_format=AudioFormat.DEBATE,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_audio_with_language(self, client, generation_notebook_id):
        result = await client.artifacts.generate_audio(
            generation_notebook_id,
            language="en",
        )
        assert_generation_started(result)


@requires_auth
class TestVideoGeneration:
    """Video generation tests."""

    @pytest.mark.asyncio
    async def test_generate_video_default(self, client, generation_notebook_id):
        """Test video generation with non-default style to verify param encoding."""
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_style=VideoStyle.ANIME,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_explainer_anime(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_format=VideoFormat.EXPLAINER,
            video_style=VideoStyle.ANIME,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_brief_whiteboard(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_format=VideoFormat.BRIEF,
            video_style=VideoStyle.WHITEBOARD,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_with_instructions(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_format=VideoFormat.EXPLAINER,
            video_style=VideoStyle.CLASSIC,
            instructions="Focus on key concepts for beginners",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_kawaii_style(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_style=VideoStyle.KAWAII,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_watercolor_style(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_style=VideoStyle.WATERCOLOR,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_video_auto_style(self, client, generation_notebook_id):
        result = await client.artifacts.generate_video(
            generation_notebook_id,
            video_style=VideoStyle.AUTO_SELECT,
        )
        assert_generation_started(result)


@requires_auth
@pytest.mark.variants
class TestCinematicVideoGeneration:
    """Cinematic video generation tests.

    Cinematic videos use Veo 3 AI for documentary-style footage.
    Requires Google AI Ultra subscription. Generation takes ~30-40 minutes.
    The whole class is opt-in because each request is materially more expensive
    than a standard video generation.
    """

    @pytest.mark.asyncio
    async def test_generate_cinematic_video_default(self, client, generation_notebook_id):
        """Test cinematic video generation with defaults."""
        result = await client.artifacts.generate_cinematic_video(generation_notebook_id)
        assert_generation_started(result)

    @pytest.mark.asyncio
    async def test_generate_cinematic_video_with_instructions(self, client, generation_notebook_id):
        """Test cinematic video generation with custom instructions."""
        result = await client.artifacts.generate_cinematic_video(
            generation_notebook_id,
            instructions="Focus on key concepts for a general audience",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    async def test_generate_cinematic_video_with_language(self, client, generation_notebook_id):
        """Test cinematic video generation with explicit language."""
        result = await client.artifacts.generate_cinematic_video(
            generation_notebook_id,
            language="en",
        )
        assert_generation_started(result)


@requires_auth
class TestQuizGeneration:
    """Quiz generation tests.

    Every option pair here is **asymmetric** and read back off the wire (#2195).
    Both rules are load-bearing:

    * ``assert_generation_started`` alone cannot tell "the fewest, hardest
      questions" from "the most, easiest" — which is why #2116 shipped a fully
      transposed flashcards payload through a green e2e tier.
    * a symmetric pair defeats the read-back too, and ``QuizQuantity.MORE`` and
      ``QuizDifficulty.HARD`` are BOTH ``3``, so ``more`` + ``hard`` is exactly
      as blind as ``standard`` + ``medium``.
    """

    @pytest.mark.asyncio
    async def test_generate_quiz_fewer_hard(self, client, generation_notebook_id):
        """Quiz options survive the round trip: what we send is what is stored."""
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.HARD,
        )
        assert_generation_started(result)
        options = await read_back_option_pair(
            client, generation_notebook_id, result.task_id, family="quiz"
        )
        assert options.quantity == QuizQuantity.FEWER
        assert options.difficulty == QuizDifficulty.HARD

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_quiz_with_options(self, client, generation_notebook_id):
        """``MORE`` is a distinct wire value (3), not the alias #2117 claimed.

        Paired with ``EASY`` rather than ``HARD``: ``MORE`` and ``HARD`` are
        both 3, so that pairing could not detect a transposition.
        """
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            quantity=QuizQuantity.MORE,
            difficulty=QuizDifficulty.EASY,
            instructions="Focus on key concepts and definitions",
        )
        assert_generation_started(result)
        options = await read_back_option_pair(
            client, generation_notebook_id, result.task_id, family="quiz"
        )
        assert options.quantity == QuizQuantity.MORE
        assert options.difficulty == QuizDifficulty.EASY

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_quiz_fewer_medium(self, client, generation_notebook_id):
        """A third pair, again asymmetric — ``FEWER`` + ``EASY`` are both 1."""
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.MEDIUM,
        )
        assert_generation_started(result)
        options = await read_back_option_pair(
            client, generation_notebook_id, result.task_id, family="quiz"
        )
        assert options.quantity == QuizQuantity.FEWER
        assert options.difficulty == QuizDifficulty.MEDIUM


@requires_auth
class TestFlashcardsGeneration:
    """Flashcards generation tests.

    Same asymmetry + read-back rules as :class:`TestQuizGeneration`, and they
    matter most here: flashcards is the family that shipped transposed (#2116),
    and its options live in a *different* slot from the quiz ones
    (``data[9][1][6]`` vs ``[7]``), so a quiz-only check would not cover it.
    """

    @pytest.mark.asyncio
    async def test_generate_flashcards_default(self, client, generation_notebook_id):
        """An omitted ``difficulty`` reaches the wire as the documented default.

        ``quantity=MORE(3)`` with ``difficulty`` left unset stores ``[3, 2]``:
        asymmetric, and it pins the ``None`` → ``MEDIUM`` coercion the builders
        apply (#2196) as an observable fact rather than a claim in a docstring.
        """
        result = await client.artifacts.generate_flashcards(
            generation_notebook_id,
            quantity=QuizQuantity.MORE,
        )
        assert_generation_started(result)
        options = await read_back_option_pair(
            client, generation_notebook_id, result.task_id, family="flashcards"
        )
        assert options.quantity == QuizQuantity.MORE
        assert options.difficulty == QuizDifficulty.MEDIUM

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_flashcards_with_options(self, client, generation_notebook_id):
        """``STANDARD`` + ``HARD`` — asymmetric, unlike the ``MEDIUM`` it replaced."""
        result = await client.artifacts.generate_flashcards(
            generation_notebook_id,
            quantity=QuizQuantity.STANDARD,
            difficulty=QuizDifficulty.HARD,
            instructions="Create cards for vocabulary terms",
        )
        assert_generation_started(result)
        options = await read_back_option_pair(
            client, generation_notebook_id, result.task_id, family="flashcards"
        )
        assert options.quantity == QuizQuantity.STANDARD
        assert options.difficulty == QuizDifficulty.HARD


@requires_auth
class TestInfographicGeneration:
    """Infographic generation tests."""

    @pytest.mark.asyncio
    async def test_generate_infographic_default(self, client, generation_notebook_id):
        """Test infographic generation with non-default orientation to verify param encoding."""
        result = await client.artifacts.generate_infographic(
            generation_notebook_id,
            orientation=InfographicOrientation.PORTRAIT,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_infographic_portrait_detailed(self, client, generation_notebook_id):
        result = await client.artifacts.generate_infographic(
            generation_notebook_id,
            orientation=InfographicOrientation.PORTRAIT,
            detail_level=InfographicDetail.DETAILED,
            instructions="Include statistics and key findings",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_infographic_square_concise(self, client, generation_notebook_id):
        result = await client.artifacts.generate_infographic(
            generation_notebook_id,
            orientation=InfographicOrientation.SQUARE,
            detail_level=InfographicDetail.CONCISE,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_infographic_landscape(self, client, generation_notebook_id):
        result = await client.artifacts.generate_infographic(
            generation_notebook_id,
            orientation=InfographicOrientation.LANDSCAPE,
        )
        assert_generation_started(result)


@requires_auth
class TestSlideDeckGeneration:
    """Slide deck generation tests."""

    @pytest.mark.asyncio
    async def test_generate_slide_deck_default(self, client, generation_notebook_id):
        """Test slide deck generation with non-default format to verify param encoding."""
        result = await client.artifacts.generate_slide_deck(
            generation_notebook_id,
            slide_format=SlideDeckFormat.PRESENTER_SLIDES,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_slide_deck_detailed(self, client, generation_notebook_id):
        result = await client.artifacts.generate_slide_deck(
            generation_notebook_id,
            slide_format=SlideDeckFormat.DETAILED_DECK,
            slide_length=SlideDeckLength.DEFAULT,
            instructions="Include speaker notes",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_slide_deck_presenter_short(self, client, generation_notebook_id):
        result = await client.artifacts.generate_slide_deck(
            generation_notebook_id,
            slide_format=SlideDeckFormat.PRESENTER_SLIDES,
            slide_length=SlideDeckLength.SHORT,
        )
        assert_generation_started(result)


@requires_auth
class TestDataTableGeneration:
    """Data table generation tests."""

    @pytest.mark.asyncio
    async def test_generate_data_table_default(self, client, generation_notebook_id):
        """Test data table generation with instructions to verify param encoding."""
        result = await client.artifacts.generate_data_table(
            generation_notebook_id,
            instructions="Create a comparison table",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_data_table_with_instructions(self, client, generation_notebook_id):
        result = await client.artifacts.generate_data_table(
            generation_notebook_id,
            instructions="Create a comparison table of key concepts",
            language="en",
        )
        assert_generation_started(result)


@requires_auth
class TestMindMapGeneration:
    """Mind map generation tests."""

    @pytest.mark.asyncio
    async def test_generate_mind_map(self, client, generation_notebook_id, generation_journal):
        """Mind map generation is fast (~5-10s), not slow."""
        # Clean up old mind maps to prevent accumulation from nightly runs
        existing_mind_maps = await client.notes.list_mind_maps(generation_notebook_id)
        for mm in existing_mind_maps:
            recovery = generation_journal.recovery_operation(
                resource_id=mm[0],
                notebook_id=generation_notebook_id,
                family="mind_map",
                surface="client",
                id_kind="note_mind_map",
                reason="retry_preclean",
            )
            await client.notes.delete_mind_map(generation_notebook_id, mm[0])
            remaining = {
                row[0] for row in await client.notes.list_mind_maps(generation_notebook_id)
            }
            assert mm[0] not in remaining
            recovery.delete_confirmed(mm[0], reason="retry_preclean")

        operation = generation_journal.operation(
            notebook_id=generation_notebook_id,
            family="mind_map",
            surface="client",
            id_kind="note_mind_map",
            lifecycle="settle",
        )
        result = await generate_note_mind_map(client, generation_notebook_id, operation)
        assert result is not None
        assert result.note_id is not None
        operation.persisted(result.note_id)
        # Verify mind map structure
        mind_map = result.mind_map
        assert isinstance(mind_map, dict)
        assert "name" in mind_map
        assert "children" in mind_map
        operation.completed(result.note_id)


@requires_auth
class TestStudyGuideGeneration:
    """Study guide generation tests."""

    @pytest.mark.asyncio
    async def test_generate_study_guide(self, client, generation_notebook_id):
        """Test study guide generation."""
        result = await client.artifacts.generate_study_guide(generation_notebook_id)
        assert_generation_started(result)
