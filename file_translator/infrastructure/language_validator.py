"""Language validation for glossary columns using lingua-language-detector.

Falls back to character-set heuristics when the library is not installed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    from lingua import Language, LanguageDetectorBuilder

    LINGUA_AVAILABLE = True
except ImportError:
    LINGUA_AVAILABLE = False
    logger.info("lingua-language-detector not installed; using character-set fallback")


# ── Column → ISO 639-1 mapping ──

_COLUMN_LANG: dict[str, str] = {
    "ru_word": "ru",
    "en_word": "en",
    "sb_word": "sr",
    "ch_word": "zh",
}


# ── Character-set heuristic fallback ──

# Regex patterns for quick language detection
_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
_LATIN = re.compile(r"[a-zA-Z]")
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _heuristic_detect(text: str) -> str | None:
    """Detect language using character-set analysis.

    Returns an ISO 639-1 code or *None* if undetermined.
    """
    stripped = text.strip()
    if not stripped:
        return None

    cjk_count = len(_CJK.findall(stripped))
    cyr_count = len(_CYRILLIC.findall(stripped))
    lat_count = len(_LATIN.findall(stripped))

    # For very short texts (< 5 chars), require overwhelming majority
    if len(stripped) < 5:
        total = cjk_count + cyr_count + lat_count
        if total == 0:
            return None
        # Only return a result if all characters belong to one script
        if cjk_count == total:
            return "zh"
        if cyr_count == total:
            return "ru"
        return None

    # Chinese — dominated by CJK ideographs
    if cjk_count > 0 and cjk_count >= len(stripped) * 0.3:
        return "zh"
    # Russian / Serbian Cyrillic
    if cyr_count > 0 and cyr_count >= lat_count:
        return "ru"
    # English
    if lat_count > 0:
        return "en"
    return None


# ── lingua wrapper ──

_DETECTOR: Any = None

if LINGUA_AVAILABLE:

    def _get_detector():
        global _DETECTOR
        if _DETECTOR is None:
            _DETECTOR = LanguageDetectorBuilder.from_languages(
                Language.ENGLISH,
                Language.RUSSIAN,
                Language.SERBIAN,
                Language.CHINESE,
            ).build()
        return _DETECTOR

    _LINGUA_REV: dict[Language, str] = {
        Language.RUSSIAN: "ru",
        Language.ENGLISH: "en",
        Language.SERBIAN: "sr",
        Language.CHINESE: "zh",
    }

else:

    def _get_detector():  # type: ignore[misc]
        return None


# ── Public API ──


def detect_language(text: str) -> str | None:
    """Detect the language of *text*.

    Returns an ISO 639‑1 code (``ru``, ``en``, ``sr``, ``zh``) or
    *None* when detection is not possible.
    """
    stripped = text.strip()
    if not stripped:
        return None

    detector = _get_detector()
    if detector is not None:
        detected = detector.detect_language_of(stripped)
        if detected is not None:
            return _LINGUA_REV.get(detected)

    return _heuristic_detect(stripped)


def expected_language(column: str) -> str | None:
    """Return the ISO 639‑1 code expected for a glossary *column*."""
    return _COLUMN_LANG.get(column)


def validate_glossary_value(column: str, text: str) -> str | None:
    """Check that *text* matches the language expected for *column*.

    Returns ``None`` on success, or an error message string on mismatch.
    """
    expected = expected_language(column)
    if expected is None:
        return None

    detected = detect_language(text)
    if detected is None:
        return None  # can't determine — let it pass

    # Serbian can be written in Cyrillic or Latin; accept both
    if expected == "sr" and detected in ("sr", "ru", "en"):
        return None

    # Russian → accept Cyrillic match
    if expected == "ru" and detected == "ru":
        return None

    # Chinese → accept CJK match
    if expected == "zh" and detected == "zh":
        return None

    # English → accept Latin match
    if expected == "en" and detected == "en":
        return None

    col_labels = {
        "ru_word": "Русское слово",
        "en_word": "Английское слово",
        "sb_word": "Сербское слово",
        "ch_word": "Китайское слово",
    }
    label = col_labels.get(column, column)
    return (
        f"{label} '{text}' не соответствует ожидаемому языку "
        f"(определён как '{detected}', ожидался '{expected}')"
    )
