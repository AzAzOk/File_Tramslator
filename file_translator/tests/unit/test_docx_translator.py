"""Unit tests for DocxTranslator."""

import re
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from file_translator.domain.models import DocumentFormat
from file_translator.infrastructure.translators.docx_translator import DocxTranslator


class TestDocxTranslator:
    """Tests for DocxTranslator implementation."""
    
    def test_supported_formats(self):
        translator = DocxTranslator()
        formats = translator.supported_formats()
        
        assert DocumentFormat.DOCX in formats
    
    def test_can_process_nonexistent_file(self):
        translator = DocxTranslator()
        result = translator.can_process(Path("/nonexistent/file.docx"))
        
        assert not result
    
    @patch('zipfile.ZipFile')
    def test_can_process_valid_docx(self, mock_zipfile):
        """Test that valid DOCX files are recognized."""
        mock_zip = MagicMock()
        mock_zip.namelist.return_value = [
            "word/document.xml",
            "[Content_Types].xml",
            "_rels/.rels",
        ]
        mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zip)
        
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            tmp = Path(f.name)
        try:
            translator = DocxTranslator()
            result = translator.can_process(tmp)
            assert result
        finally:
            tmp.unlink(missing_ok=True)
    
    @patch('zipfile.ZipFile')
    def test_can_process_invalid_file(self, mock_zipfile):
        """Test that non-DOCX files are rejected."""
        mock_zip = MagicMock()
        mock_zip.namelist.return_value = [
            "readme.txt",
            "image.png",
        ]
        mock_zipfile.return_value.__enter__ = MagicMock(return_value=mock_zip)
        
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            tmp = Path(f.name)
        try:
            translator = DocxTranslator()
            result = translator.can_process(tmp)
            assert not result
        finally:
            tmp.unlink(missing_ok=True)





class TestStripMethods:
    """Tests for Qwen3 response stripping methods."""
    
    def _make_t_elements(self, texts: list[str]) -> list[ET.Element]:
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        elems = []
        for t in texts:
            e = ET.Element(f"{{{ns}}}t")
            e.text = t
            elems.append(e)
        return elems
    
    def _make_provider(self):
        from file_translator.infrastructure.config import LLMConfig
        from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider
        return OpenAITranslationProvider(LLMConfig())
    
    def test_strip_think_block_present(self):
        """<think> block is removed."""
        provider = self._make_provider()
        text = "<think>I need to translate this.</think><s1>text</s1>"
        result = provider._strip_think_block(text)
        
        assert "think>" not in result
        assert "<s1>text</s1>" in result
    
    def test_strip_think_block_absent(self):
        """No change when no think block."""
        provider = self._make_provider()
        text = "<s1>text</s1>"
        result = provider._strip_think_block(text)
        assert result == "<s1>text</s1>"
    
    def test_strip_markdown_fences(self):
        """Markdown code fences are removed."""
        provider = self._make_provider()
        text = '```json\n{"key": "value"}\n```'
        result = provider._strip_markdown_fences(text)
        assert result == '{"key": "value"}'
    
    def test_strip_both_combined(self):
        """Both think block and fences are stripped."""
        provider = self._make_provider()
        text = '<think>reasoning</think>\n```json\n{"key": "value"}\n```'
        result = provider._strip_think_block(text)
        result = provider._strip_markdown_fences(result)
        assert result == '{"key": "value"}'
