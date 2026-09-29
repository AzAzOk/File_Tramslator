"""Numeric/normative-reference fidelity helpers.

The translation pipeline must never change numbers, dates, or normative
designations (№732, ГОСТ 21.1101-2013, «Таблица 3.1», …). This module provides
pure helpers used by:

- the LLM provider (``openai_provider``): protect numeric tokens with opaque
  placeholders before the model call, restore them after parsing, and verify +
  repair drift when a model drops placeholders;
- the post-merge integrity gate (``integrity.py``): detect numeric drift
  between an original and a translated document.

Pattern kinds:

- ``ref`` — designation numbers: ``№732``, ``No. 732``, ``№1077`` (digit-only
  identity; survives the ``№``/``No.`` rewording the model may produce).
- ``sub`` — dotted designators: ``3.1``, ``СН РК 1.02-03-2011`` (table/section
  numbers, standard designations). Compared as exact dot-joined strings.
- ``num`` — bare digit runs (``120``, ``732``). Compared as a multiset of digit
  strings; a mismatch here cannot be safely auto-repaired (a bare number may be
  an address/phone) and is reported as unresolved.

Dates are intentionally NOT verified by number comparison: a source date
``24.10.2014`` is legitimately rendered as ``October 24, 2014`` in English,
which contains no comparable digit string. A reference like ``№732`` inside the
same phrase is still verified by the ``ref`` kind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Chosen to be very unlikely in natural text; the model is told to keep
# unknown tokens verbatim and restores are unambiguous.
_PH = "⟦NUM{}⟧"

# Designation reference: №732 / No. 732 / № 1077 (digits identity only).
_REF_RE = re.compile(r"(?:№|No\.?|No)\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)
# Dotted designators (tables/sections/standards): 3.1, 1.02-03-2011, 21.1101-2013
_SUB_RE = re.compile(r"\b(\d+(?:\.\d+)+(?:-\d+(?:\.\d+)*)*)\b")
# Bare digit runs (>=2 digits to reduce decimal noise).
_NUM_RE = re.compile(r"(?<!\d)\d{2,}(?!\d)")

_PH_RE = re.compile(r"⟦NUM(\d+)⟧")


@dataclass
class NumericToken:
    """One protected numeric token and its original spelling."""

    kind: str                  # "ref" | "sub" | "num"
    raw: str                   # original substring as it appeared in source
    normalized: str            # canonical comparison value
    index: int = 0             # placeholder index


@dataclass
class NumericMismatch:
    """A verified numeric drift: source value vs what the target contained."""

    kind: str
    source: str
    target: str
    count: int = 1


def _normalize_ref(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def _normalize_sub(raw: str) -> str:
    return raw.strip()


def _normalize_num(raw: str) -> str:
    return raw


_DATE_SHAPE_RE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")
# Known measurement-unit suffixes that make a dotted number a VALUE (1.4 kPa)
# rather than a section/table designator.
_UNIT_SUFFIX_RE = re.compile(
    r"^\s*(?:кПа|МПа|Па|MPa|kPa|кВт|kW|Вт|В|А|мм рт\.? ст\.?|\d+|"
    r"%|°[CС]?|[мсдк]м|см|мм|км|м/с|т|кг|г|мг|м\^?3|м\^?2|л|мл|с|ч|мин)\b",
    re.IGNORECASE,
)


def _spans(text: str) -> list[tuple[int, int, str, str]]:
    """Return (start, end, kind, raw) for every numeric token in text order."""
    spans: list[tuple[int, int, str, str]] = []
    for m in _REF_RE.finditer(text):
        spans.append((m.start(), m.end(), "ref", m.group(0).strip()))
    for m in _SUB_RE.finditer(text):
        # Skip sub-matches fully inside a ref span.
        inside = any(
            m.start() >= s and m.end() <= e for (s, e, k, r) in spans if k == "ref"
        )
        if inside:
            continue
        raw = m.group(0)
        # Date-shaped dotted values (24.10.2014) are protected/restored but are
        # NOT part of numeric verification: English renders them without digits
        # ("October 24, 2014"), so strict comparison would false-positive.
        if _DATE_SHAPE_RE.match(raw):
            kind = "date"
        elif _UNIT_SUFFIX_RE.match(text[m.end():]):
            # A value with a unit suffix (1.4 kPa) is data, not a designator.
            kind = "num"
        else:
            kind = "sub"
        spans.append((m.start(), m.end(), kind, raw))
    for m in _NUM_RE.finditer(text):
        inside = any(
            m.start() >= s and m.end() <= e for (s, e, k, r) in spans
        )
        if inside:
            continue
        spans.append((m.start(), m.end(), "num", m.group(0)))
    spans.sort(key=lambda x: (x[0], x[1]))
    return spans


def _normalize_value(kind: str, raw: str) -> str:
    if kind == "ref":
        return _normalize_ref(raw)
    if kind == "sub":
        return _normalize_sub(raw)
    if kind == "date":
        # Canonical (day, month, year) triple for bookkeeping (not verified).
        parts = re.split(r"[./-]", raw)
        return "-".join(p.zfill(2) if len(p) == 1 else p for p in parts)
    return _normalize_num(raw)


def protect_numerics(text: str) -> tuple[str, list[NumericToken]]:
    """Replace numeric tokens with placeholders.

    Returns ``(protected_text, tokens)`` where ``tokens`` is the registry in
    placeholder order (``tokens[i].index == i``).
    """
    spans = _spans(text)
    tokens: list[NumericToken] = []
    if not spans:
        return text, tokens

    parts: list[str] = []
    cursor = 0
    for idx, (start, end, kind, raw) in enumerate(spans):
        parts.append(text[cursor:start])
        parts.append(_PH.format(idx))
        tokens.append(NumericToken(kind, raw, _normalize_value(kind, raw), idx))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), tokens


def restore_numerics(text: str, tokens: list[NumericToken]) -> str:
    """Replace ``⟦NUM<n>⟧`` placeholders in ``text`` with the original tokens."""
    if not tokens:
        return text

    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(tokens):
            return tokens[idx].raw
        return m.group(0)

    return _PH_RE.sub(_sub, text)


def verify_numeric(source: str, target: str) -> list[NumericMismatch]:
    """Return numeric drift between ``source`` and ``target``.

    ``ref``/``sub`` kinds are compared as multisets of normalized values;
    ``num`` is compared as a multiset of digit strings. For each source value
    missing (undercount) in the target, a mismatch is emitted whose ``source``
    is the original value and whose ``target`` is the paired *extra* value
    present in the target (the model's replacement, when one exists) so that
    :func:`repair_numeric` can locate and substitute it.
    """
    from collections import Counter

    src = _features(source)
    tgt = _features(target)
    mismatches: list[NumericMismatch] = []
    for kind in ("ref", "sub", "num"):
        s_c = Counter(src.get(kind, []))
        t_c = Counter(tgt.get(kind, []))
        if s_c == t_c:
            continue
        lost = list((s_c - t_c).elements())
        extra = list((t_c - s_c).elements())
        for i, v in enumerate(lost):
            tgt_v = extra[i] if i < len(extra) else ""
            mismatches.append(NumericMismatch(kind, v, tgt_v, count=1))
    return mismatches


def _features(text: str) -> dict[str, list[str]]:
    feats: dict[str, list[str]] = {"ref": [], "sub": [], "num": [], "date": []}
    for start, end, kind, raw in _spans(text):
        feats[kind].append(_normalize_value(kind, raw))
    return feats


def repair_numeric(
    target: str,
    mismatches: list[NumericMismatch],
    tokens: list[NumericToken],
) -> tuple[str, list[NumericMismatch]]:
    """Repair repairable numeric drift in ``target``.

    ``ref`` and ``sub`` mismatches are repaired by locating in ``target`` a
    token whose normalized value equals the drifted target value and replacing
    it with the source raw spelling. ``num`` mismatches and anything not
    findable are returned in ``unresolved``.
    """
    repaired_text = target
    unresolved: list[NumericMismatch] = []
    # Source spelling lookup by normalized value.
    raw_by_value: dict[tuple[str, str], str] = {}
    for t in tokens:
        raw_by_value.setdefault((t.kind, t.normalized), t.raw)

    for mm in mismatches:
        if mm.kind == "num":
            unresolved.append(mm)
            continue
        # Only repair when the same count of the drifted value was present in
        # target — otherwise the model reworded too much; retry instead.
        target_hits = [(s, e, k, r) for (s, e, k, r) in _spans(repaired_text)
                       if k == mm.kind and _normalize_value(k, r) == mm.target]
        if not target_hits:
            unresolved.append(mm)
            continue
        # Replace the FIRST occurrence of the drifted value with source raw.
        s, e, k, r = target_hits[0]
        src_raw = raw_by_value.get((k, mm.source))
        if src_raw is None:
            unresolved.append(mm)
            continue
        # Preserve a leading '№'/'No.' prefix if the source raw lacks it.
        prefix = ""
        mm_pref = re.match(r"^(?:№|No\.?)\s*", r)
        if mm_pref and not re.match(r"^(?:№|No\.?)\s*", src_raw):
            prefix = mm_pref.group(0)
        repaired_text = repaired_text[:s] + prefix + src_raw + repaired_text[e:]

    return repaired_text, unresolved