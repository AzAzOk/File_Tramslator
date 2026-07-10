"""GlossaryEngine — forced terminology substitutions.

Integrates with the existing ``GlossaryService`` to load glossary
entries and apply word-boundary replacements **before** the text
reaches the LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class GlossaryEngine:
    """Applies glossary substitutions on a per-entity basis.

    Usage::

        engine = GlossaryEngine()
        engine.load_entries([{"source": "Valve", "target": "Клапан"}, ...])
        text = engine.apply("Valve is open")  # "Клапан is open"
    """

    def __init__(self) -> None:
        self._replacements: list[tuple[re.Pattern, str]] = []

    def load_entries(self, entries: list[dict[str, str]]) -> None:
        """Load glossary entries.

        Each entry should have ``"source"`` (in source language) and
        ``"target"`` (in target language) keys.
        """
        self._replacements = []
        for entry in entries:
            source = entry.get("source", entry.get("ru_word", entry.get("en_word", "")))
            target = entry.get("target", entry.get("en_word", entry.get("ru_word", "")))
            if not source or not target:
                continue
            # Word-boundary replacement to avoid partial matches
            pattern = re.compile(
                rf"\b{re.escape(source)}\b", re.IGNORECASE
            )
            self._replacements.append((pattern, target))

    def apply(self, text: str) -> str:
        """Run all glossary replacements on *text*."""
        result = text
        for pattern, replacement in self._replacements:
            result = pattern.sub(replacement, result)
        return result

    @property
    def entry_count(self) -> int:
        return len(self._replacements)
