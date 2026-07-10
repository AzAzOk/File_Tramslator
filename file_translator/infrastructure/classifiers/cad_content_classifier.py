"""CAD ContentClassifier — maps EntityType to translation categories.

Classification rules (from the CAD architecture):

| EntityType          | Category        |
|---------------------|-----------------|
| TEXT                | TRANSLATE       |
| MTEXT               | TRANSLATE       |
| ATTRIB (value)      | TRANSLATE       |
| ATTRIB (tag)        | METADATA (skip) |
| ATTDEF (default)    | TRANSLATE       |
| ATTDEF (tag)        | METADATA (skip) |
| DIMENSION           | TRANSLATE       |
| TABLE_TEXT          | TRANSLATE       |
| TABLE_MTEXT         | TRANSLATE       |
| TABLE_CELL          | TRANSLATE       |
| INSERT_ATTRIBUTE    | TRANSLATE       |
| EMBEDDED_TEXT       | TRANSLATE       |
"""

from __future__ import annotations

from enum import Enum

from file_translator.domain.document_model import EntityType, TranslatableEntity


class Category(Enum):
    TRANSLATE = "TRANSLATE"
    METADATA = "METADATA"
    MEASUREMENT = "MEASUREMENT"
    FORMULA = "FORMULA"
    SKIP = "SKIP"


_CAD_CATEGORY_MAP: dict[str, Category] = {
    "TEXT": Category.TRANSLATE,
    "MTEXT": Category.TRANSLATE,
    "ATTRIB": Category.TRANSLATE,
    "ATTDEF": Category.TRANSLATE,
    "DIMENSION": Category.TRANSLATE,
    "TABLE_TEXT": Category.TRANSLATE,
    "TABLE_MTEXT": Category.TRANSLATE,
    "TABLE_CELL": Category.TRANSLATE,
    "INSERT_ATTRIBUTE": Category.TRANSLATE,
    "EMBEDDED_TEXT": Category.TRANSLATE,
    "PARAGRAPH": Category.TRANSLATE,
    "CELL": Category.TRANSLATE,
    "FORMULA": Category.FORMULA,
}


class CadContentClassifier:
    """Classifies CAD translatable entities into categories."""

    def classify(self, entity: TranslatableEntity) -> Category:
        """Return the translation category for *entity*.

        Handles ATTRIB/ATTDEF tag vs value distinction via the
        ``is_tag`` metadata flag.
        """
        base = _CAD_CATEGORY_MAP.get(entity.type.value, Category.TRANSLATE)

        # ATTRIB/ATTDEF tags are metadata, not translatable
        if entity.type in (EntityType.ATTRIB, EntityType.ATTDEF):
            if entity.metadata.get("is_tag", False):
                return Category.METADATA

        # DIMENSION without override text is measurement
        if entity.type == EntityType.DIMENSION:
            if not entity.text.strip():
                return Category.MEASUREMENT

        return base

    def classify_many(
        self, entities: list[TranslatableEntity]
    ) -> dict[str, Category]:
        """Classify a list of entities and return ``{entity_id: Category}``."""
        return {e.id: self.classify(e) for e in entities}
