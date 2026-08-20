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
        
        Handles paragraph-split entities: if ``_build_text_units`` split a large
        entity into ``entity_id__pN`` children (via ``metadata["split_parent"]``),
        this method merges their translations (or originals for missing ones)
        back into a single ``\\P``-joined block and sets it on the parent entity.
        
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
        
        # ── Handle split entities (paragraph-level splitting) ──
        split_children: dict[str, list[TextUnit]] = {}
        for unit in text_units:
            parent = unit.metadata.get("split_parent")
            if parent:
                split_children.setdefault(parent, []).append(unit)
        
        handled_parents: set[str] = set()
        if split_children:
            for parent_id, children in split_children.items():
                children.sort(key=lambda u: u.metadata.get("split_index", 0))
                parts: list[str] = []
                for child in children:
                    child_id = child.id
                    if child_id in translations:
                        translated = translations[child_id]
                        cad_tokens = tokens_by_id.get(child_id, [])
                        if cad_tokens:
                            translated = protector.decode(translated, cad_tokens)
                        parts.append(translated)
                    else:
                        cad_tokens = tokens_by_id.get(child_id, [])
                        original = child.original_text
                        if cad_tokens:
                            original = protector.decode(original, cad_tokens)
                        parts.append(original)
                        logger.info(
                            "Split child %s missing from translations, "
                            "using original text as fallback", child_id
                        )
                
                merged = "\\P".join(parts)
                for entity in doc.get_all_texts():
                    if entity.id == parent_id:
                        entity.translated_text = merged
                        break
                handled_parents.add(parent_id)
            
            logger.info(
                "Merged %d split parent entities from %d children",
                len(split_children),
                sum(len(v) for v in split_children.values()),
            )
        
        # ── Handle normal (non-split) entities ──
        for entity in doc.get_all_texts():
            if entity.id in handled_parents:
                continue
            entity_id = entity.id
            if entity_id in translations:
                translated = translations[entity_id]
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
        
        Large entities (≥3 \\P paragraphs and ≥300 chars) are split into
        individual paragraph-level units (entity_id__pN) to improve LLM
        translation reliability. Missing paragraphs fall back to original text.
        """
        SPLIT_MIN_PARAGRAPHS = 3
        SPLIT_MIN_CHARS = 300

        protector = CadTokenProtector()
        text_units = []
        skipped_placeholder_only = 0
        split_count = 0

        def _add_unit(eid: str, original: str, layer: str, pos: tuple[float, float, float],
                      etype: str, context: str, measurement: str | None = None) -> None:
            nonlocal skipped_placeholder_only, split_count

            para_count = original.count("\\P")
            should_split = para_count >= SPLIT_MIN_PARAGRAPHS and len(original) >= SPLIT_MIN_CHARS

            if should_split:
                paragraphs = original.split("\\P")
                split_count += 1
                for i, para in enumerate(paragraphs):
                    child_id = f"{eid}__p{i}"
                    enc, toks = protector.encode(para, entity_id=child_id)
                    if not protector.has_translatable_content(enc):
                        continue
                    preview = para[:50]
                    if len(para) > 50:
                        preview += "..."
                    kind = "DIMENSION" if measurement is not None else "TEXT"
                    logger.info("Extracted %s split child %s (layer=%s, %d/%d): %r",
                                kind, child_id, layer, i + 1, len(paragraphs), preview)
                    text_units.append(TextUnit(
                        id=child_id,
                        original_text=enc,
                        context=context,
                        metadata={
                            "x": pos[0], "y": pos[1], "z": pos[2],
                            "entity_type": etype,
                            "cad_tokens": toks,
                            "split_parent": eid,
                            "split_index": i,
                        },
                    ))
                return

            enc, toks = protector.encode(original, entity_id=eid)
            if not protector.has_translatable_content(enc):
                skipped_placeholder_only += 1
                return

            preview = original[:50]
            if len(original) > 50:
                preview += "..."

            if measurement is not None:
                logger.info("Extracted DIMENSION entity %s (measurement=%s): %r",
                            eid, measurement, preview)
            else:
                logger.info("Extracted TEXT entity %s (layer=%s): %r",
                            eid, layer, preview)

            text_units.append(TextUnit(
                id=eid,
                original_text=enc,
                context=context,
                metadata={
                    "x": pos[0], "y": pos[1], "z": pos[2],
                    "entity_type": etype,
                    "cad_tokens": toks,
                },
            ))

        for entity in doc.get_text_entities():
            etype = (entity.entity_type.value if isinstance(entity.entity_type, DxfEntityType)
                     else str(entity.entity_type))
            _add_unit(
                eid=entity.id, original=entity.original_text,
                layer=entity.layer,
                pos=(entity.position.x, entity.position.y, entity.position.z),
                etype=etype,
                context=f"dxf_layer:{entity.layer}",
            )

        for dim in doc.get_dimensions():
            _add_unit(
                eid=dim.id, original=dim.original_text,
                layer="",
                pos=(dim.text_position.x, dim.text_position.y, 0.0),
                etype="DIMENSION",
                context=f"dxf_dimension:{dim.measurement}",
                measurement=str(dim.measurement),
            )

        if split_count:
            logger.info("Split %d large entities into paragraph-level units", split_count)
        if skipped_placeholder_only:
            logger.info(f"Skipped {skipped_placeholder_only} entities with no translatable "
                        f"content (formatting codes only) — kept as-is, never sent to LLM")
        return text_units
