"""DXF parser — reads DXF files into domain models.

This module provides two parsers:

1. ``DxfParser`` (BACKWARD-COMPATIBLE) — returns ``DxfDocument``.
   Used by the existing ``DxfTranslator`` via ``DocumentTranslator``.

2. ``DxfDocumentParser`` (NEW) — implements ``IParser``, returns ``Document``.
   Used by the new pipeline via ``FormatRegistry``.

Both use ``EzdxfBackend`` internally for actual DXF I/O.
"""

from __future__ import annotations

import logging
from pathlib import Path

from file_translator.domain.document_model import (
    Document,
    EntityType,
    TranslatableEntity,
    TranslationStatus,
)
from file_translator.domain.dxf_models import (
    DxfBlock,
    DxfDimension,
    DxfDocument,
    DxfEntity,
    DxfEntityType,
    DxfLayer,
    DxfTextEntity,
    DxfTextPosition,
    DxfTextProperties,
)
from file_translator.domain.interfaces import IParser
from file_translator.infrastructure.backends.ezdxf_backend import EzdxfBackend
from file_translator.infrastructure.classifiers.cad_token_protector import CadTokenProtector

logger = logging.getLogger(__name__)


# ── shared helpers ──

_dxf_type_to_entity_type: dict[str, DxfEntityType] = {
    "TEXT": DxfEntityType.TEXT,
    "MTEXT": DxfEntityType.MTEXT,
    "ATTRIB": DxfEntityType.ATTRIB,
    "ATTDEF": DxfEntityType.ATTDEF,
    "DIMENSION": DxfEntityType.DIMENSION,
}


# ── backward-compatible DxfParser (returns DxfDocument) ──


class DxfParseError(Exception):
    """Raised when DXF parsing fails."""
    pass


class DxfParser:
    """Backward-compatible parser returning ``DxfDocument``.

    Used by ``DxfTranslator`` (the old ``DocumentTranslator``-based path).
    """

    def __init__(self) -> None:
        self._backend = EzdxfBackend()

    def parse(self, file_path: str | Path) -> DxfDocument:
        """Parse a DXF file into a ``DxfDocument``."""
        path = Path(file_path)
        if not path.exists():
            raise DxfParseError(f"DXF file not found: {path}")
        if path.suffix.lower() not in (".dxf", ".dwg"):
            raise DxfParseError(f"Unsupported format: {path.suffix}")

        return self._parse_to_dxf_doc(path)

    def _parse_to_dxf_doc(self, path: Path) -> DxfDocument:
        dxf_doc = self._backend.open(path)
        doc = DxfDocument(file_path=str(path.absolute()))
        doc.format_version = dxf_doc.dxfversion

        text_entities: list[DxfTextEntity] = []
        dimensions: list[DxfDimension] = []

        for raw_entity, source in self._backend.iter_entities(dxf_doc):
            text = self._backend.get_text(raw_entity)
            if not text.strip():
                continue

            handle = self._backend.get_handle(raw_entity)
            layer = self._backend.get_layer(raw_entity)
            dxf_type = raw_entity.dxftype() if hasattr(raw_entity, "dxftype") else "TEXT"

            entity_type = _dxf_type_to_entity_type.get(dxf_type, DxfEntityType.TEXT)

            if dxf_type == "DIMENSION":
                dim = DxfDimension(
                    handle=handle,
                    layer=layer,
                    original_text=text,
                )
                dimensions.append(dim)
            else:
                te = DxfTextEntity(
                    handle=handle,
                    layer=layer,
                    entity_type=entity_type,
                    original_text=text,
                )
                text_entities.append(te)

        doc.entities = text_entities + dimensions
        logger.info("Parsed %d entities from %s", len(doc.entities), path.name)
        return doc

    def validate_structure(self, file_path: str | Path) -> bool:
        """Quick validation that the file is a valid DXF."""
        path = Path(file_path)
        if not path.exists() or path.suffix.lower() != ".dxf":
            return False
        try:
            self._backend.open(path)
            return True
        except Exception:
            return False


# ── new IParser implementation (returns Document) ──


class DxfDocumentParser(IParser):
    """IParser implementation for DXF, returns universal ``Document``.

    Registered in ``FormatRegistry`` for use by the new pipeline.
    """

    def __init__(self) -> None:
        self._backend = EzdxfBackend()

    def parse(self, path: Path) -> Document:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"DXF file not found: {path}")

        dxf_doc = self._backend.open(path)
        entities: list[TranslatableEntity] = []
        seen_texts: dict[str, TranslatableEntity] = {}
        counter = 0
        protector = CadTokenProtector()

        for raw_entity, source in self._backend.iter_entities(dxf_doc):
            text = self._backend.get_text(raw_entity)
            if not text.strip():
                continue

            handle = self._backend.get_handle(raw_entity)
            layer = self._backend.get_layer(raw_entity)
            dxf_type = raw_entity.dxftype() if hasattr(raw_entity, "dxftype") else "TEXT"

            # Encode MTEXT format codes before batching
            encoded_text, tokens = protector.encode(text, entity_id=handle)

            # Dedup identical text → group handles
            if encoded_text in seen_texts:
                seen_texts[encoded_text].handles.append(handle)
                continue

            entity_id = f"dxf_{counter}"
            counter += 1

            et = self._map_type(dxf_type)
            te = TranslatableEntity(
                id=entity_id,
                handles=[handle],
                type=et,
                text=encoded_text,
                translation_status=TranslationStatus.PENDING,
                protected_tokens=tokens,
                metadata={
                    "dxf_type": dxf_type,
                    "layer": layer,
                    "source": source,
                    "handle": handle,
                },
            )
            entities.append(te)
            seen_texts[encoded_text] = te

        return Document(
            schema_version="1.0",
            metadata={
                "file_path": str(path.absolute()),
                "format": "DXF",
                "format_version": dxf_doc.dxfversion,
                "entity_count": len(entities),
            },
            entities=entities,
        )

    def capabilities(self) -> set[str]:
        return {"blocks", "attributes", "tables"}

    @staticmethod
    def _map_type(dxf_type: str) -> EntityType:
        return {
            "TEXT": EntityType.TEXT,
            "MTEXT": EntityType.MTEXT,
            "ATTRIB": EntityType.ATTRIB,
            "ATTDEF": EntityType.ATTDEF,
            "DIMENSION": EntityType.DIMENSION,
            "PROXY": EntityType.TABLE_CELL,
        }.get(dxf_type, EntityType.TEXT)
