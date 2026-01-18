"""E2E tests for settings operations."""

import pytest

from .conftest import requires_auth


@requires_auth
class TestSettingsLanguage:
    """Tests for language settings operations."""

    @pytest.mark.asyncio
    async def test_get_output_language(self, client):
        """Test getting current language setting."""
        result = await client.settings.get_output_language()
        # Result can be None (not set) or a language code string
        assert result is None or isinstance(result, str)

    @pytest.mark.asyncio
    async def test_set_and_get_language(self, client):
        """Test setting and then getting language."""
        # First get current language to restore later
        original_lang = await client.settings.get_output_language()

        try:
            # Set to a different language
            test_lang = "zh_Hans"
            result = await client.settings.set_output_language(test_lang)
            # Server may return the set language or None
            # The important thing is no error is raised
            if result is not None:
                assert result == test_lang

            # Verify it was set
            current = await client.settings.get_output_language()
            assert current == test_lang

        finally:
            # Restore original language
            if original_lang:
                await client.settings.set_output_language(original_lang)
            else:
                # Set to English as default
                await client.settings.set_output_language("en")

    @pytest.mark.asyncio
    async def test_set_language_to_english(self, client):
        """Test setting language to English."""
        result = await client.settings.set_output_language("en")
        # Should work without error
        if result is not None:
            assert result == "en"

    @pytest.mark.asyncio
    async def test_set_language_to_japanese(self, client):
        """Test setting language to Japanese."""
        # Get current to restore
        original = await client.settings.get_output_language()

        try:
            result = await client.settings.set_output_language("ja")
            if result is not None:
                assert result == "ja"
        finally:
            # Restore
            restore_lang = original if original else "en"
            await client.settings.set_output_language(restore_lang)

    @pytest.mark.asyncio
    async def test_set_language_with_region(self, client):
        """Test setting language with regional variant."""
        # Get current to restore
        original = await client.settings.get_output_language()

        try:
            # Brazilian Portuguese
            result = await client.settings.set_output_language("pt_BR")
            if result is not None:
                assert result == "pt_BR"
        finally:
            # Restore
            restore_lang = original if original else "en"
            await client.settings.set_output_language(restore_lang)


@requires_auth
class TestSettingsLanguagePersistence:
    """Tests for language settings persistence across sessions."""

    @pytest.mark.asyncio
    async def test_language_persists_across_client_sessions(self, auth_tokens):
        """Test that language setting persists when creating new client."""
        from notebooklm import NotebookLMClient

        original = None

        try:
            # First session - set language
            async with NotebookLMClient(auth_tokens) as client1:
                original = await client1.settings.get_output_language()
                await client1.settings.set_output_language("ko")

            # Second session - verify language persisted
            async with NotebookLMClient(auth_tokens) as client2:
                current = await client2.settings.get_output_language()
                assert current == "ko"

        finally:
            # Restore original in third session
            async with NotebookLMClient(auth_tokens) as client3:
                restore_lang = original if original else "en"
                await client3.settings.set_output_language(restore_lang)
