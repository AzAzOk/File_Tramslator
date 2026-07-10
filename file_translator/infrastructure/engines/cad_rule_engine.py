"""CAD RuleEngine — skip/prefill rules for non-translatable patterns.

Evaluates each entity's text against a configurable set of rules.
Rules can:
1. **SKIP** — the entity is never sent to the LLM (codes, standards)
2. **PREFILL** — the entity keeps its original text (numbers, dimensions)
3. **PASS** — the entity goes through normal translation

Built-in default rules (JSON-configurable):
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from file_translator.domain.document_model import TranslatableEntity


class RuleAction(Enum):
    SKIP = "SKIP"
    PREFILL = "PREFILL"
    PASS = "PASS"


class RuleEngine:
    """Evaluates text against rules and returns actions per entity.

    Usage::

        engine = RuleEngine()
        action = engine.evaluate(entity)  # RuleAction.SKIP, .PREFILL, .PASS
    """

    DEFAULT_RULES: list[dict[str, Any]] = [
        # Nominal diameters
        {"pattern": r"^DN\d+", "action": "SKIP", "description": "DN diameters"},
        # Dimensions: 100x200, 100x200x300
        {"pattern": r"\d+[×xх]\d+", "action": "SKIP", "description": "dimensions"},
        # Diameter symbol + number
        {"pattern": r"Ø\d+", "action": "SKIP", "description": "diameter codes"},
        # Standards
        {"pattern": r"ISO\d+", "action": "SKIP", "description": "ISO standards"},
        {"pattern": r"GOST\s*\d+", "action": "SKIP", "description": "GOST standards"},
        {"pattern": r"ГОСТ\s*\d+", "action": "SKIP", "description": "ГОСТ standards"},
        # Pure numbers
        {"pattern": r"^\d+$", "action": "SKIP", "description": "pure numbers"},
        # Alphanumeric codes: VALVE001, PIPE-12A
        {"pattern": r"^[A-Z]{2,}\d*$", "action": "SKIP", "description": "product codes"},
        # Material grades
        {"pattern": r"^[A-Za-z]+\d+[A-Za-z]+$", "action": "SKIP", "description": "material grades"},
    ]

    def __init__(self, rules: list[dict[str, Any]] | None = None) -> None:
        self._rules: list[dict[str, Any]] = rules or list(self.DEFAULT_RULES)
        self._compiled: list[tuple[re.Pattern, RuleAction]] = [
            (re.compile(r["pattern"]), RuleAction(r["action"]))
            for r in self._rules
        ]

    def evaluate(self, entity: TranslatableEntity) -> RuleAction:
        """Run text through all rules, return the first matching action."""
        text = entity.text.strip()
        if not text:
            return RuleAction.SKIP

        for pattern, action in self._compiled:
            if pattern.search(text):
                return action

        return RuleAction.PASS

    def evaluate_many(
        self, entities: list[TranslatableEntity]
    ) -> dict[str, RuleAction]:
        """Evaluate a list of entities, return ``{entity_id: RuleAction}``."""
        return {e.id: self.evaluate(e) for e in entities}
