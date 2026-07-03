"""Unit tests for TranslationProvider."""

import pytest
from unittest.mock import AsyncMock, patch

from file_translator.domain.models import LanguageCode, TranslationBatch, TextUnit
from file_translator.infrastructure.config import LLMConfig
from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider


def _make_provider(**overrides) -> OpenAITranslationProvider:
    return OpenAITranslationProvider(LLMConfig(**overrides))


class TestOpenAITranslationProvider:
    """Tests for OpenAI-compatible translation provider."""
    
    def test_default_configuration(self):
        provider = _make_provider()
        
        assert provider.base_url == LLMConfig().base_url
        assert provider.model_name == LLMConfig().model_name
        assert provider.temperature == LLMConfig().temperature
    
    def test_custom_configuration(self):
        provider = _make_provider(
            base_url="http://localhost:8080/v1/chat/completions",
            model_name="custom-model:v1",
            temperature=0.7,
        )
        
        assert provider.base_url == "http://localhost:8080/v1/chat/completions"
        assert provider.model_name == "custom-model:v1"
        assert provider.temperature == 0.7
    
    @pytest.mark.asyncio
    async def test_translate_batch_invalid_json_response(self):
        """Test handling of invalid JSON response."""
        provider = _make_provider()
        
        # Create a simple batch for testing
        text_unit = TextUnit(id="test-1", original_text="Hello")
        batch = TranslationBatch(
            sequence_id=1,
            text_units=[text_unit],
            source_language=LanguageCode.EN,
            target_language=LanguageCode.RU,
        )
        
        batch_data = {
            "batch": batch,
            "source_language": LanguageCode.EN,
            "target_language": LanguageCode.RU,
        }
        
        # Mock the HTTP response with invalid JSON (not parseable even after fixes)
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = "not JSON at all"
        mock_response.json = lambda: {"choices": [{"message": {"content": "not JSON at all"}}]}
        
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        async def mock_post(*args, **kwargs):
            return mock_response
        mock_client.post = mock_post
        
        with patch('file_translator.infrastructure.providers.openai_provider.httpx.AsyncClient', return_value=mock_client):
            from file_translator.domain.errors import TranslationError
            
            with pytest.raises(TranslationError):
                await provider.translate_batch(batch_data)
