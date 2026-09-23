# simulated_xliff — reconstructed retained XLIFF for topo-report-2026

## Why it exists

The real Tikal XLIFF that the translation job produced was deleted long before
group-7 debug retention (`keep_artifacts` / `.diagnostics-retained`) existed.
Without it, `diagnostics locate` had nothing to search, so B2/B3 in
`../bug-journal.md` could not be resolved past "pending (requires retained
XLIFF)".

This directory holds a **simulated retained XLIFF** (`topo.xlf`) reconstructed
from the DOCX pair that *is* preserved in testdata:

- `<source>` = paragraph text from `../original.docx` (word/document.xml)
- `<target>` = paragraph text from `../translated.docx` at the same index
- unit `id="p_<i>"` where `<i>` is the 0-based **`<w:p>` start-event index** in
  word/document.xml — the exact numbering used by
  `glue_scanner.scan_docx_glued_words` (`paragraph: N`) and consistent with the
  leak scanner's per-paragraph iteration.

## Fidelity notes (read before citing unit ids)

1. **Numbering**: the extractor mirrors the glue scanner's
   `start`-event + `end`-append algorithm byte-for-byte, so
   `glue paragraph N == unit p_N` is a guarantee, not a coincidence. Nested
   `<w:p>` (textbox content) get their own ids exactly like the scanner counts
   them; container paragraphs that wrap a textbox emit nothing (scanner
   reproduces the same quirk). This is a deliberate 1:1 mapping to the
   diagnostics tooling surface, NOT a claim about Okapi/Tikal's original ids.
2. **Targets**: a translated paragraph on the translated side populates
   `<target>`; a missing/untranslated one leaves `<target/>`, so
   `is_translated` stays `False` where the LLM produced nothing.
3. **Content**: both XML bodies have identical structure (2949 start-p events,
   2936 emitted ids; 1129 nested paragraphs) — verified during reconstruction,
   so source/target indexing is meaningful.

## How to regenerate

```
python <repo>/scripts/build_simulated_xliff.py \
  ../original.docx ../translated.docx topo.xlf
```

(committed at `scripts/build_simulated_xliff.py` — run from the repo root or any directory; paths are absolute/relative to the caller)

## Verification performed (2026-09-17)

- `diagnostics locate "Вид и объем выполненных работ"` → `p_407` (untranslated)
- `diagnostics locate "СН РК 1.02-04-2013"` → `p_402` (untranslated/whitelisted)
- `diagnostics glue translated.docx` → paragraph 361/365/390/706/1600/1730/1862;
  tokens verified verbatim in `p_361`/`p_365`/`p_390`/`p_706`/`p_1600`/`p_1730`/`p_1862` targets
- Resulting unit ids for B2/B3 recorded in `../bug-journal.md`