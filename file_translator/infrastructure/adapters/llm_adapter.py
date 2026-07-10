"""LLMAdapter — abstraction over LLM translation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMAdapter(ABC):
    """Abstract interface for batch translation via LLM."""

    @abstractmethod
    async def translate_batch(
        self, batch: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Translate a batch of text units.

        Args:
            batch: Dict with keys:
                - "texts": list of strings to translate
                - "source_language": source language code
                - "target_language": target language code
                - "style": translation style hint
                - "glossary_hints": optional list of glossary entries

        Returns:
            List of ``{"id": str, "text": str}`` dicts.
        """
        ...


class OpenaiLLMAdapter(LLMAdapter):
    """Adapter wrapping the existing ``OpenAITranslationProvider``."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def translate_batch(
        self, batch: dict[str, Any]
    ) -> list[dict[str, str]]:
        texts = batch.get("texts", [])
        if not texts:
            return []

        batch_data = {
            "batch": batch.get("_batch_obj"),
            "source_language": batch.get("source_language"),
            "target_language": batch.get("target_language"),
            "translation_style": batch.get("style"),
            "translation_mode": batch.get("translation_mode", "full"),
            "use_glossary": bool(batch.get("glossary_hints")),
            "glossary_entries": batch.get("glossary_hints", []),
        }

        return await self._provider.translate_batch(batch_data)
