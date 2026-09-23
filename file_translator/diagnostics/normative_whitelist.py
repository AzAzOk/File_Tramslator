"""Normative-reference whitelist for untranslated-content detection.

Names and designations of normative documents (СН РК, СП РК, ГОСТ, ГКИНП,
СНиП, ВСН, ЕНиР, …) are intentionally preserved in the source language when a
document is translated. The leak scanner must NOT flag such fragments, but
MUST flag surrounding headings and descriptions (e.g. «НПА и технические
условия:»).

This module holds the data (designation prefixes) as a machine-checkable
contract and exposes matchers that the future LLM-prompt rule can reuse.
"""

from __future__ import annotations

import re
from typing import Iterable

# Designation prefixes used in Russian/CIS normative references. A fragment is
# considered a normative reference when it starts (after optional whitespace)
# with one of these prefixes and is followed by an identifier — typically a
# number (e.g. ГОСТ Р 21.1101-2013) or a short code (ГКИНП-02-033).
DEFAULT_DESIGNATION_PREFIXES: tuple[str, ...] = (
    "ГОСТ Р",
    "ГОСТ",
    "СН РК",
    "СП РК",
    "СНиП РК",
    "СНиП",
    "ГКИНП",
    "ВСН",
    "ЕНиР",
    "СанПиН",
    "ТР ТС",
    "ТКП",
    "СТ РК",
    "РДС",
    "МДС",
    "ПУЭ",
    "ПБ",
    "НПБ",
)

# Prefixes considered "list headings / descriptions" — these are surrounding
# text that MUST be translated (the user's normative-references decision:
# document names stay, headings/descriptions translate).
HEADING_PATTERNS: tuple[str, ...] = (
    "НПА и технические условия",
    "Нормативные документы",
    "Нормативные ссылки",
    "Перечень НПА",
    "Список используемой литературы",
)

_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff"
    r"\uac00-\ud7af\uf900-\ufaff\uf900-\ufa2d]+"
)
_CYRILLIC_PATTERN = re.compile(r"[\u0400-\u04ff]+")


def _compile_prefix_regex(prefixes: Iterable[str]) -> re.Pattern:
    """Compile a regex that matches any of the given designation prefixes at
    the start of a fragment.

    A bare prefix with no identifier (e.g. just "СН РК" or "ГОСТ") is NOT a
    normative reference — the prefix must be followed by a separator and an
    identifier (typically digits, e.g. "ГОСТ Р 21.1101-2013"), or by a dash
    (e.g. "ГКИНП-02-033").
    """
    escaped = [re.escape(p) for p in prefixes]
    # Sort by length descending so ГОСТ Р matches before ГОСТ.
    escaped.sort(key=len, reverse=True)
    # Allow an optional leading list bullet ("- ", "— ", "• ") before the
    # prefix: normative lists commonly render entries as "- СН РК 1.02-04-2013".
    # After the prefix: either whitespace + an identifier token containing a
    # digit, or an immediate dash (hyphen/en/em). This avoids matching a bare
    # heading-like "СН РК" or "ГОСТ" at the end of a fragment.
    return re.compile(
        r"^\s*(?:[-–—•]\s*)?(?:" + "|".join(escaped) + r")(?=\s+\S*\d|[-—–])",
        re.IGNORECASE,
    )


DEFAULT_PREFIX_RE = _compile_prefix_regex(DEFAULT_DESIGNATION_PREFIXES)


def matches_normative_designation(text: str,
                                  prefixes: Iterable[str] | None = None) -> bool:
    """Return True if ``text`` starts with a normative designation.

    Examples: "СН РК 1.02-04-2013 «Инженерно-геодезические изыскания...»",
    "ГОСТ 21.1101-2013".
    """
    if not text or not text.strip():
        return False
    prefix_re = _compile_prefix_regex(
        prefixes or DEFAULT_DESIGNATION_PREFIXES
    )
    return bool(prefix_re.search(text))


def is_list_heading(text: str) -> bool:
    """Return True if ``text`` is a normative-reference list heading that
    must be translated (e.g. «НПА и технические условия:»)."""
    if not text or not text.strip():
        return False
    lower = text.lower()
    return any(h.lower() in lower for h in HEADING_PATTERNS)


def is_normative_reference(fragment: str,
                           prefixes: Iterable[str] | None = None) -> bool:
    """Classify a fragment for the leak scanner.

    Returns True when the fragment is a normative document reference that
    should be whitelisted (designation prefix + following content, not a list
    heading).
    """
    if is_list_heading(fragment):
        return False
    return matches_normative_designation(fragment, prefixes)


def strip_cjk(text: str) -> str:
    """Remove CJK characters (used to detect Chinese leftover text)."""
    return _CJK_PATTERN.sub("", text)


def strip_cyrillic(text: str) -> str:
    """Remove Cyrillic characters (used to detect Russian leftover text)."""
    return _CYRILLIC_PATTERN.sub("", text)