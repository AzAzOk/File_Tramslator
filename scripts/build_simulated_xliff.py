"""Reconstruct a simulated retained XLIFF from a translated DOCX pair.

The real Tikal XLIFF predecessor is deleted by the 1-hour orphan sweep unless
group-7 debug retention was enabled for the job. When it was not (the topo pair
predates retention), the preserved DOCX pair is the only evidence left. This
script rebuilds a faithful approximation whose trans-unit ids match the
diagnostics scanner numbering EXACTLY (see
``testdata/topo-report-2026/simulated_xliff/README.md``):

- unit id == index over *start* events of `w:p` in word/document.xml
  (0-based), the same convention used by ``glue_scanner`` (``para_index``).
- source = original.docx paragraph text; target = translated.docx paragraph
  text at the same id (structural layout is identical between the pair members
  for translated jobs — verified: 2949/2949 paragraphs).

Run:
    python scripts/build_simulated_xliff.py <orig.docx> <trans.docx> <out.xlf>
"""
from __future__ import annotations

import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
W_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"


def extract_paragraph_map(docx_path: str | Path) -> dict[int, str]:
    """Return {start-index: paragraph text} using the glue scanner's algorithm."""
    result: dict[int, str] = {}
    parts: list[str] = []
    in_para = False
    para_index = -1
    with zipfile.ZipFile(docx_path) as zf:
        with zf.open("word/document.xml") as fh:
            for event, elem in ET.iterparse(fh, events=("start", "end")):
                if event == "start" and elem.tag == W_P:
                    parts = []
                    in_para = True
                    para_index += 1
                elif event == "end" and elem.tag == W_T and in_para:
                    parts.append(elem.text or "")
                elif event == "end" and elem.tag == W_P and in_para:
                    result[para_index] = "".join(parts)
                    in_para = False
                    parts = []
                    elem.clear()
    return result


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_xliff(orig: dict[int, str], trans: dict[int, str]) -> str:
    ids = sorted(set(orig) | set(trans))
    lines: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<xliff version="1.1">',
        '  <file source-language="ru" target-language="en" datatype="plaintext"',
        '        original="topo-report-2026.docx">',
        '    <body>',
    ]
    for i in ids:
        src = orig.get(i, "")
        tgt = trans.get(i, "")
        lines.append(f'      <trans-unit id="p_{i}">')
        lines.append(f'        <source>{xml_escape(src)}</source>')
        lines.append(f'        <target>{xml_escape(tgt)}</target>')
        lines.append('      </trans-unit>')
    lines.append('    </body>')
    lines.append('  </file>')
    lines.append('</xliff>')
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    orig_path, trans_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    orig_map = extract_paragraph_map(orig_path)
    trans_map = extract_paragraph_map(trans_path)
    print(f"original start-p ids: {len(orig_map)}")
    print(f"translated start-p ids: {len(trans_map)}")
    print(f"ids in both: {len(set(orig_map) & set(trans_map))}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(build_xliff(orig_map, trans_map), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())