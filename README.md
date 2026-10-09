# File Translator v2.0.0

Industrial-grade document translation system powered by LLM (OpenAI-compatible API).

Translates **DOCX**, **DOC**, **XLSX**, **XLS** and **PDF** files between English, Russian, Serbian, and Chinese — preserving formatting, fonts, tables, styles, and document structure.

## Features

- **Translation-fidelity guarantees** — no content loss (empty targets fall back to source instead of being dropped), numeric/normative-reference preservation (numbers, dates, `№` references, table numbers), completeness checks (no partially translated / mixed-language units), and a post-merge integrity gate that records findings in the job log
- **Concurrency-safe service** — blocking pipeline work (PDF conversion, Tikal CLI, XLIFF merge) runs off the event loop, so the API stays responsive during long conversions
- **Multi-format support** — DOCX, DOC (LibreOffice), XLSX, XLS (LibreOffice), PDF (First PDF → DOCX)
- **Okapi Tikal integration** — industry-standard XLIFF-based DOCX processing pipeline
- **PDF pipeline** — PDF is converted to DOCX by the companion `first_pdf_converter` service (drives First PDF 6.4 on a Windows host), then runs through the shared DOCX pipeline
- **Named glossaries** — CRUD for term collections with per-entry language validation; CSV import/export (UTF-8 BOM)
- **JWT + LDAP authentication** with RBAC (ADMIN, OPERATOR, VIEWER, API roles)
- **Per-user FIFO job queue** — one job at a time per user, cancellation, queue-position tracking; jobs are server-owned (closing the browser tab does not stop them)
- **Terminal job cleanup** — failed/cancelled jobs are removed from the UI instantly, their reason is written to the activity journal, temp dirs are deleted immediately and the Redis record is removed after a short grace (~60 s)
- **Diagnostics toolkit** — host-side render loop, structure scanner, leak/glue scanners, page→unit locator; debug artifact retention (`DEBUG_KEEP_ARTIFACTS`) keeps XLIFF for after-the-fact analysis
- **Automatic cleanup** — TTL-based temp-dir and job-record cleanup (1 h default)
- **Docker deployment** — API, Redis, MongoDB, frontend (Nginx), ODA converter
- **Frontend SPA** — single-page app with drag-and-drop, activity journal, per-file progress, theme toggle, and an animated auth-screen background

## Supported Formats

| Input | Output | Notes |
|-------|--------|-------|
| .docx | .docx | Okapi Tikal |
| .doc  | .docx | LibreOffice → DOCX |
| .xlsx | .xlsx | lxml ZIP processing |
| .xls  | .xlsx | LibreOffice → XLSX |
| .pdf  | .docx | First PDF converter → DOCX |
| .dwg  | .dwg  | ODA DXF/DWG (see `oda-converter/`) |
| .dxf  | .dwg  | ODA DXF/DWG (see `oda-converter/`) |

## Supported Languages

| Code | Language |
|------|----------|
| en   | English  |
| ru   | Russian  |
| sr   | Serbian  |
| zh   | Chinese  |

## Architecture

```
file_translator/
├── domain/                          # Models, errors, interfaces, glossary, job/journal
├── application/
│   ├── service.py                   # TranslationService — orchestrator (to_thread offload, integrity gate, FILTER_BY_SOURCE)
│   ├── integrity.py                 # Post-merge integrity gate (loss / numeric drift / mixed units)
│   ├── schemas.py                   # Pydantic schemas
│   ├── glossary_service.py          # Glossary CRUD + validation
│   └── user_queue.py                # Per-user FIFO job queue
├── infrastructure/
│   ├── config.py
│   ├── auth/                        # JWT + LDAP auth provider, RBAC
│   ├── providers/openai_provider.py # LLM provider (numeric protection, completeness retry, missing-units retry)
│   ├── translators/
│   │   ├── docx_translator.py       # DOCX via Okapi Tikal
│   │   ├── pdf_translator.py        # PDF → DOCX (composes DocxTranslator)
│   │   ├── xlsx_translator.py       # XLSX via lxml ZIP
│   │   ├── dxf_translator.py        # DXF/DWG
│   │   └── okapi_service.py         # Tikal CLI wrapper + XLIFF save/merge (loss-prevention)
│   ├── converters/
│   │   ├── doc_to_docx_converter.py # LibreOffice CLI (.doc/.xls)
│   │   └── pdf_to_docx_converter.py # First PDF HTTP client (size-based deadline)
│   ├── repositories/                # Redis jobs, MySQL glossary, Mongo auth
│   ├── backends/ezdxf_backend.py    # DXF/DWG via ezdxf/ODA
│   ├── classifiers/                 # XLSX content classifier, CAD token protector
│   └── language_validator.py        # Language detection for glossary values
├── diagnostics/                     # Host-side tooling (not imported by the service)
│   ├── render.py                    # DOCX → PDF → per-page PNG + page_text JSONL
│   ├── structure_scanner.py         # Structural invariants
│   ├── leak_scanner.py              # Leftover source-language fragments
│   ├── glue_scanner.py              # Glued words (lost spaces)
│   ├── unit_locator.py              # Fragment → XLIFF trans-unit
│   ├── normative_whitelist.py       # Normative designations kept as-is
│   ├── numeric_fidelity.py          # Number/normative-token protection and repair
│   └── retention.py                 # Debug artifact retention
└── presentation/api/app.py          # FastAPI routes, middleware, background cleanup

first_pdf_converter/                 # Companion service: PDF→DOCX via First PDF 6.4 (Windows host)
oda-converter/                       # DWG/DXF conversion helper
static/                              # Frontend SPA (HTML/CSS/JS)
testdata/                            # Triage corpus (pairs, bug journal, reports)
```

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- An OpenAI-compatible LLM endpoint (Ollama / vLLM / OpenAI)
- For PDF: the `first_pdf_converter` service running on a Windows host (interactive session)

### Run with Docker Compose (Recommended)

```bash
docker compose up -d --build
```

Services:
- **API**: http://localhost:8000
- **Frontend** (Nginx): http://localhost:3000
- **Redis**: localhost:6379
- **MongoDB**: localhost:27017
- **ODA converter**: internal

Glossary data lives in an external MySQL server (`GLOSSARY_DB_HOST`); auth data in MongoDB.

### Run Locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Optional: accurate language detection (170MB)
pip install lingua-language-detector

uvicorn file_translator.presentation.api.app:app --host 0.0.0.0 --port 8000 --reload
```

### Configuration

Key environment variables (see `.env` / `docker-compose.yml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | — | LLM API base URL |
| `LLM_MODEL_NAME` | — | Model name |
| `LLM_API_KEY` | — | API key |
| `REDIS_HOST` / `REDIS_PASSWORD` | `redis` / — | Redis connection |
| `JOB_TTL_SECONDS` | `3600` | Job-record TTL |
| `GLOSSARY_DB_*` | — | External MySQL glossary |
| `JWT_SECRET` | — (required) | JWT signing secret |
| `LDAP_*` | — | LDAP (Active Directory) settings |
| `FIRST_PDF_CONVERTER_URL` | `http://172.17.106.164:8001` | PDF→DOCX converter base URL |
| `DEBUG_KEEP_ARTIFACTS` | off | Keep job XLIFF/temp dirs for diagnostics |

On the converter host: `FIRST_PDF_TIMEOUT=180` (recommended floor; per-size deadlines come from the client's `X-Timeout-Seconds` header, capped by `FIRST_PDF_MAX_TIMEOUT`).

## API Endpoints

### Document Translation / Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/jobs` | Upload one file for translation |
| POST | `/jobs/batch` | Upload multiple files |
| GET | `/jobs` | List the user's jobs |
| GET | `/job/{job_id}` | Job status, stage, progress, queue position |
| POST | `/job/{job_id}/cancel` | Cancel a pending/running job |
| GET | `/job/{job_id}/download` | Download the translated file |
| POST | `/translate` | Synchronous single-file translation |
| POST | `/validate` | Validate a document without translating |
| GET | `/supported-formats` | Supported formats |

### Glossary

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/glossary/collections` | List collections |
| GET/POST | `/glossary` | List / add entries |
| GET/PUT/DELETE | `/glossary/{entry_id}` | Read / update / delete entry |
| POST | `/glossary/import` | Import from CSV |
| GET | `/glossary/export` | Export to CSV |

### Auth & System

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` · `/api/auth/refresh` · `/api/auth/logout` | JWT login / refresh / logout |
| GET | `/api/auth/me` | Current user |
| GET | `/health` · `/version` · `/help` | Health / version / help |
| GET | `/journal` · `/journal/{date}` | Activity journal (any authenticated user) |
| GET/POST | `/support/feedback` | Support feedback |

## Test Suite

```bash
pytest                           # All tests (437 passed, 5 skipped)
pytest -v                        # Verbose
pytest --cov=file_translator     # Coverage report
```

## Translation Pipeline

1. **Extract** — parse the document (Okapi Tikal for DOCX; lxml ZIP for XLSX; PDF is first converted to DOCX by `first_pdf_converter`)
2. **Filter (optional)** — in `filter_source` mode, units are kept whenever they contain source-script characters (language detection is advisory), so source text is never silently left untranslated
3. **Batch & translate** — unit batches are sent to the LLM with numeric tokens protected by placeholders; after parsing, placeholders are restored and drift is verified/repaired, partially translated units are retried, and units the model omitted are re-requested
4. **Apply** — translated targets are written into the XLIFF with inline-code-aware distribution; any unit left with an empty target receives a **source fallback** (no content loss)
5. **Merge** — Tikal merges the translated XLIFF back into DOCX
6. **Post-process** — CJK fonts → Arial, table row heights `exact` → `atLeast`
7. **Integrity gate** — compares original vs translated (loss / numeric drift / mixed units); findings are logged and critical ones fail the job instead of shipping silently

## PDF Translation

PDF files are translated by composing the shared DOCX pipeline:

1. `PdfTranslator` calls the `first_pdf_converter` HTTP service (`POST /convert`) with a size-based deadline (`X-Timeout-Seconds`, cap `FIRST_PDF_MAX_TIMEOUT`)
2. The service converts PDF → DOCX via First PDF 6.4 (UI automation) on a Windows host
3. The resulting DOCX runs through extract → translate → save → integrity gate

See `first_pdf_converter/README.md` for the converter service details.

## Diagnostics Toolkit

Host-side, read-only tooling for triaging translation-time structure breakage (`python -m file_translator.diagnostics`):

- `render` / `render-pair` — DOCX → PDF → per-page PNG + `page_text.jsonl` (text with coordinates)
- `scan` — structural invariants (paragraph/table/row/media/text-node/tab counts, page count)
- `leaks` — leftover source-language fragments (with the normative whitelist)
- `glue` — glued words (lost inter-word spaces)
- `locate` — map a fragment from a rendered page to XLIFF trans-units

The `testdata/` directory holds the triage corpus (document pairs, `bug-journal.md`, combined `report.json`).

## Glossary Language Validation

Each glossary column is validated against its expected language:

- `ru_word` → Russian (Cyrillic)
- `en_word` → English (Latin)
- `sb_word` → Serbian (Cyrillic or Latin)
- `ch_word` → Chinese (CJK)

Uses optional `lingua-language-detector` (ML-based, 170MB) or a character-set heuristic fallback.

## License

MIT
