"""Unit tests for language_validator module (character-set fallback path)."""

import pytest
from file_translator.infrastructure.language_validator import (
    detect_language,
    expected_language,
    validate_glossary_value,
)


class TestDetectLanguage:
    """Test character-set heuristic fallback detection."""

    def test_detect_russian_cyrillic(self):
        assert detect_language("Привет мир") == "ru"

    def test_detect_english_latin(self):
        assert detect_language("Hello world") == "en"

    def test_detect_chinese_cjk(self):
        assert detect_language("你好世界") == "zh"

    def test_detect_mixed_cyrillic_latin_cyrillic_wins(self):
        assert detect_language("Привет Hello") == "ru"

    def test_detect_empty_string(self):
        assert detect_language("") is None

    def test_detect_whitespace_only(self):
        assert detect_language("   ") is None


class TestExpectedLanguage:
    """Test column-to-language mapping."""

    def test_ru_word(self):
        assert expected_language("ru_word") == "ru"

    def test_en_word(self):
        assert expected_language("en_word") == "en"

    def test_sb_word(self):
        assert expected_language("sb_word") == "sr"

    def test_ch_word(self):
        assert expected_language("ch_word") == "zh"

    def test_unknown_column(self):
        assert expected_language("unknown") is None


class TestValidateGlossaryValue:
    """Test full validation pipeline."""

    def test_russian_in_ru_column(self):
        assert validate_glossary_value("ru_word", "Привет") is None

    def test_english_in_en_column(self):
        assert validate_glossary_value("en_word", "Hello") is None

    def test_chinese_in_ch_column(self):
        assert validate_glossary_value("ch_word", "你好") is None

    def test_english_in_ru_column_fails(self):
        error = validate_glossary_value("ru_word", "Hello")
        assert error is not None
        assert "Русское слово" in error

    def test_russian_in_en_column_fails(self):
        error = validate_glossary_value("en_word", "Привет")
        assert error is not None
        assert "Английское слово" in error

    def test_latin_in_ch_column_fails(self):
        error = validate_glossary_value("ch_word", "Hello")
        assert error is not None
        assert "Китайское слово" in error

    def test_empty_value_skips_validation(self):
        assert validate_glossary_value("ru_word", "") is None

    def test_unknown_column_skips_validation(self):
        assert validate_glossary_value("unknown", "anything") is None

    def test_numeric_mixed_with_latin_passes_en(self):
        assert validate_glossary_value("en_word", "ABC-123") is None


class TestValidateGlossaryValueSerbian:
    """Serbian (sb_word) accepts both Cyrillic and Latin."""

    def test_serbian_latin_in_sb_column(self):
        assert validate_glossary_value("sb_word", "Zdravo") is None

    def test_serbian_cyrillic_in_sb_column(self):
        assert validate_glossary_value("sb_word", "Здраво") is None

    def test_english_in_sb_column_passes(self):
        assert validate_glossary_value("sb_word", "Hello") is None
