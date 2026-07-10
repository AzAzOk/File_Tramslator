"""DXF document translator implementation.

Translates text content in DXF CAD files while preserving
all geometry, layers, blocks, and positioning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from file_translator.domain.dxf_models import (
    DxfDimension,
    DxfDocument,
    DxfEntity,
    DxfEntityType,
    DxfTextEntity,
)
from file_translator.domain.errors import (
    DocumentOpenError,
    DocumentParseError,
    SaveDocumentError,
)
from file_translator.domain.interfaces import DocumentTranslator
from file_translator.domain.models import (
    DocumentFormat,
    DocumentMetadata,
    TextUnit,
)

logger = logging.getLogger(__name__)


class DxfTranslator(DocumentTranslator):
    """Translator implementation for DXF CAD documents.
    
    Handles TEXT, MTEXT, DIMENSION, ATTRIB, ATTDEF entities.
    Preserves exact geometry, layers, blocks, and formatting.
    Uses DxfParser for reading and DxfBuilder for writing.
    """
    
    SUPPORTED_FORMATS = {DocumentFormat.DXF}
    
    def __init__(self):
        self._parser = None
        self._builder = None
        self._document: DxfDocument | None = None
    
    @property
    def parser(self):
        if not self._parser:
            from file_translator.infrastructure.parsers.dxf_parser import DxfParser
            self._parser = DxfParser()
        return self._parser
    
    @property
    def builder(self):
        if not self._builder:
            from file_translator.infrastructure.builders.dxf_builder import DxfBuilder
            self._builder = DxfBuilder()
        return self._builder
    
    @classmethod
    def supported_formats(cls) -> set[DocumentFormat]:
        return cls.SUPPORTED_FORMATS.copy()
    
    def can_process(self, file_path: Path) -> bool:
        suffix = file_path.suffix.lower()
        if suffix != ".dxf":
            return False
        # Basic DXF header validation
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                first = f.read(100)
            return "0" in first and "SECTION" in first
        except Exception:
            return False
    
    def extract(self, file_path: Path,
                source_lang: str = "en",
                target_lang: str = "ru") -> dict[str, Any]:
        """Extract text content from a DXF file.
        
        Returns dict with:
            - text_units: list of TextUnit objects
            - dxf_document: DxfDocument for write-back reference
            - metadata: DocumentMetadata
        """
        if not file_path.exists():
            raise DocumentOpenError(
                file_path=str(file_path),
                reason=f"DXF file not found: {file_path}",
            )
        
        try:
            self._document = self.parser.parse(file_path)
        except Exception as e:
            raise DocumentParseError(
                error_type="DXF",
                details=str(e),
            )
        
        text_units = self._build_text_units(self._document)
        
        metadata = DocumentMetadata(
            format_version=self._document.format_version,
            page_count=len(self._document.layers),
        )
        
        return {
            "text_units": text_units,
            "dxf_document": self._document,
            "metadata": metadata,
        }
    
    def translate(self, extracted_data: dict[str, Any],
                  translations: dict[str, str]) -> dict[str, Any]:
        """Apply translations back to the DXF document.
        
        Args:
            extracted_data: Original extract() output (includes dxf_document).
            translations: Map of unit_id -> translated_text.
        """
        doc = extracted_data.get("dxf_document")
        if not doc:
            logger.warning("No DXF document in extracted_data, cannot translate")
            return {"dxf_document": None}
        
        for entity in doc.get_all_texts():
            entity_id = entity.id
            if entity_id in translations:
                if isinstance(entity, DxfTextEntity):
                    entity.translated_text = translations[entity_id]
                elif isinstance(entity, DxfDimension):
                    entity.translated_text = translations[entity_id]
        
        return extracted_data
    
    def save(self, translated_data: dict[str, Any], output_path: Path) -> Path:
        """Save the translated DXF document.

        Args:
            translated_data: translate() output with dxf_document.
            output_path: Where to write the translated .dxf.

        Returns:
            The actual path the document was saved to (may differ from
            output_path if a fallback format was used, e.g. DWG conversion
            failure falling back to .dxf).
        """
        doc = translated_data.get("dxf_document")
        if not doc:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason="No DXF document data available",
            )

        try:
            return self.builder.build(doc, output_path)
        except Exception as e:
            raise SaveDocumentError(
                output_path=str(output_path),
                reason=str(e),
            )
        
    def _build_text_units(self, doc: DxfDocument) -> list[TextUnit]:
        """Convert DXF entities into TextUnit list for translation pipeline."""
        text_units = []
        
        for entity in doc.get_text_entities():
            unit = TextUnit(
                id=entity.id,
                original_text=entity.original_text,
                context=f"dxf_layer:{entity.layer}",
                metadata={
                    "x": entity.position.x,
                    "y": entity.position.y,
                    "z": entity.position.z,
                    "entity_type": entity.entity_type.value if isinstance(entity.entity_type, DxfEntityType) else str(entity.entity_type),
                },
            )
            text_units.append(unit)
        
        for dim in doc.get_dimensions():
            unit = TextUnit(
                id=dim.id,
                original_text=dim.original_text,
                context=f"dxf_dimension:{dim.measurement}",
                metadata={
                    "x": dim.text_position.x,
                    "y": dim.text_position.y,
                    "entity_type": "DIMENSION",
                },
            )
            text_units.append(unit)
        
        return text_units
