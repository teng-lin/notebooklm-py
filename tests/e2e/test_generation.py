"""Artifact generation tests.

All artifact generation tests consolidated here. These tests:
- Use `generation_notebook_id` fixture (auto-created, has content)
- Variant tests are marked @pytest.mark.variants (skip to save quota)

Notebook lifecycle:
- Auto-created on first run if NOTEBOOKLM_GENERATION_NOTEBOOK_ID not set
- Artifacts/notes cleaned BEFORE tests to ensure clean state
- In CI (CI=true): notebook deleted after tests to avoid orphans
- Locally: notebook persists for verification, ID stored in NOTEBOOKLM_HOME
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

from .conftest import assert_generation_started, requires_auth


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
class TestQuizGeneration:
    """Quiz generation tests."""

    @pytest.mark.asyncio
    async def test_generate_quiz_default(self, client, generation_notebook_id):
        """Test quiz generation with non-default difficulty to verify param encoding."""
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            difficulty=QuizDifficulty.HARD,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_quiz_with_options(self, client, generation_notebook_id):
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            quantity=QuizQuantity.MORE,
            difficulty=QuizDifficulty.HARD,
            instructions="Focus on key concepts and definitions",
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_quiz_fewer_easy(self, client, generation_notebook_id):
        result = await client.artifacts.generate_quiz(
            generation_notebook_id,
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.EASY,
        )
        assert_generation_started(result)


@requires_auth
class TestFlashcardsGeneration:
    """Flashcards generation tests."""

    @pytest.mark.asyncio
    async def test_generate_flashcards_default(self, client, generation_notebook_id):
        """Test flashcards generation with non-default quantity to verify param encoding."""
        result = await client.artifacts.generate_flashcards(
            generation_notebook_id,
            quantity=QuizQuantity.MORE,
        )
        assert_generation_started(result)

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_flashcards_with_options(self, client, generation_notebook_id):
        result = await client.artifacts.generate_flashcards(
            generation_notebook_id,
            quantity=QuizQuantity.STANDARD,
            difficulty=QuizDifficulty.MEDIUM,
            instructions="Create cards for vocabulary terms",
        )
        assert_generation_started(result)


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
    async def test_generate_mind_map(self, client, generation_notebook_id):
        """Mind map generation is fast (~5-10s), not slow."""
        result = await client.artifacts.generate_mind_map(generation_notebook_id)
        assert result is not None
        assert "mind_map" in result
        assert "note_id" in result
        # Verify mind map structure
        mind_map = result["mind_map"]
        assert isinstance(mind_map, dict)
        assert "name" in mind_map
        assert "children" in mind_map


@requires_auth
class TestStudyGuideGeneration:
    """Study guide generation tests."""

    @pytest.mark.asyncio
    async def test_generate_study_guide(self, client, generation_notebook_id):
        """Test study guide generation."""
        result = await client.artifacts.generate_study_guide(generation_notebook_id)
        assert_generation_started(result)
