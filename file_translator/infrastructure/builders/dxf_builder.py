"""DxfBuilder — writes DxfDocument back to DXF using ezdxf.

Existing class kept for backward compatibility with DxfTranslator.
New code should use DxfParser + DxfUpdater directly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import ezdxf

from file_translator.domain.dxf_models import (
    DxfDocument,
    DxfTextEntity,
    DxfDimension,
)
from file_translator.infrastructure.backends.ezdxf_backend import EzdxfBackend

logger = logging.getLogger(__name__)


class DxfBuildError(Exception):
    """Raised when DXF building fails."""
    pass


class DxfBuilder:
    """Writes a DxfDocument (with translations applied) to a DXF file."""

    def __init__(self) -> None:
        self._backend = EzdxfBackend()

    def build(self, document: DxfDocument, output_path: str | Path) -> str:
        """Write the translated DXF document."""
        path = Path(output_path)
        source_path = Path(document.file_path)

        if not source_path.exists():
            raise DxfBuildError(f"Source DXF not found: {source_path}")

        dxf_doc = self._backend.open(source_path)

        # Apply translations via handle
        handle_map: dict[str, str] = {}
        for entity in document.get_all_texts():
            if entity.translated_text and entity.handle:
                handle_map[entity.handle] = entity.translated_text

        applied = 0
        for raw_entity, _ in self._backend.iter_entities(dxf_doc):
            handle = self._backend.get_handle(raw_entity)
            if handle in handle_map:
                self._backend.set_text(raw_entity, handle_map[handle])
                applied += 1

        actual_path = self._backend.save(dxf_doc, path)
        logger.info("DXF saved: %s (%d updates)", path.name, applied)
        return str(actual_path)
