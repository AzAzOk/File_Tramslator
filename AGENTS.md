# AGENTS.md — Project memory for AI coding assistants

## Project
File Translator v2.0.0 — FastAPI backend that translates DOCX files via LLM (OpenAI).

## Architecture
- `file_translator/domain/` — models, errors, interfaces
- `file_translator/application/` — service, schemas, validators
- `file_translator/infrastructure/` — providers (OpenAI), translators (DOCX), repositories (MySQL journals/glossary)
- `file_translator/presentation/` — FastAPI routes
- `frontend/` — React + Vite + TypeScript + Tailwind v4 SPA
- Database: MySQL (glossary, jobs, journal)
- LLM: OpenAI with `gpt-4o-mini`
- Storage local (`./uploads`, `./output`, `./logs` journal JSON files)
- Auth: stub only (no real auth)

## Key Decisions
- FILTER_BY_SOURCE: **server-side + prompt**. Before batching, `service.py` runs `detect_language()` on each text unit and skips those not matching source language. The LLM prompt still carries the filter instruction as a safety net. This fixes unreliable LLM-only filtering (Russian → Chinese, missed English).
- Boundary markers `<<<INPUT_TEXT>>>` / `<<<END_INPUT_TEXT>>>` wrap all input data
- `_clean_model_output()` strips markers + common boilerplate (`"Вот перевод:"`, `"Sure, here is:"`)
- Style prompts: TECHNICAL (GOST/ISO), LEGAL (formal), MIXED (auto-detect)
- Temperature per style: LEGAL=0.0, TECHNICAL=0.1, MIXED=0.3
- JSON robustness: `_fix_json_syntax()` inserts missing commas between `}{`, `first_brace`/`last_brace` extraction
- max_file_size = 128 MB
- Download uses `Content-Disposition: attachment`
- Output format mapping: DXF/DWG→.dwg, PDF→.pdf, DOCX/DOC→.docx, XLSX/XLS→.xlsx

## History of Changes (pre‑Okapi era)
> **Note**: The entries below describe the **old architecture** where DOCX XML was manipulated directly via ElementTree. As of 2026-06-23, **all DOCX XML manipulation is delegated to Okapi Tikal CLI** (`okapi_service.py`). These historical entries are kept for context but the code no longer contains `_merge_runs_within_paragraph`, `_extract_from_paragraphs`, `_distribute_words`, or `_register_all_namespaces`. The only remaining direct fix is `_post_process_docx()` (CJK fonts + table heights).

### DOCX header/footer + textbox fix
**Problem**: Before fix, `_extract_from_file` had `if is_main_document:` guard — headers/footers were completely skipped (not extracted at all).
**Fix (1)**: Removed guard so headers/footers are processed through `_extract_from_paragraphs`, `_extract_from_tables`, `_extract_from_textboxes`. Units tagged with `is_in_header_footer=True`.

**Problem**: `<w:t>` inside `<w:txbxContent>` was extracted as part of paragraph/cell text → double-counted text.
**Fix (2)**: `_extract_from_paragraphs`/`_extract_from_tables` exclude `<w:t>` in `<w:txbxContent>` via set subtraction. New `_extract_from_textboxes()` extracts text boxes as separate units with `tb_` prefix.

**Problem (found later)**: `unit_id` in extraction methods was local per file — `p_0` appeared in document.xml, header1.xml, AND footer1.xml. `translate()` matched by ID, so ONE dict key (`"p_0"`) modified MULTIPLE files' `<w:t>` elements → footer text overwrote header and first body paragraph.
**Fix (3)**: `extract()` now manages a global `unit_counter`. Passed through `_extract_from_file` → all 3 extraction methods. IDs are globally unique: `p_0` (document), `p_3` (header), `p_4` (footer), etc.

### DOCX run fragmentation fixes
**Problem**: LibreOffice .doc → .docx conversion fragments `<w:t>` within `<w:r>` (word across 3-8 fragments). `translate()` proportional distribution with `max(1, ...)` gave each fragment ≥1 character → character-level fragmentation.
**Fix (4)**: New `_merge_runs_within_paragraph()` merges `<w:t>` within each `<w:r>` into one. Called in all 3 extraction methods.

**Problem**: `_extract_from_tables()` grouped ALL paragraphs in a cell into ONE unit. Fix (4)'s `>5` threshold in `translate()` then killed all but the first paragraph.
**Fix (5)**: `_extract_from_tables()` now produces one unit per `<w:p>` within `<w:tc>`, matching `_extract_from_paragraphs` granularity. `>5` threshold in `translate()` reverted.

**Problem**: `full_text = "".join(t_elements).strip()` stripped trailing whitespace from `original_text`. Proportional distribution then gave whitespace-only runs `""` → run disappeared from output.
**Fix (6)**: `strip()` only used for skip-check (`if not full_text.strip(): continue`); `original_text` keeps all whitespace, so whitespace runs get proportional translation share.

### Relevant files
- `file_translator/infrastructure/translators/docx_translator.py` — all DOCX extraction/save logic
- `file_translator/infrastructure/providers/openai_provider.py` — boundary markers, style prompts, temperature, JSON fix, FILTER_BY_SOURCE
- `file_translator/application/service.py` — output format map (`_OUTPUT_FORMAT_MAP`)
- `file_translator/application/schemas.py` — version 2.0.0, GlossaryUpdateSchema
- `file_translator/presentation/api/app.py` — glossary CRUD, import/export stubs, download

### DOC support via LibreOffice conversion
**.doc files are now supported** — automatically converted to .docx via LibreOffice CLI.

**What was changed**:
- `Dockerfile` — added `libreoffice-writer` package
- `file_translator/infrastructure/converters/doc_to_docx_converter.py` — `LibreOfficeConverter` wrapping `soffice --headless --convert-to` with UUID-based profile; supports .doc→.docx and .xls→.xlsx
- `file_translator/infrastructure/translators/docx_translator.py` — `SUPPORTED_FORMATS` includes `DocumentFormat.DOC`; `can_process()` accepts `.doc`; `extract()` converts `.doc` → `.docx` before standard extraction
- `file_translator/presentation/api/app.py` — endpoint accepts `.doc` along with `.docx`

**Output**: always `.docx` (`.doc` → `.docx` in `_OUTPUT_FORMAT_MAP`).

## Frontend (Static HTML SPA)

Main UI at `static/index.html`, served directly by FastAPI (`StaticFiles` mount at `/`).

**Single page** (Главная): file upload (drag-and-drop), language config, style/mode/glossary settings, activity log. Dark/light theme toggle with `localStorage` persistence.

**Tech**: Vanilla HTML/CSS/JS + Tailwind CDN + Google Fonts (Geist, Space Grotesk, JetBrains Mono) + Material Symbols.

### Frontend status matching (critical case fix)
**Problem**: `JobStatus` enum values are lowercase (`"completed"`, `"failed"`, `"pending"`, `"running"`, `"cancelled"`), but JS checked uppercase `'COMPLETED'` → polling never stopped, download button never appeared.
**Fix**: All status comparisons in `updateJobStatusUI`, `pollJobStatus`, `downloadLastCompleted`, `updateMainDownloadButton` use lowercase strings matching API.
**Architecture detail**: frontend runs as separate nginx container (`file-translator-frontend`) proxying API paths to `file-translator-api`. Access via port 3000 for nginx or port 8000 for FastAPI directly. `./static` is volume-mounted in nginx (port 3000) but was baked into API image (port 8000) — added volume mount `./static:/app/static:ro` in API service for development.

### Frontend bug list
(No BUGS.md — all known issues have been addressed.)

### `_merge_runs_within_paragraph()` — style-group consolidation (2026-06-17)
**Problem (old)**: LibreOffice .doc → .docx fragments `<w:t>` within `<w:r>` → `_merge_runs_within_paragraph` was needed.

**Problem (new)**: Proportional character distribution `max(1, int(len * ratio))` fragmented words letter-by-letter when runs had small proportions. Simple "first run gets all" broke per-run formatting (bold, italic, font changes).

**Fix**: `_merge_runs_within_paragraph` now does two things:
1. Merges `<w:t>` within each `<w:r>` (old behaviour)
2. Groups consecutive `<w:r>` with IDENTICAL `<w:rPr>` (run properties) and merges their `<w:t>` into the first `<w:r>` of each group; subsequent `<w:r>` in the group are removed from the paragraph's XML tree.

`_distribute_words()` in `translate()` then distributes the translated text at **word boundaries** across the remaining `<w:t>` elements (now one per distinct style group). Non-whitespace character proportion per group determines how many words each group receives. This preserves style boundaries while preventing character-level fragmentation.

### `_extract_from_paragraphs` — exclude table paragraphs (2026-06-17)
**Problem**: `_extract_from_paragraphs` iterates `root.iter(w:p)` which returns ALL paragraphs including those inside `<w:tc>` (table cells). `_extract_from_tables` then extracts the same paragraphs again → duplicate units translated twice, writes overwriting each other.

**Fix**: `_extract_from_paragraphs` builds a set of `id(p)` for all `<w:p>` inside `<w:tc>` elements, and skips them via `if id(p_elem) in table_paragraph_ids: continue`.

### `_distribute_words()` — word-level distribution with edge case handling (2026-06-17)
**Problem (1)**: `word_count <= 1` shortcut dumped everything into the first style group, wiping out the second group's formatting entirely (e.g. `"AGREED WITH:"` in bold+normal → all bold).
**Fix (1)**: Removed `word_count <= 1` special case. Always distributes proportionally.

**Problem (2)**: When `word_count < number_of_content_groups`, `max(1, round(...))` tried to give every group ≥1 word but ran out — overflow consumed words from remainder.
**Fix (2)**: New branch handles `word_count <= len(content_groups)` by sorting groups by original content length and distributing one word at a time to the heaviest groups. Empty groups get `""`.

### `_translate_with_split` — recursion depth limit (2026-06-17)
**Problem**: No bound on recursive splitting. If a single-unit batch consistently returns incomplete results, infinite `translate_batch → _translate_with_split` recursion causes stack overflow.

**Fix**: `_split_depth` parameter (default 0) propagated through call chain. `translate_batch` returns `[]` at depth > 3. All split calls pass `depth + 1`.

### `save()` — XML namespace registration (2026-06-17)
**Problem**: `ET.write()` replaces registered namespace prefixes (`w:`, `r:`, `mc:`, etc.) with auto-generated `ns0:`, `ns1:`, … → Word rejects the file. Hardcoded list missed ~9 namespaces (`m:`, `w14:`, `mo:`, `wp14:`, etc.) → formula elements and other features broken.

**Fix**: `_register_all_namespaces()` dynamically scans all XML files in `temp_dir` via `re.findall(rb'xmlns:(\w+)=["\']([^"\']+)["\']', ...)` and registers every found prefix with `ET.register_namespace()`. Covers any DOCX variant (old Word, Mac Office, formulas, extended styles).

### Per-user job queue (2026-06-22)
**Problem**: Multiple files from one user (or admin) ran concurrently via `asyncio.create_task` — no rate limiting on OpenAI API calls. Batch uploads didn't exist.
**Fix**: New `UserJobQueue` (`file_translator/application/user_queue.py`) manages per-user FIFO queues. Each user gets one background worker that processes jobs sequentially. Queue positions are tracked per `job_id` and exposed via `queue_position` field in `GET /job/{job_id}` and `POST /jobs` responses.
**Batch endpoint**: `POST /jobs/batch` accepts `list[UploadFile]`, creates individual jobs for each file, enqueues all for the user, returns `BatchJobCreateResponseSchema`.
**Frontend**: Queue position shown in file cards ("⏳ Очередь: позиция N"). Cancelling a pending job also cleans up queue tracking.

### SimSun → Arial font substitution (2026-06-22)
**Problem**: Tikal explicitly injects `w:eastAsia="SimSun"` into runs containing CJK translated text.
**Fix**: `DocxTranslator.save()` calls `_post_process_docx()` after Tikal merge. Uses regex to match attributes `w:eastAsia`, `w:ascii`, `w:hAnsi` and replaces 10 CJK fonts (SimSun, SimHei, MingLiU, MS Mincho, MS Gothic, DengXian, FangSong, KaiTi, NSimSun, PMingLiU) with Arial. Only processes XML and .rels files in the DOCX ZIP archive. No XML parsing needed — regex catches all occurrences in any attribute or text node.

### Table row height optimization (2026-06-23)
**Problem**: Translated text can expand, causing overflow in table rows with `w:hRule="exact"` (the "Exactly" checkbox in Word's table row properties). Text becomes invisible when the row has a fixed height.
**Fix**: `_post_process_docx()` also changes `w:hRule="exact"` → `"atLeast"` in all XML files, allowing rows to expand as needed while keeping the original minimum height.

### Combined post-processing optimization (2026-06-23)
**Problem**: Previously `_fix_cjk_fonts_in_docx()` and `_fix_table_row_heights()` were called sequentially in `save()`, each opening/closing the DOCX ZIP archive separately — reading all files into memory twice.
**Fix**: Combined both fixes into a single `_post_process_docx()` method that processes the DOCX archive in one pass: read once → modify XML files (both CJK + height) → write back atomically via temp file + os.replace. Reduced I/O operations by 50%.

## Test suite (2026-07-03)
**89 tests** (1 skipped — requires running API server).

| Layer | File | Tests |
|-------|------|-------|
| Domain models | `tests/unit/test_domain_models.py` | 7 |
| DocxTranslator unit | `tests/unit/test_docx_translator.py` | 7 |
| OpenAI provider unit | `tests/unit/test_translation_provider.py` | 3 |
| OkapiService unit | `tests/unit/test_okapi_service.py` | 28 (XLIFF parse/save, plain‑text extraction, inline‑code distribution) |
| Docx pipeline integration | `tests/integration/test_docx_pipeline.py` | 26 (can_process, post‑process, extract→translate→save with mocked Tikal) |
| XlsxTranslator unit | `tests/unit/test_xlsx_translator.py` | 12 (extract, translate, save, shared strings, inline, comments, rich text, empty, serialization no‑ns0) |
| API E2E | `tests/e2e/test_api_endpoints.py` | 1 passed (health check), 1 skipif (translate — requires Tikal CLI) |

**Coverage highlights**:
- `_post_process_docx()` tested with real DOCX ZIPs: all 10 CJK fonts → Arial, `w:hRule="exact"` → `"atLeast"`, both combined, no‑op check
- `_set_target_with_inline_codes()` tested: plain text, `<g>` tags, `<x/>`/`<bx/>` tags, attribute preservation, remainder overflow
- Pipeline error paths tested: `TikalNotAvailableError` → `DocumentParseError` / `SaveDocumentError`, `save_xliff` failure → errors field

### XlsxTranslator + lxml 6.1.1 namespace fix (2026-07-03)
**Problem**: `_register_namespaces()` called `etree.register_namespace("", uri)`, but **lxml 6.1.1** raises `ValueError: Invalid tag name ''` for an empty prefix → module import failed → every `.xlsx` job died with `initialization error: Invalid tag name ''`.

**Fix**: Removed the empty-prefix registration entirely. `_serialize_xml()` now post‑processes the serialized output with regex: it finds any auto‑generated `ns0`/`ns1` prefix that maps to the spreadsheet namespace and rewrites it to a clean default namespace declaration (`xmlns="..."`).

**Impact on other namespaces**: `r:` prefix registration (`etree.register_namespace("r", ...)`) still works — only the empty prefix is banned in lxml ≥ 6.

## Code review findings (2026-06-23)
Minor issues fixed:
- WARNING‑level logs with translation content → DEBUG level (`openai_provider.py:227-228`)
- Inline stdlib imports (`zipfile`, `os`, `re`, `shutil`, `platform`) moved to module level in `docx_translator.py` and `okapi_service.py`
- Hidden test `text_text_distribution_across_g_tags` → `test_text_distribution_across_g_tags` (typo hid it from pytest)
- AGENTS.md now clarifies the Okapi migration vs old docs
- `__init__.py` files added to all test subdirectories (e2e, integration, unit)
- Tikal output file discovery now uses single glob `*.out.*` in isolated temp directory — no more fallback chain ambiguity

Remaining (low priority / out of scope):
- Temp dir cleanup in every exception handler before re‑raise — masks original error location in traceback

### Glossary language validation (2026-07-03)
**LanguageValidator** — validates each glossary column value matches its expected language:
- `ru_word` → Russian (`"ru"`)
- `en_word` → English (`"en"`)
- `sb_word` → Serbian (`"sr"`, accepts Cyrillic and Latin)
- `ch_word` → Chinese (`"zh"`)

**Two detection backends**:
1. **`lingua-language-detector`** (optional, 170MB) — accurate ML-based detection. Add uncommented to `requirements.txt` to enable.
2. **Character-set heuristic fallback** — regex-based detection using Unicode ranges (Cyrillic, Latin, CJK). Always available.

**Integration points**:
- `GlossaryService.add_entry()` calls `_validate_language()` before `_check_duplicate()`
- `GlossaryService.update_entry()` calls `_validate_language()` before `_check_duplicate()`
- Both raise `ValueError` with Russian-language error message on mismatch

**Files**: `file_translator/infrastructure/language_validator.py`
**Tests**: `file_translator/tests/unit/test_language_validator.py` (23 tests)

### Implemented
- Named glossaries (CRUD with collections)
- User preferences (auth-based)

## Still to do
- PDF, DXF/DWG translators (deferred)
- `new_collection_name` stub in import — table creation not implemented (requires CREATE TABLE LIKE + ACL update)

## Implemented (glossary import/export — 2026-07-02)
- **Export**: `GET /glossary/export?collection_id=X` — returns UTF-8 BOM CSV with columns `ru_word,en_word,sb_word,ch_word`. Logged to journal.
- **Import**: `POST /glossary/import` — accepts CSV file + `collection_id` form fields. Parses CSV (UTF-8 BOM tolerant), validates all 4 columns required per row, appends entries via existing `add_entry` path. Returns `GlossaryImportResponseSchema` with count + per-row errors. Logs to journal.
- **Architectural stub**: `new_collection_name` form field exists in import endpoint. Currently no-op with TODO comment showing where table creation would go (CREATE TABLE LIKE + collection registration).
- **Frontend**: Two new buttons in glossary tab header ("Экспорт CSV", "Импорт CSV"). Export triggers browser download. Import opens modal with collection selector + file picker + submit.

## File cleanup & TTL (2026-07-02)
**All temp files and Redis job records are deleted 1 hour after creation**, regardless of status.

### Cleanup triggers
| Trigger | What's deleted | When |
|---------|---------------|------|
| Job fails | Temp dir (`translator_*`) | Immediate (`_run_translation_job` catch block) |
| Job cancelled | Temp dir | Immediate (`POST /job/{id}/cancel`) |
| File downloaded | Entire temp dir | After `FileResponse` via `BackgroundTasks` |
| Periodic sweep | `translator_*`, `docx_okapi_*`, `tikal_*` dirs older than 1h | Every 30 min via `_cleanup_orphaned_temp_dirs` |
| Redis TTL | Job record `job:<uuid>` | Auto-expire after 1h (configurable via `JOB_TTL_SECONDS`) |

### What changed
- `redis_job_repository.py`: `_TERMINAL_TTL = 3600` (was 604800 — 7 days), `_MAX_TTL = 3600` (was 172800 — 2 days)
- `app.py`: `_cleanup_orphaned_temp_dirs()` now scans filesystem for ALL `translator_*`, `docx_okapi_*`, `tikal_*` temp dirs older than 1 hour + safety-net Redis job cleanup. Runs every 1800s (was 7200s).
- **Frontend**: Already removes terminal jobs from `activeJobs` after 1 hour in-memory. On page reload, only non-terminal jobs are restored — with Redis TTL=1h, stale jobs won't reappear.

### Key files
- `file_translator/infrastructure/repositories/redis_job_repository.py` — TTL constants
- `file_translator/presentation/api/app.py` — `_cleanup_orphaned_temp_dirs()`, `_cleanup_temp_dir()`, download cleanup via `BackgroundTasks`
- `file_translator/infrastructure/translators/xlsx_translator.py` — `XlsxTranslator` (ZIP + lxml, shared strings, inline strings, comments, rich text)
- `file_translator/infrastructure/converters/doc_to_docx_converter.py` — `LibreOfficeConverter` (.doc→.docx, .xls→.xlsx)
