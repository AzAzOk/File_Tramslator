"""TextExtractor — collects translatable text units from a Document."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from file_translator.domain.document_model import (
    Document,
    TranslatableEntity,
    TranslationStatus,
)
from file_translator.infrastructure.classifiers.cad_content_classifier import (
    CadContentClassifier,
    Category,
)
from file_translator.infrastructure.engines.cad_rule_engine import (
    RuleAction,
    RuleEngine,
)


@dataclass
class TextUnit:
    """A single unit of text ready for LLM translation."""
    entity_id: str
    text: str
    context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TextExtractor:
    """Filters a Document for entities that should be translated.

    Applies ContentClassifier → RuleEngine → produces TextUnit list.
    """

    def __init__(
        self,
        classifier: CadContentClassifier | None = None,
        rule_engine: RuleEngine | None = None,
    ) -> None:
        self._classifier = classifier or CadContentClassifier()
        self._rule_engine = rule_engine or RuleEngine()

    def extract(self, document: Document) -> list[TextUnit]:
        """Return all translatable text units from *document*."""
        units: list[TextUnit] = []

        for entity in document.entities:
            # Step 1: classify
            category = self._classifier.classify(entity)
            if category != Category.TRANSLATE:
                continue

            # Step 2: rule engine
            action = self._rule_engine.evaluate(entity)
            if action == RuleAction.SKIP:
                entity.translation_status = TranslationStatus.SKIPPED
                continue

            # Step 3: build TextUnit
            context = entity.metadata.get("layer", "")
            unit = TextUnit(
                entity_id=entity.id,
                text=entity.text,
                context=context,
                metadata=dict(entity.metadata),
            )
            units.append(unit)

        return units
