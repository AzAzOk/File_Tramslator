"""Word-boundary breakage scanner — detects glued words (missing inter-word
spaces) in a translated document.

The most common space-loss failure modes in the translation pipeline are:

- ``[a-z][A-Z]`` camel boundaries where a space was dropped before a word that
  starts with a capital letter (e.g. ``andSP``, ``theProject``) — but legitimate
  CamelCase names (``AutoCAD``, ``PetroKazakhstan``, ``GalaxyG6``) must NOT be
  flagged.
- Pure-lowercase concatenations of common words (``fortheproject`` =
  ``for`` + ``the`` + ``project``, ``createan`` = ``create`` + ``an``) produced
  by LLM output or XLIFF save water-fill dropping spaces inside a single text
  element.

The scanner reports the fragment (paragraph per-page text line), the paragraph
index, the rendered page (when page text is available), and the offending token.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Word data
#
# COMMON_WORDS is a curated set of very frequent English words plus the
# engineering/survey vocabulary that appears in this corpus. A pure-lowercase
# token is candidate glue only when it is NOT itself a dictionary word but can
# be fully decomposed into >=2 dictionary words (with at least one high-
# frequency function word participating) or is long enough that a full
# content-word split is suspicious.
# ---------------------------------------------------------------------------

_COMMON_WORDS_RAW = """
a about above after again against all also always an and any are as at
be because been before being below between both but by
can cannot could
did do does down during
each either else
few for from
get give go good
had has have having he her here hers herself him himself his how
i if in into is it its itself
just
like
many may me might more most much must my myself
neither no nor not now
of off on once one only or other our ours ourselves out over own
same say shall she should so some someplace something somewhere still such
than that the their theirs them themselves then there these they this those
through thus to too
under until up upon us
very
we well what when where which while who whom why will with within without
would
you your yours yourself yourselves

project projects engineering topographic topography survey surveys surveying
investigation investigations production capacity construction building
create created creating update updated up-to-date plan plans plant plants
design designing designed works work field land data soil area water ground
site report reports results conclusion conclusion introduction section
layers formation facilities object objects territory scale marks networks
purpose final initial stage stages works performed performed methods according
basis based including includes included provided requires required
development develop developed possible used use using conduct conducting
carried carry processing complex paraffin oils refinery owner customer
contractor documents documentation technical information conditions
compliance natural environmental protection safety industrial sanitary
requirements objects existing planned reconstructed roads highways railways
pipeline pipelines wells boreholes indicators determinations current state
strips route routes width longitudinal transverse profiles geodetic
horizontal cartographic materials topographic maps digital terrain model
models relief sections height reference network benchmarks points
photogrammetric aerial geodesic satellite global positioning systems
precision accuracy errors permissible coordinate coordinates elevations
surveying engineering-geological geological hydrogeological physical
mechanical properties engineering-geodynamic processes karst subsidence
"""

_FUNCTION_WORD_RAW = """
a about above after again against all also always an and any are as at
be because been before being below between both but by
can cannot could
did do does down during
each either else
few for from
get give go good
had has have having he her here hers herself him himself his how
i if in into is it its itself
just
like
many may me might more most much must my myself
neither no nor not now
of off on once one only or other our ours ourselves out over own
same say shall she should so some someplace something somewhere still such
than that the their theirs them themselves then there these they this those
through thus to too
under until up upon us
very
we well what when where which while who whom why will with within without
would
you your yours yourself yourselves
"""


def _build_wordset(raw: str) -> frozenset[str]:
    return frozenset(w for w in raw.split() if w)


COMMON_WORDS: frozenset[str] = _build_wordset(_COMMON_WORDS_RAW)
FUNCTION_WORDS: frozenset[str] = _build_wordset(_FUNCTION_WORD_RAW)

# Legitimate English words that a dictionary splitter would decompose; these
# must never be reported as glue.
NON_GLUE_COMPOUNDS: frozenset[str] = frozenset({
    "another", "anybody", "anyhow", "anymore", "anyone", "anything", "anyway",
    "anywhere", "because", "everybody", "everyone", "everything", "everywhere",
    "footnote", "footnotes", "groundwater", "henceforth", "herself", "himself",
    "however", "indoor", "indoors", "input", "into", "itself", "meanwhile",
    "moreover", "myself", "nobody", "nonetheless", "northeast", "northwest",
    "notwithstanding", "oneself", "online", "onto", "otherwise", "outdoor",
    "outdoors", "outside", "output", "southeast", "southwest", "sometime",
    "sometimes", "somewhat", "somewhere", "themselves", "thereafter",
    "thereby", "therefore", "therein", "thereof", "thereto", "underneath",
    "unless", "upstairs", "upstream", "whereas", "whereby", "wherein",
    "whatever", "whenever", "wherever", "workshop", "workbook", "workplace",
    "workpiece", "worldwide", "yourself", "yourselves",
    # Engineering/survey vocabulary that a splitter would decompose.
    "abovementioned", "aboveground", "groundwater", "underground",
    "together", "throughout",
})

# ---------------------------------------------------------------------------
# Tokenisation and splitter
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"[a-z]{2,}[A-Z]")
_LETTERS_RE = re.compile(r"[A-Za-z]+(?:[A-Za-z'-]+)?")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")

_MIN_GLUE_LEN = 8
_MAX_SPLIT_EXPLOSION = 8  # refuse pathological splits (word is mostly letters)


def _greedy_split(word: str, wordset: frozenset[str]) -> list[str] | None:
    """Split ``word`` into dictionary words by longest-match greedy descent.

    Returns the list of components if the WHOLE word decomposes into >=2 pieces
    each of length >= 2, else None. ``wordset`` must contain lowercase words.
    """
    if len(word) < _MIN_GLUE_LEN:
        return None
    components: list[str] = []
    pos = 0
    length = len(word)
    while pos < length:
        best: str | None = None
        # Greedy longest match against COMMON_WORDS (wordset is a superset —
        # we pass the full set, so the caller controls the vocabulary).
        for end in range(length, pos + 1, -1):
            candidate = word[pos:end]
            if candidate in wordset:
                best = candidate
                break
        if best is None or len(best) < 2:
            return None
        components.append(best)
        pos += len(best)
        if len(components) > _MAX_SPLIT_EXPLOSION:
            return None
    if len(components) < 2:
        return None
    return components


def _is_english_word(word: str) -> bool:
    return word in COMMON_WORDS


def _token_is_international(name: str) -> bool:
    """True when a token carries non-Latin characters (e.g. Cyrillic) — the
    glue scanner only reports English-like text."""
    return bool(_CYRILLIC_RE.search(name))


def _classify_token(token: str) -> str | None:
    """Classify one alphabetic token.

    Returns a reason string when the token is a suspicious glue, else None.
    """
    if not token or _token_is_international(token):
        return None
    if token in NON_GLUE_COMPOUNDS:
        return None
    # A token that is itself a dictionary word is never glue.
    if token.lower() in COMMON_WORDS:
        return None

    # 1) Camel boundary: lower-run (len>=2) followed directly by an uppercase
    #    letter. Report only when the lower run is itself a common English
    #    word, and the whole token is not a known dictionary word.
    if _CAMEL_RE.search(token):
        for m in _CAMEL_RE.finditer(token):
            lower_run = m.group(0)[:-1].lower()
            if _is_english_word(lower_run):
                return "camel_boundary"
        return None

    # 2) Pure-lowercase concatenation.
    if not token.islower():
        return None
    split = _greedy_split(token, COMMON_WORDS)
    if split is None:
        return None
    # Precision rule: at least one component must be a high-frequency function
    # word (the classic glue word), OR the token is long enough that a full
    # content-word split is unlikely to be an accidental single word.
    has_function = any(w.lower() in FUNCTION_WORDS for w in split)
    if not has_function and len(token) < 16:
        return None
    return "lowercase_concat"


def find_glued_words(text: str,
                     max_per_fragment: int = 5) -> list[dict[str, Any]]:
    """Scan ``text`` and return glued-word findings for this fragment.

    Each finding: ``{"token", "reason", "context"}`` where context is a short
    slice of the original text around the token.
    """
    findings: list[dict[str, Any]] = []
    for m in _LETTERS_RE.finditer(text):
        token = m.group(0)
        reason = _classify_token(token)
        if not reason:
            continue
        context = text[max(0, m.start() - 40):m.end() + 40]
        findings.append({
            "token": token,
            "reason": reason,
            "context": context,
        })
        if len(findings) >= max_per_fragment:
            break
    return findings


# ---------------------------------------------------------------------------
# DOCX paragraph scan
# ---------------------------------------------------------------------------

@dataclass
class GlueFinding:
    """One reported glued word."""

    page: int | None
    paragraph: int | None
    token: str
    reason: str
    fragment: str
    block: int | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "paragraph": self.paragraph,
            "block": self.block,
            "line": self.line,
            "token": self.token,
            "reason": self.reason,
            "fragment": self.fragment,
        }


def scan_docx_glued_words(docx_path: Path | str) -> list[dict[str, Any]]:
    """Scan a DOCX's ``word/document.xml`` paragraphs for glued words.

    Paragraph numbers are 0-based indices into document.xml body order; page is
    ``None`` (not known without rendering — use ``scan_page_text`` / the render
    loop for page-accurate results).
    """
    docx_path = Path(docx_path)
    findings: list[GlueFinding] = []
    with zipfile.ZipFile(str(docx_path)) as zf:
        try:
            with zf.open("word/document.xml") as fh:
                para_parts: list[str] = []
                in_paragraph = False
                para_index = -1
                for event, elem in ET.iterparse(fh, events=("start", "end")):
                    local = elem.tag.rsplit("}", 1)[-1]
                    if event == "start" and local == "p":
                        para_parts = []
                        in_paragraph = True
                        para_index += 1
                    elif event == "end" and local == "t":
                        if in_paragraph:
                            para_parts.append(elem.text or "")
                    elif event == "end" and local == "p":
                        if in_paragraph:
                            para_text = "".join(para_parts)
                            for g in find_glued_words(para_text):
                                findings.append(GlueFinding(
                                    page=None,
                                    paragraph=para_index,
                                    token=g["token"],
                                    reason=g["reason"],
                                    fragment=para_text,
                                ))
                        in_paragraph = False
                        para_parts = []
                    if event == "end":
                        elem.clear()
        except KeyError:
            logger.warning("No word/document.xml in %s", docx_path)
    return [f.to_dict() for f in findings]


# ---------------------------------------------------------------------------
# Rendered page-text scan
# ---------------------------------------------------------------------------

def scan_page_text(page_text_path: Path | str) -> list[dict[str, Any]]:
    """Scan a rendered ``page_text.jsonl`` for glued words.

    Words are grouped into visual lines by (block, line), matching what a human
    sees in the rendered PNG; page/block/line references are reported.
    """
    page_text_path = Path(page_text_path)
    findings: list[GlueFinding] = []
    with page_text_path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line %d", line_no)
                continue

            page = rec.get("page")
            words = rec.get("words") or []
            lines: dict[tuple[int, int], list[str]] = {}
            order: list[tuple[int, int]] = []
            for w in words:
                key = (w.get("block", 0), w.get("line", 0))
                if key not in lines:
                    lines[key] = []
                    order.append(key)
                lines[key].append(str(w.get("text", "")))

            has_word_lines = bool(order)
            if has_word_lines:
                for key in order:
                    fragment = " ".join(lines[key])
                    for g in find_glued_words(fragment):
                        findings.append(GlueFinding(
                            page=page,
                            paragraph=None,
                            block=key[0],
                            line=key[1],
                            token=g["token"],
                            reason=g["reason"],
                            fragment=fragment,
                        ))
            else:
                for para in (rec.get("text") or "").split("\n"):
                    for g in find_glued_words(para):
                        findings.append(GlueFinding(
                            page=page,
                            paragraph=None,
                            token=g["token"],
                            reason=g["reason"],
                            fragment=para,
                        ))

    return [f.to_dict() for f in findings]