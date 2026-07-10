"""BatchBuilder — groups text units into translation batches.

Strategies:
- **FLAT**: simple sequential split (default).
- **BY_LAYER**: group by CAD layer, keeps related text together.
- **BY_COORDINATES**: group by spatial proximity (CAD-specific).
"""

from __future__ import annotations

from typing import Any

from file_translator.infrastructure.services.text_extractor import TextUnit


class BatchBuilder:
    """Groups TextUnits into batches for the LLM."""

    def __init__(self, batch_size: int = 50, strategy: str = "FLAT") -> None:
        self._batch_size = batch_size
        self._strategy = strategy.upper()

    def build(self, units: list[TextUnit]) -> list[list[TextUnit]]:
        """Split *units* into batches according to the active strategy."""
        if self._strategy == "BY_LAYER":
            return self._build_by_layer(units)
        return self._build_flat(units)

    def _build_flat(self, units: list[TextUnit]) -> list[list[TextUnit]]:
        batches: list[list[TextUnit]] = []
        for i in range(0, len(units), self._batch_size):
            batches.append(units[i:i + self._batch_size])
        return batches

    def _build_by_layer(self, units: list[TextUnit]) -> list[list[TextUnit]]:
        layers: dict[str, list[TextUnit]] = {}
        for u in units:
            layer = u.metadata.get("layer", "0")
            layers.setdefault(layer, []).append(u)

        batches: list[list[TextUnit]] = []
        for layer_units in layers.values():
            for i in range(0, len(layer_units), self._batch_size):
                batches.append(layer_units[i:i + self._batch_size])
        return batches
