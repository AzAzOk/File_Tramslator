"""CAD TokenProtector — replaces MTEXT formatting codes with placeholders.

Protected patterns (DXF/DWG MTEXT codes):
  - ``{\\f...}``       font/formatting block
  - ``\\P``            newline (paragraph end)
  - ``\\S...^...``     stacked text (fractions)
  - ``\\A``            alignment
  - ``\\H``            text height
  - ``\\Q``            oblique angle
  - ``\\W``            width factor
  - ``\\T``            tracking
  - ``\\_``            underline toggle
  - ``\\O``            overline toggle
  - ``\\L``            strikethrough toggle
  - ``\\~``            non-breaking space
  - ``\\p...;``        paragraph format (e.g. \\pxl1;, \\pxi4.2923;)
  - ``\\C``            color
  - ``\\F``            font
  - ``\\S``            stacked text
  - ``\\c``            lowercase color
  - ``\\s``            lowercase stacked
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Match any \letter...; format code, and standalone \letter codes.
# Braces {...} are NOT matched — they are group markers that may surround
# translatable text (e.g. {\C7;Щит питания...}). Only the format codes
# inside braces are encoded individually by the patterns above.
_FMT_PATTERN = re.compile(
    r"\\[A-Za-z][^;]*;|"      # \code...;  (any format code with params terminated by ;)
    r"\\[A-Za-z~_]"           # \code       (standalone codes like \P, \L, \O, \_, \~)
)

_COUNTER_ATTR = "_token_counter"


_PLACEHOLDER_RE = re.compile(r"\[\[F\d+\]\]")


class CadTokenProtector:
    """Protect MTEXT format codes by replacing them with placeholders.

    Usage::

        protector = CadTokenProtector()
        encoded, tokens = protector.encode("{\\fArial|b0|i0;Valve\\PSize}")
        # encoded == "{[[F0]]Valve[[F1]]Size}"
        # tokens == [{"placeholder": "[[F0]]", "original": "\\fArial|b0|i0;"}, ...]
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
            placeholder = f"[[F{counter}]]"
            tokens.append({
                "placeholder": placeholder,
                "original": original,
            })
            counter += 1
            return placeholder

        encoded = _FMT_PATTERN.sub(_replace, text)

        if entity_id:
            preview_before = text[:80].replace("\n", "\\n")
            preview_after = encoded[:80].replace("\n", "\\n")
            if len(text) > 80:
                preview_before += "..."
            if len(encoded) > 80:
                preview_after += "..."
            logger.info(
                "encode %s: found %d tokens: before=%r after=%r",
                entity_id, counter, preview_before, preview_after,
            )

        return encoded, tokens

    def decode(self, text: str, tokens: list[dict[str, str]]) -> str:
        """Restore format codes from placeholders."""
        result = text
        for token in tokens:
            result = result.replace(token["placeholder"], token["original"])
        return result

    def has_translatable_content(self, encoded_text: str) -> bool:
        """True, если после удаления плейсхолдеров в строке остался хоть какой-то текст."""
        return bool(_PLACEHOLDER_RE.sub("", encoded_text).strip())
