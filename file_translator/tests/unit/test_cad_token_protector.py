"""Unit tests for CadTokenProtector — MTEXT format code encoding/decoding."""

import pytest

from file_translator.infrastructure.classifiers.cad_token_protector import (
    CadTokenProtector,
    _FMT_PATTERN,
)


class TestFmtPattern:
    """Verify _FMT_PATTERN matches MTEXT codes correctly."""

    def test_height_with_semicolon(self):
        """\\H2.5x; must match as one token, not \\H + 2.5x;"""
        m = _FMT_PATTERN.search("\\H2.5x;")
        assert m is not None
        assert m.group(0) == "\\H2.5x;"

    def test_height_bare(self):
        """\\H alone (no semicolon) must still match."""
        m = _FMT_PATTERN.search("text\\Hmore")
        assert m is not None
        assert m.group(0) == "\\H"

    def test_newline(self):
        m = _FMT_PATTERN.search("\\P")
        assert m is not None
        assert m.group(0) == "\\P"

    def test_stacked_text(self):
        """\\S1/2; must match as one token."""
        m = _FMT_PATTERN.search("\\S1/2;")
        assert m is not None
        assert m.group(0) == "\\S1/2;"

    def test_font_block(self):
        """Font block without closing } won't match."""
        m = _FMT_PATTERN.search("{\\fArial|b0|i0;")
        assert m is None

    def test_font_block_with_closing_brace(self):
        """{\\fArial|b0|i0;Text} — matches full block."""
        m = _FMT_PATTERN.search("{\\fArial|b0|i0;Text}")
        assert m is not None
        assert m.group(0) == "{\\fArial|b0|i0;Text}"

    def test_alignment_bare(self):
        """\\A alone (no digit) must still match."""
        m = _FMT_PATTERN.search("text\\Arest")
        assert m is not None
        assert m.group(0) == "\\A"

    def test_alignment_with_digit(self):
        """\\A1; must match as one token — digit+; consumed together."""
        m = _FMT_PATTERN.search("\\A1;")
        assert m is not None
        assert m.group(0) == "\\A1;"

    def test_alignment_all_values(self):
        """\\A0;, \\A1;, \\A2; — each must match fully."""
        for value in ("\\A0;", "\\A1;", "\\A2;"):
            m = _FMT_PATTERN.search(value)
            assert m is not None, f"{value} did not match"
            assert m.group(0) == value, f"{value} matched as {m.group(0)}"

    def test_alignment_with_following_text(self):
        """\\A1;Отметка must not leak the digit/; into surrounding text."""
        m = _FMT_PATTERN.search("\\A1;Отметка")
        assert m is not None
        assert m.group(0) == "\\A1;"
        # 'Отметка' is not consumed
        assert m.end() == 4  # after '\\A1;'

    def test_width_factor(self):
        m = _FMT_PATTERN.search("\\W0.75;")
        assert m is not None
        assert m.group(0) == "\\W0.75;"

    def test_nonbreaking_space(self):
        m = _FMT_PATTERN.search("\\~")
        assert m is not None
        assert m.group(0) == "\\~"


class TestEncodeDecode:
    """Encode/decode round-trip tests on real MTEXT strings."""

    def setup_method(self):
        self.protector = CadTokenProtector()

    def test_simple_newline(self):
        text = "Line1\\PLine2"
        encoded, tokens = self.protector.encode(text)
        assert "\\P" not in encoded
        assert len(tokens) == 1
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_height_with_value(self):
        text = "Big\\H2.5x;Text"
        encoded, tokens = self.protector.encode(text)
        assert "\\H" not in encoded
        assert len(tokens) == 1
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_font_block_and_newline(self):
        text = "{\\fArial|b0|i0;Диаметр\\H0.7x;\\PУсловный проход}"
        encoded, tokens = self.protector.encode(text)
        assert "\\f" not in encoded
        assert "\\H" not in encoded
        assert "\\P" not in encoded
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_multiple_format_codes(self):
        text = "Before\\PMiddle\\H1.5x;After\\Q30;End"
        encoded, tokens = self.protector.encode(text)
        # Should have 4 tokens: \P, \H1.5x;, \Q, \Q30;
        # Actually: \P, \H1.5x;, \Q, \Q30; = 4 tokens
        assert len(tokens) >= 3
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_plain_text_untouched(self):
        text = "Hello World — no format codes here"
        encoded, tokens = self.protector.encode(text)
        assert encoded == text
        assert len(tokens) == 0

    def test_empty_text(self):
        encoded, tokens = self.protector.encode("")
        assert encoded == ""
        assert len(tokens) == 0

    def test_entity_id_in_placeholder(self):
        text = "\\P"
        encoded, tokens = self.protector.encode(text, entity_id="ent42")
        assert "[[FMT_ent42_0]]" in encoded
        assert tokens[0]["placeholder"] == "[[FMT_ent42_0]]"

    def test_round_trip_complex_mtext(self):
        """Full MTEXT string with mixed codes — the exact pattern that
        was breaking LLM JSON output."""
        text = (
            "{\\fArial|b0|i0;Заголовок}"
            "\\H2.5x;"
            "Текст\\P"
            "\\A1;"
            "Выравнивание\\S1/2;"
            "дробь\\W0.8;"
            "конец"
        )
        encoded, tokens = self.protector.encode(text)
        # No raw format codes should remain in encoded text
        assert "\\f" not in encoded
        assert "\\H" not in encoded
        assert "\\P" not in encoded
        assert "\\A" not in encoded
        assert "\\S" not in encoded
        assert "\\W" not in encoded
        # Decode restores exactly
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_decode_preserves_order(self):
        """Multiple tokens of the same type must decode to correct positions."""
        text = "First\\PSecond\\PThird"
        encoded, tokens = self.protector.encode(text)
        assert len(tokens) == 2
        # Both placeholders are [[FMT_..._0]] and [[FMT_..._1]]
        restored = self.protector.decode(encoded, tokens)
        assert restored == text
        assert restored.count("\\P") == 2

    def test_alignment_with_following_text(self):
        """\\A1;Отметка -0.150 — entire \\A1; goes into one placeholder, no leakage."""
        text = "\\A1;Отметка -0.150"
        encoded, tokens = self.protector.encode(text)
        assert "\\A" not in encoded
        # '1;' must NOT leak into encoded text
        assert "1;" not in encoded
        assert len(tokens) == 1
        assert tokens[0]["original"] == "\\A1;"
        restored = self.protector.decode(encoded, tokens)
        assert restored == text

    def test_alignment_all_values_round_trip(self):
        """\\A0;, \\A1;, \\A2; — encode/decode round-trip for each."""
        for value in ("\\A0;", "\\A1;", "\\A2;"):
            text = f"{value}Отметка"
            encoded, tokens = self.protector.encode(text)
            assert "\\A" not in encoded
            assert len(tokens) == 1
            restored = self.protector.decode(encoded, tokens)
            assert restored == text, f"Round-trip failed for {value}"

    def test_alignment_in_complex_mtext(self):
        """\\A1; inside a complex MTEXT string — all codes protected."""
        text = "Header\\P\\A1;Отметка -0.150\\H0.7x; (отн.)"
        encoded, tokens = self.protector.encode(text)
        assert "\\A" not in encoded
        assert "\\P" not in encoded
        assert "\\H" not in encoded
        restored = self.protector.decode(encoded, tokens)
        assert restored == text
