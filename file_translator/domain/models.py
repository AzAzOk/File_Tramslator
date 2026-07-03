"""Core domain models for document translation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LanguageCode(Enum):
    """Supported language codes."""
    
    RU = "ru"  # Russian
    EN = "en"  # English
    SR = "sr"  # Serbian
    ZH = "zh"  # Chinese
    DETECT = "auto"  # Auto-detect language
    
    @classmethod
    def from_string(cls, code: str) -> LanguageCode:
        """Create language code from string representation."""
        # Map lowercase codes to enum members by value
        reverse_map = {member.value: member for member in cls}
        normalized = code.lower().replace("auto", "auto")
        if normalized not in reverse_map:
            raise ValueError(f"Unsupported language code: {code}")
        return reverse_map[normalized]
    
    @classmethod
    def is_supported(cls, code: str) -> bool:
        """Check if a language code is supported (including auto)."""
        return code.lower() in {member.value for member in cls}


class DocumentFormat(Enum):
    """Supported document formats."""
    
    DOCX = "docx"
    DOC = "doc"
    PDF = "pdf"
    XLSX = "xlsx"
    XLS = "xls"
    DWG = "dwg"
    DXF = "dxf"


class TranslationStyle(Enum):
    """Translation style for domain-specific language."""
    
    TECHNICAL = "technical"
    LEGAL = "legal"
    MIXED = "mixed"


class TranslationMode(Enum):
    """Translation mode for controlling which text gets translated."""
    
    FULL = "full"                      # Translate all text content
    FILTER_BY_SOURCE = "filter_source"  # Only translate text matching source language


@dataclass(frozen=True)
class TextUnit:
    """Represents a single unit of text to be translated.
    
    This class encapsulates all metadata needed for translation
    while preserving the original document structure.
    """
    
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    path: str = ""  # XML path within the document
    original_text: str = ""
    translated_text: str = ""
    context: str = ""  # Surrounding text for better translation
    metadata: dict[str, Any] = field(default_factory=dict)
    
    @property
    def needs_translation(self) -> bool:
        """Check if this text unit requires translation."""
        return bool(self.original_text.strip()) and not self.translated_text
    
    @property
    def is_translatable(self) -> bool:
        """Check if the text unit contains translatable content."""
        stripped = self.original_text.strip()
        # Skip pure whitespace, numbers only, or XML tags
        return bool(stripped) and not all(c.isdigit() or c.isspace() for c in stripped)


@dataclass
class TranslationRequest:
    """Request payload for document translation."""
    
    source_language: LanguageCode
    target_language: LanguageCode
    translation_style: TranslationStyle = TranslationStyle.TECHNICAL
    translation_mode: TranslationMode = TranslationMode.FULL
    text_units: list[TextUnit] = field(default_factory=list)
    batch_size: int = 50
    preserve_formatting: bool = True
    use_glossary: bool = False
    glossary_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TranslationResult:
    """Result of a translation operation."""
    
    success: bool
    text_units_translated: int = 0
    total_text_units: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    output_path: str = ""
    raw_response: dict | None = None
    
    @property
    def is_complete(self) -> bool:
        """Check if translation was fully successful."""
        return self.success and not self.errors


@dataclass
class DocumentMetadata:
    """Metadata extracted from a document."""
    
    format_version: str = ""
    page_count: int = 0
    word_count: int = 0
    character_count: int = 0
    has_tables: bool = False
    has_headers: bool = False
    has_footers: bool = False
    has_notes: bool = False
    author: str = ""
    created_date: str = ""
    modified_date: str = ""


class TranslationBatch:
    """A batch of text units for LLM translation."""
    
    def __init__(self, sequence_id: int, text_units: list, source_language: LanguageCode,
                 target_language: LanguageCode, translation_style: TranslationStyle = TranslationStyle.TECHNICAL,
                 translation_mode: TranslationMode = TranslationMode.FULL,
                 use_glossary: bool = False, glossary_id: str = ""):
        self.sequence_id = sequence_id
        self.text_units = text_units
        self.source_language = source_language
        self.target_language = target_language
        self.translation_style = translation_style
        self.translation_mode = translation_mode
        self.use_glossary = use_glossary
        self.glossary_id = glossary_id
