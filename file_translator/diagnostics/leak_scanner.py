"""Cyrillic-leak scanner — detects leftover source-language text in a
translated document, honouring the normative-reference whitelist.

Design decisions (user-approved):
- Document names/designations (СН РК, СП РК, ГОСТ, ГКИНП, СНиП, ВСН, ЕНиР, …)
  are intentionally preserved in the source language → whitelisted, NOT flagged.
- List headings and descriptions (e.g. «НПА и технические условия:»,
  «Нормативные документы») MUST be translated → flagged even though they are
  Cyrillic.
- Fragments are scanned per rendered page (respecting the visual layout) or,
  without a render, per paragraph of ``word/document.xml``.

The same whitelist module (:mod:`normative_whitelist`) is the machine-checkable
contract for a future LLM-prompt rule that tells the model which parts stay
untranslated and which must be translated.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from file_translator.diagnostics.normative_whitelist import (
    is_list_heading,
    is_normative_reference,
)

logger = logging.getLogger(__name__)

_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")


@dataclass
class LeakFinding:
    """One flagged fragment of leftover source-language text."""

    page: int | None
    fragment: str
    cyrillic_chars: int
    reason: str  # "untranslated_fragment" | "list_heading"
    block: int | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "block": self.block,
            "line": self.line,
            "fragment": self.fragment,
            "cyrillic_chars": self.cyrillic_chars,
            "reason": self.reason,
        }


def _cyrillic_count(text: str) -> int:
    return len(_CYRILLIC_RE.findall(text))


def _classify_fragment(fragment: str,
                       whitelist_prefixes: Iterable[str] | None) -> LeakFinding | None:
    """Classify one text fragment: return a finding if it is a leak."""
    text = fragment.strip()
    if not text:
        return None
    cyr = _cyrillic_count(text)
    if cyr == 0:
        return None
    # Normative document designation → preserved by design, not a leak.
    if is_normative_reference(text, whitelist_prefixes):
        return None
    reason = "list_heading" if is_list_heading(text) else "untranslated_fragment"
    return LeakFinding(
        page=None, fragment=text, cyrillic_chars=cyr, reason=reason,
    )


def scan_page_text(page_text_path: Path | str,
                   whitelist_prefixes: Iterable[str] | None = None,
                   ) -> list[dict[str, Any]]:
    """Scan a rendered ``page_text.jsonl`` file for Cyrillic leaks.

    Fragments are reconstructed per visual line (block+line grouping of the
    word-coordinate records), so the page context is accurate and matches what
    a human sees in the rendered PNG.
    """
    page_text_path = Path(page_text_path)
    findings: list[LeakFinding] = []
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
            # Group consecutive words into visual lines by (block, line).
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
                    finding = _classify_fragment(fragment, whitelist_prefixes)
                    if finding:
                        finding.page = page
                        finding.block, finding.line = key
                        findings.append(finding)
            else:
                # Fallback: split the raw page text into paragraphs.
                for para in (rec.get("text") or "").split("\n"):
                    finding = _classify_fragment(para, whitelist_prefixes)
                    if finding:
                        finding.page = page
                        findings.append(finding)

    return [f.to_dict() for f in findings]


def scan_docx_leaks(docx_path: Path | str,
                    whitelist_prefixes: Iterable[str] | None = None,
                    ) -> list[dict[str, Any]]:
    """Scan a DOCX's ``word/document.xml`` paragraphs for Cyrillic leaks.

    Page numbers are not known without rendering (pass ``None``); use
    :func:`scan_page_text` for rendered, page-accurate results.
    """
    docx_path = Path(docx_path)
    findings: list[LeakFinding] = []
    with zipfile.ZipFile(str(docx_path)) as zf:
        try:
            with zf.open("word/document.xml") as fh:
                # Streaming paragraph text: iterparse with elem.clear() wipes
                # child .text before the parent <w:p> end event fires, so we
                # accumulate <w:t> text on the fly via start/end events.
                para_parts: list[str] = []
                in_paragraph = False
                for event, elem in ET.iterparse(fh, events=("start", "end")):
                    local = elem.tag.rsplit("}", 1)[-1]
                    if event == "start" and local == "p":
                        para_parts = []
                        in_paragraph = True
                    elif event == "end" and local == "t":
                        if in_paragraph:
                            para_parts.append(elem.text or "")
                    elif event == "end" and local == "p":
                        if not in_paragraph:
                            # Standalone <w:p/> with no start capture (should
                            # not happen — iterparse pairs start/end).
                            elem.clear()
                            continue
                        para_text = "".join(para_parts)
                        finding = _classify_fragment(para_text, whitelist_prefixes)
                        if finding:
                            findings.append(finding)
                        in_paragraph = False
                        para_parts = []
                    # Clear only on end events: clearing a <w:t> at its start
                    # wipes the text before iterparse populates it.
                    if event == "end":
                        elem.clear()
        except KeyError:
            logger.warning("No word/document.xml in %s", docx_path)
    return [f.to_dict() for f in findings]