"""Unit tests for domain models."""

import pytest
from file_translator.domain.models import LanguageCode, TextUnit, TranslationRequest


class TestLanguageCode:
    """Tests for LanguageCode enum."""
    
    def test_from_string_valid(self):
        assert LanguageCode.from_string("ru") == LanguageCode.RU
        assert LanguageCode.from_string("en") == LanguageCode.EN
        assert LanguageCode.from_string("sr") == LanguageCode.SR
        assert LanguageCode.from_string("zh") == LanguageCode.ZH
    
    def test_from_string_case_insensitive(self):
        assert LanguageCode.from_string("RU") == LanguageCode.RU
        assert LanguageCode.from_string("En") == LanguageCode.EN
    
    def test_from_string_invalid(self):
        with pytest.raises(ValueError):
            LanguageCode.from_string("xx")


class TestTextUnit:
    """Tests for TextUnit data class."""
    
    def test_create_text_unit(self):
        unit = TextUnit(id="test-1", original_text="Hello world")
        
        assert unit.id == "test-1"
        assert unit.original_text == "Hello world"
        assert unit.needs_translation
        assert unit.is_translatable
    
    def test_needs_translation_flag(self):
        unit = TextUnit(id="test-2", original_text="Test text")
        
        assert unit.needs_translation
        
        translated = TextUnit(
            id=unit.id, original_text=unit.original_text,
            translated_text="Тестовый текст"
        )
        assert not translated.needs_translation
    
    def test_is_translatable_empty_text(self):
        unit = TextUnit(id="test-3", original_text="   ")
        assert not unit.is_translatable


class TestTranslationRequest:
    """Tests for TranslationRequest data class."""
    
    def test_create_request(self):
        request = TranslationRequest(
            source_language=LanguageCode.EN,
            target_language=LanguageCode.RU,
            batch_size=50,
        )
        
        assert request.source_language == LanguageCode.EN
        assert request.target_language == LanguageCode.RU
        assert request.batch_size == 50
