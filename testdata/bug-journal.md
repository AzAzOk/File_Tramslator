# Bug journal — structure-fidelity triage

Schema: `id | case | page | category | fragment | unit id | hypothesis | status`

Status values: `open`, `localized (unit ids known)`, `seed-data-only` (measured
by a different method than the current scanner — awaiting re-verification with
`diagnostics` tooling), `fixed`, `regression-verified`, `accepted` (intentional
behavior, e.g. normative whitelist).

---

## B1 — Page drift (33 → 37)

- **id**: B1
- **case**: topo-report-2026
- **page**: rendered overview (33 vs 37 pages)
- **category**: layout / pagination
- **fragment**: whole document
- **unit id**: n/a (multi-unit)
- **hypothesis**: translation length growth changes pagination; or Tikal merge adds/removes paragraph-level spacing.
- **status**: seed-data-only

## B2 — Untranslated normative block, page 9

- **id**: B2
- **case**: topo-report-2026
- **page**: 9
- **category**: untranslated-content / LLM skip
- **fragment**: «Закон РК…», «ГКИНП…», «Условные знаки…», «Правила по технике безопасности…», heading «Вид и объем выполненных работ:», table headers «П/П», «Наименования работ» (~591 Cyrillic chars across 24 `<w:t>`)
- **unit id**: `p_401`, `p_404`, `p_405`, `p_406`, `p_407`, `p_411`, `p_412` (resolved via `diagnostics locate` on the reconstructed XLIFF — see below); `p_402`/`p_403` (СН РК / СП РК) are **whitelisted by design** (`normative_whitelist`) and excluded from leak findings
- **note on artifact**: original Tikal XLIFF predates group-7 debug retention and is gone; unit ids resolved against the **simulated retained XLIFF** (`simulated_xliff/topo.xlf`) reconstructed from the DOCX pair (source = original paragraphs, target = translated paragraphs, ids = glue-scanner paragraph indices). Affected units carry `target` populated with the Cyrillic source text verbatim → `is_translated=True` per `locate`'s text-presence check, but effectively untranslated.
- **hypothesis**: units inside the block were skipped by LLM (FILTER_BY_SOURCE or batch failure) and the XLIFF target was populated with source verbatim downstream (water-fill / Tikal merge). Heading must translate per user decision; whitelisted norm titles must NOT.
- **status**: localized (unit ids known)

## B3 — Glued words (missing inter-word spaces)

- **id**: B3
- **case**: topo-report-2026
- **page**: multiple
- **category**: text-fidelity / space loss
- **fragment**: `fortheproject`, `createan up-to-date`, and similar concatenations
- **unit id**: `p_361` (`fortheproject`), `p_365` (`createan`, `up-to-dateengineeringtopographic`), `p_390` (`ofLLC`), `p_706` (`theinitial`), `p_1600` (`andSP`), `p_1730` (`ofthesite`), `p_1862` (`fortheproject`) — resolved via `diagnostics glue` (paragraph index) + confirmed present in the corresponding unit's `<target>` of `simulated_xliff/topo.xlf`
- **note on artifact**: same reconstruction as B2 (original Tikal XLIFF lost before group-7 retention). Glue scanner's 0-based `start`-event paragraph indices map 1:1 to XLIFF unit ids.
- **hypothesis**: space-loss inside a single text element (LLM output or XLIFF save water-fill), not cross-run merge — confirmed: the glued token exists verbatim inside one unit's target (`p_361`, etc.).
- **status**: localized (unit ids known)

## B4 — Tab-stop delta (recon: 0 → 131)

- **id**: B4
- **case**: topo-report-2026
- **page**: n/a (XML-level)
- **category**: representation / tab injection
- **fragment**: 34 paragraphs with tab stops in translated output
- **unit id**: pending
- **hypothesis**: Tikal merge or First-PDF conversion injects `<w:tab/>`. NOTE: scanner (structure_scanner.profile_docx) currently counts **258 effective tab elements in document.xml in BOTH files** — the recon method (0→131) measured something narrower (e.g. visible tabs in extracted text outside tables). To be re-verified with the diagnostics scanner before fixing.
- **status**: seed-data-only

## B5 — Text-element count drop (2116 → 1694)

- **id**: B5
- **case**: topo-report-2026
- **page**: n/a (XML-level)
- **category**: structure / node loss
- **fragment**: −422 `<w:t>` elements (2116 → 1694), paragraphs/tables/rows/media intact
- **unit id**: n/a (multi-unit)
- **hypothesis**: Tikal merge consolidates runs (fewer but longer `<w:t>`). Topological invariants hold; this is a count-only warning per design, not a hard failure.
- **status**: seed-data-only