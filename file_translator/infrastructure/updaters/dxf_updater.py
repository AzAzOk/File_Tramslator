"""DxfUpdater — IUpdater implementation for DXF via EzdxfBackend."""

from __future__ import annotations

import logging
from pathlib import Path

from file_translator.domain.document_model import Document, TranslationStatus
from file_translator.domain.interfaces import IUpdater
from file_translator.infrastructure.backends.ezdxf_backend import EzdxfBackend
from file_translator.infrastructure.classifiers.cad_token_protector import CadTokenProtector

logger = logging.getLogger(__name__)


class DxfUpdater(IUpdater):
    """Applies translations and saves DXF files."""

    def __init__(self) -> None:
        self._backend = EzdxfBackend()
        self._dxf_doc: object | None = None

    def apply(self, document: Document, translations: dict[str, str]) -> None:
        """Write translated text back into the DXF entities.

        Uses ``set_text()`` on each handled entity.  When multiple
        handles map to the same translated text, all of them are
        updated.
        """
        for entity in document.entities:
            translated = translations.get(entity.id)
            if translated is None:
                continue
            entity.translated_text = translated
            entity.translation_status = TranslationStatus.TRANSLATED

    def save(self, document: Document, output_path: Path) -> None:
        """Persist the translated DXF to *output_path*.

        Opens the original DXF, applies all entity translations, and
        saves. CadTokenProtector placeholders are decoded back to
        MTEXT format codes before writing to the DXF.
        """
        source_path = Path(
            document.metadata.get("source_dxf_path", "")
            or document.metadata.get("file_path", "")
        )
        if not source_path.exists():
            raise FileNotFoundError(
                f"Original DXF not found: {source_path}"
            )

        dxf_doc = self._backend.open(source_path)
        protector = CadTokenProtector()

        # Build a lookup: handle → translated_text (decoded)
        handle_map: dict[str, str] = {}
        for entity in document.entities:
            if entity.translated_text:
                decoded = protector.decode(
                    entity.translated_text, entity.protected_tokens
                )
                for h in entity.handles:
                    handle_map[h] = decoded

        # Walk the DXF and apply translations by handle
        applied = 0
        for raw_entity, _ in self._backend.iter_entities(dxf_doc):
            handle = self._backend.get_handle(raw_entity)
            if handle in handle_map:
                self._backend.set_text(raw_entity, handle_map[handle])
                applied += 1

        self._backend.save(dxf_doc, output_path)
        self._backend.close(dxf_doc)

        logger.info(
            "Saved translated DXF: %s (%d entities updated)",
            output_path.name,
            applied,
        )
