"""TranslationMemory — normalized-text cache to avoid redundant LLM calls.

Only caches when an entity's text has been **translated by the LLM**.
Does NOT cache pre-filled or glossary substitutions — those are handled
by their respective components.

Entities with identical text that also share handles (grouped by the
parser) share a single cache entry, because ``DxfParser`` already
deduplicates them.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TranslationMemory:
    """In-memory cache: normalized_text → translated_text.

    Normalization: strip, lowercase, collapse whitespace.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())

    def lookup(self, text: str) -> str | None:
        """Return cached translation, or ``None``."""
        key = self._normalize(text)
        result = self._cache.get(key)
        if result is not None:
            self._hits += 1
        else:
            self._misses += 1
        return result

    def store(self, original_text: str, translated_text: str) -> None:
        """Cache a translation."""
        key = self._normalize(original_text)
        self._cache[key] = translated_text

    def bulk_store(self, translations: dict[str, str]) -> None:
        """Cache many translations at once: ``{original: translated}``."""
        for original, translated in translations.items():
            self.store(original, translated)

    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    def clear(self) -> None:
        self._cache.clear()
        self._hits = 0
        self._misses = 0
