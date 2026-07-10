"""CAD TokenProtector — replaces MTEXT formatting codes with placeholders.

Protected patterns (DXF/DWG MTEXT codes):
  - ``{\\f...}``  font/formatting block
  - ``\\P``       newline
  - ``\\S...^...`` stacked text (fractions)
  - ``\\A``       alignment
  - ``\\H``       text height
  - ``\\Q``       oblique angle
  - ``\\W``       width factor
  - ``\\T``       tracking
  - ``\\_``       underline toggle
  - ``\\O``       overline toggle
  - ``\\L``       strikethrough toggle
  - ``\\~``       non-breaking space
"""

from __future__ import annotations

import re
from typing import Any

_FMT_PATTERN = re.compile(
    r"\\[PASHLQWT_OL~]|"
    r"\\S[^;]*;|"
    r"\\[HWCQF](\d+(\.\d+)?)?;|"
    r"\{[^}]*\}"
)

_COUNTER_ATTR = "_token_counter"


class CadTokenProtector:
    """Protect MTEXT format codes by replacing them with placeholders.

    Usage::

        protector = CadTokenProtector()
        encoded, tokens = protector.encode("{\\fArial|b0|i0;Valve\\PSize}")
        # encoded == "[[FMT_0]]Valve[[NL_0]]Size[[FMT_0_end]]"
        # tokens == [{"placeholder": "[[FMT_0]]", "original": "{\\fArial|b0|i0;"}, ...]
        restored = protector.decode(encoded, tokens)
        # restored == "{\\fArial|b0|i0;Valve\\PSize}"
    """

    def encode(self, text: str, entity_id: str = "") -> tuple[str, list[dict[str, str]]]:
        """Replace format codes in *text* with placeholders.

        Returns ``(encoded_text, token_list)`` where each token has:
        - ``placeholder``: the replacement string
        - ``original``: the original format code
        """
        tokens: list[dict[str, str]] = []
        counter = 0

        def _replace(match: re.Match) -> str:
            nonlocal counter
            original = match.group(0)
            placeholder = f"[[FMT_{entity_id}_{counter}]]"
            tokens.append({
                "placeholder": placeholder,
                "original": original,
            })
            counter += 1
            return placeholder

        encoded = _FMT_PATTERN.sub(_replace, text)
        return encoded, tokens

    def decode(self, text: str, tokens: list[dict[str, str]]) -> str:
        """Restore format codes from placeholders."""
        result = text
        for token in tokens:
            result = result.replace(token["placeholder"], token["original"])
        return result
