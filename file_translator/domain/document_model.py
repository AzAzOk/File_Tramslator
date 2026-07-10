"""Universal Document Model for cross-format translation pipeline.

Document is the single contract that all parsers produce and all updaters
consume, regardless of the source format (DXF, DOCX, XLSX, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EntityType(Enum):
    """Cross-format entity type classification.

    These types span all supported document formats.  A CAD-specific
    DxfEntityType is mapped to EntityType by the ContentClassifier.
    """
    TEXT = "TEXT"
    MTEXT = "MTEXT"
    ATTRIB = "ATTRIB"
    ATTDEF = "ATTDEF"
    DIMENSION = "DIMENSION"
    TABLE_TEXT = "TABLE_TEXT"
    TABLE_MTEXT = "TABLE_MTEXT"
    TABLE_CELL = "TABLE_CELL"
    INSERT_ATTRIBUTE = "INSERT_ATTRIBUTE"
    EMBEDDED_TEXT = "EMBEDDED_TEXT"
    PARAGRAPH = "PARAGRAPH"
    CELL = "CELL"
    FORMULA = "FORMULA"


class TranslationStatus(Enum):
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"
    TRANSLATED = "TRANSLATED"
    FAILED = "FAILED"


@dataclass
class TranslatableEntity:
    """A single translatable unit within a Document.

    ``handles`` holds the native-format identifiers (DXF handles, Word
    bookmark ids, XLSX cell refs, …).  When identical text appears in
    multiple locations the parser groups them into *one* entity, keeping
    all handles — the updater later replicates the translation to every
    handle stored here.
    """
    id: str
    handles: list[str] = field(default_factory=list)
    type: EntityType = EntityType.TEXT
    text: str = ""
    translated_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    protected_tokens: list[dict[str, str]] = field(default_factory=list)
    translation_status: TranslationStatus = TranslationStatus.PENDING


@dataclass
class Document:
    """Universal document representation.

    Every ``IParser.parse()`` returns a ``Document``.  Every
    ``IUpdater.apply()`` / ``save()`` receives one.

    ``schema_version`` protects against evolutions of this model:
    - minor bump (1.0 → 1.1) — optional fields added
    - major bump (1.x → 2.0) — breaking; FormatRegistry may hold
      multiple major versions during migration.
    """
    schema_version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)
    entities: list[TranslatableEntity] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def entity_by_id(self, entity_id: str) -> TranslatableEntity | None:
        for e in self.entities:
            if e.id == entity_id:
                return e
        return None
