# File Translator v2.0.0

Industrial-grade document translation system powered by LLM (OpenAI-compatible API).

Translates **DOCX**, **XLSX**, **DOC** and **XLS** files between English, Russian, Serbian, and Chinese — preserving all formatting, fonts, tables, styles, and document structure.

## Features

- **Lossless translation** — preserves formatting, fonts, styles, tables, comments, rich text
- **Multi-format support** — DOCX, XLSX, DOC (via LibreOffice), XLS (via LibreOffice)
- **Okapi Tikal integration** — industry-standard XLIFF-based document processing pipeline
- **Named glossaries** — CRUD for term collections with per-entry language validation
- **Glossary import/export** — CSV import/export with UTF-8 BOM support
- **Asynchronous job queue** — per-user FIFO queues with cancellation and positional tracking
- **Automatic file cleanup** — TTL-based temp dir and Redis job record cleanup (1 hour)
- **Content classifier** — detects sheets/tables vs. plain text for optimal XLSX processing
- **Language validation** — optional ML-based (`lingua-language-detector`) or character-set heuristic fallback
- **Docker deployment** — multi-service architecture (API, Redis, MySQL, Nginx, LibreOffice)
- **Frontend SPA** — single-page application with drag-and-drop upload

## Supported Formats

| Input | Output |
|-------|--------|
| .docx | .docx |
| .doc  | .docx |
| .xlsx | .xlsx |
| .xls  | .xlsx |
| .pdf  | .pdf  |
| .dwg  | .dwg  |
| .dxf  | .dwg  |

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
├── domain/                          # Business logic, models, interfaces
│   ├── models.py                    # TextUnit, LanguageCode, etc.
│   ├── errors.py                    # Custom exception hierarchy
│   ├── glossary.py                  # Glossary, GlossaryEntry, GlossaryCollection
│   └── interfaces.py                # Abstract contracts (Translator, Provider, Repos)
│
├── application/                     # Use cases and orchestration
│   ├── service.py                   # TranslationService — main orchestrator
│   ├── schemas.py                   # Pydantic validation schemas
│   ├── glossary_service.py          # Glossary CRUD + term substitution
│   ├── user_queue.py                # Per-user FIFO job queue
│   └── validators.py                # Input validators
│
├── infrastructure/                  # External integrations
│   ├── config.py                    # Configuration management
│   ├── translators/
│   │   ├── docx_translator.py       # DOCX via Okapi Tikal CLI
│   │   └── xlsx_translator.py       # XLSX via lxml ZIP manipulation
│   ├── converters/
│   │   └── doc_to_docx_converter.py # LibreOffice CLI wrapper
│   ├── providers/
│   │   └── openai_provider.py       # OpenAI-compatible LLM provider
│   ├── repositories/
│   │   ├── redis_job_repository.py  # Job persistence in Redis
│   │   └── mysql_glossary_repository.py  # Glossary storage in MySQL
│   ├── okapi_service.py             # Okapi Tikal CLI wrapper
│   ├── language_validator.py        # Language detection for glossary values
│   └── classifiers/
│       └── xlsx_content_classifier.py  # Content type detection for XLSX
│
├── presentation/                    # FastAPI application
│   └── api/
│       └── app.py                   # Routes, middleware, startup
│
├── tests/
│   ├── unit/                        # 83+ unit tests
│   ├── integration/                 # Integration tests (mocked Tikal)
│   └── e2e/                         # End-to-end API tests
│
├── static/                          # Frontend SPA (HTML/CSS/JS)
├── docker-compose.yml               # Multi-service deployment
├── Dockerfile                       # API container build
├── nginx.conf                       # Nginx reverse proxy config
└── requirements.txt                 # Python dependencies
```

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- OpenAI-compatible API (e.g., Ollama, OpenAI, vLLM)

### Run with Docker Compose (Recommended)

```bash
docker-compose up --build
```

Services:
- **API**: http://localhost:8000
- **Frontend** (via Nginx): http://localhost:3000
- **Redis**: localhost:6379
- **MySQL**: localhost:3306

### Run Locally

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt

# Optional: install language detection (170MB)
pip install lingua-language-detector

# Start
uvicorn file_translator.presentation.api.app:app --host 0.0.0.0 --port 8000 --reload
```

### Configuration

Key environment variables (see `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `http://172.16.101.90:11434/v1` | LLM API base URL |
| `LLM_MODEL_NAME` | `gpt-4o-mini` | Model name |
| `LLM_API_KEY` | — | API key |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `MYSQL_DSN` | — | MySQL connection string |
| `JOB_TTL_SECONDS` | `3600` | Job record TTL |

## API Endpoints

### Document Translation

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/jobs` | Upload file for translation |
| POST | `/jobs/batch` | Upload multiple files |
| GET | `/jobs` | List user jobs |
| GET | `/job/{id}` | Job status + queue position |
| POST | `/job/{id}/cancel` | Cancel pending job |
| GET | `/download/{id}` | Download translated file |
| GET | `/supported-formats` | List supported file formats |

### Glossary Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/glossary` | List glossary entries |
| POST | `/glossary` | Add entry |
| PUT | `/glossary/{id}` | Update entry |
| DELETE | `/glossary/{id}` | Delete entry |
| POST | `/glossary/import` | Import from CSV |
| GET | `/glossary/export` | Export to CSV |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/auth/login` | Authentication stub |
| GET | `/collections` | List glossary collections |

## Test Suite

```bash
pytest                           # All tests (112 pass, 1 skipped)
pytest -v                        # Verbose
pytest --cov=file_translator     # Coverage report
```

## Translation Pipeline

1. **Extract** — parse document (via Okapi Tikal for DOCX, lxml ZIP for XLSX)
2. **Batch** — group text units (configurable batch size)
3. **Translate** — send batches to LLM with structured prompt
4. **Apply** — replace translated text in document XML
5. **Post-process** — fix CJK fonts → Arial, table row heights `exact` → `atLeast`

## Glossary Language Validation

Each glossary column is validated against its expected language:

- `ru_word` → Russian (Cyrillic)
- `en_word` → English (Latin)
- `sb_word` → Serbian (Cyrillic or Latin)
- `ch_word` → Chinese (CJK)

Uses optional `lingua-language-detector` (ML-based, 170MB) or character-set heuristic fallback.

## License

MIT
