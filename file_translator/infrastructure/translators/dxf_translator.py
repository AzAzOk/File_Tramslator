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
from file_translator.infrastructure.classifiers.cad_token_protector import CadTokenProtector

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
        
        Decodes CadTokenProtector placeholders before writing translated text
        back to entities, restoring MTEXT format codes (\\P, \\H, {\\f...}).
        
        Args:
            extracted_data: Original extract() output (includes dxf_document).
            translations: Map of unit_id -> translated_text.
        """
        doc = extracted_data.get("dxf_document")
        if not doc:
            logger.warning("No DXF document in extracted_data, cannot translate")
            return {"dxf_document": None}
        
        protector = CadTokenProtector()
        
        # Build token lookup from TextUnits stored in extracted_data
        text_units = extracted_data.get("text_units", [])
        tokens_by_id: dict[str, list[dict[str, str]]] = {}
        for unit in text_units:
            cad_tokens = unit.metadata.get("cad_tokens", [])
            if cad_tokens:
                tokens_by_id[unit.id] = cad_tokens
        
        for entity in doc.get_all_texts():
            entity_id = entity.id
            if entity_id in translations:
                translated = translations[entity_id]
                # Decode cad tokens if they were encoded during extraction
                cad_tokens = tokens_by_id.get(entity_id, [])
                if cad_tokens:
                    translated = protector.decode(translated, cad_tokens)
                if isinstance(entity, DxfTextEntity):
                    entity.translated_text = translated
                elif isinstance(entity, DxfDimension):
                    entity.translated_text = translated
        
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
        """Convert DXF entities into TextUnit list for translation pipeline.
        
        MTEXT format codes (\\P, \\H, {\\f...} etc.) are encoded via
        CadTokenProtector before batching so they don't break LLM JSON output.
        Tokens are stored in TextUnit.metadata["cad_tokens"] for decode after translation.
        Entities where all content is formatting codes only (no real text)
        are skipped entirely — they never reach the LLM.
        """
        protector = CadTokenProtector()
        text_units = []
        skipped_placeholder_only = 0
        
        for entity in doc.get_text_entities():
            encoded_text, tokens = protector.encode(entity.original_text, entity_id=entity.id)
            if not protector.has_translatable_content(encoded_text):
                skipped_placeholder_only += 1
                continue
            preview = entity.original_text[:50]
            if len(entity.original_text) > 50:
                preview += "..."
            logger.info("Extracted TEXT entity %s (layer=%s): %r",
                        entity.id, entity.layer, preview)
            unit = TextUnit(
                id=entity.id,
                original_text=encoded_text,
                context=f"dxf_layer:{entity.layer}",
                metadata={
                    "x": entity.position.x,
                    "y": entity.position.y,
                    "z": entity.position.z,
                    "entity_type": entity.entity_type.value if isinstance(entity.entity_type, DxfEntityType) else str(entity.entity_type),
                    "cad_tokens": tokens,
                },
            )
            text_units.append(unit)
        
        for dim in doc.get_dimensions():
            encoded_text, tokens = protector.encode(dim.original_text, entity_id=dim.id)
            if not protector.has_translatable_content(encoded_text):
                skipped_placeholder_only += 1
                continue
            preview = dim.original_text[:50]
            if len(dim.original_text) > 50:
                preview += "..."
            logger.info("Extracted DIMENSION entity %s (measurement=%s): %r",
                        dim.id, dim.measurement, preview)
            unit = TextUnit(
                id=dim.id,
                original_text=encoded_text,
                context=f"dxf_dimension:{dim.measurement}",
                metadata={
                    "x": dim.text_position.x,
                    "y": dim.text_position.y,
                    "entity_type": "DIMENSION",
                    "cad_tokens": tokens,
                },
            )
            text_units.append(unit)
        
        if skipped_placeholder_only:
            logger.info(f"Skipped {skipped_placeholder_only} entities with no translatable "
                        f"content (formatting codes only) — kept as-is, never sent to LLM")
        
        return text_units
