# File Translator v2.0.0 — Структура проекта

## Конфигурационные файлы

| Файл | Описание |
|------|----------|
| `.env` | Переменные окружения: LDAP, JWT_SECRET, пароли, пути к базам данных и глоссариям |
| `.gitignore` | Исключает `__pycache__`, `.log`, локальные файлы из git-отслеживания |
| `docker-compose.yml` | Определяет четыре сервиса: API (FastAPI), фронтенд (nginx), MySQL, Redis; `JOB_TTL_SECONDS=3600` |
| `Dockerfile` | Собирает контейнер API с Python, Okapi Tikal CLI, LibreOffice Writer и SSL-библиотеками |
| `requirements.txt` | Список Python-зависимостей: fastapi, openai, PyMySQL, redis, bcrypt, cryptography |
| `nginx.conf` | Конфигурация nginx для фронтенда с проксированием `/api/` к API и обслуживания статики |
| `AGENTS.md` | Документация архитектуры проекта для AI-ассистентов (Okapi Tikal, DOCX pipeline, история изменений) |
| `README.md` | Общая документация по запуску и использованию сервиса |
| `PROJECT_STRUCTURE.md` | Данный файл — полное описание структуры файлов и модулей |

---

## Domain слой (`file_translator/domain/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация доменного модуля |
| `auth.py` | Модель пользователя с ролями (ADMIN, OPERATOR, VIEWER, API) и правами (TRANSLATE, MANAGE_USERS, SEND_FEEDBACK, VIEW_FEEDBACK и др.) |
| `dxf_models.py` | Модели данных для DXF-файлов (слои, примитивы, текстовые блоки) |
| `errors.py` | Кастомные исключения: TranslationError, DocumentParseError, TikalNotAvailableError, SaveDocumentError с reason |
| `glossary.py` | Модель глоссария с коллекциями, аудитом (created_by/at, updated_by/at) и элементами для перевода терминов |
| `interfaces.py` | ABC-интерфейсы: GlossaryRepository, JobManager, TextTranslator, AuthProvider |
| `job.py` | Модель задания с этапами (VALIDATION → EXTRACTION → GLOSSARY → TRANSLATION → SAVE) |
| `journal.py` | Модель журнала для аудита выполнения заданий |
| `models.py` | Общие доменные модели: языки перевода, стили, форматы файлов |
| `validation.py` | Схемы валидации входных данных (языки, стили, batch size) |

---

## Application слой (`file_translator/application/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация прикладного модуля |
| `auth_service.py` | Сервис аутентификации: JWT-токены (access + refresh rotation в MongoDB), LDAP-авторизация, управление сессиями |
| `glossary_service.py` | Сервис работы с глоссариями: загрузка, коллекции по AD-группам, аудит (создатель/редактор) |
| `job_manager.py` | Управление жизненным циклом заданий: создание, прогресс, отмена, завершение |
| `journal_service.py` | Сервис ведения журнала операций по этапам обработки документов |
| `schemas.py` | Pydantic-схемы: TranslationRequestSchema (с collection_id), FeedbackCreateSchema, FeedbackEntrySchema, BatchJobCreateResponseSchema |
| `service.py` | Основной сервис перевода: pipeline от извлечения до сохранения с проверками отмены; глоссарий подаётся как LLM-hints (не модификация текста); выходной файл сохраняет оригинальное имя |
| `user_queue.py` | Очередь пользовательских заданий — FIFO с одним воркером на пользователя (race condition fix: pop до break) |
| `validators.py` | Валидаторы файлов: _check_file_size() без аллокации, _safe_filename() (null bytes + Windows reserved names) |

---

## Infrastructure слой (`file_translator/infrastructure/`)

### Auth (`infrastructure/auth/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля аутентификации |
| `glossary_access_resolver.py` | Разрешение доступа к коллекциям глоссария по AD-группам из GLOSSARY_COLLECTION_MAP |
| `jwt_auth_provider.py` | Provider для JWT: генерация, валидация, декодирование access/refresh токенов; jti = uuid4() |
| `ldap_service.py` | Соединение с Active Directory: поиск пользователей, групп, авторизация |
| `stub_auth_provider.py` | Заглушка аутентификации для разработки без реального LDAP |
| `stub_user_repository.py` | Заглушка хранилища пользователей для тестирования |

### Builders (`infrastructure/builders/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля builders |
| `dxf_builder.py` | Сборка DXF-файлов из переведённых текстовых блоков с сохранением структуры |

### Config (`infrastructure/`)

| Файл | Описание |
|------|----------|
| `config.py` | Загрузка конфигурации из .env: URL базы данных, API ключи, настройки Redis, JWT_SECRET (RuntimeError при отсутствии) |

### Converters (`infrastructure/converters/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля конвертеров |
| `doc_to_docx_converter.py` | Конвертация .doc/.xls в .docx/.xlsx через LibreOffice CLI (LibreOfficeConverter) с UUID-профилем для избежания конфликтов |

### Parsers (`infrastructure/parsers/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля парсеров |
| `dxf_parser.py` | Парсинг DXF-файлов: извлечение текстовых объектов, слоёв и атрибутов |

### Providers (`infrastructure/providers/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля провайдеров |
| `mongo_provider.py` | Хранилище токенов в MongoDB с TTL-индексами на `refresh_tokens` и `sessions`; `expires_at` хранится как Date |
| `openai_provider.py` | Работа с OpenAI API: boundary markers, стили (TECHNICAL/LEGAL/MIXED), температура (0.0/0.1/0.3), JSON robustness fix, глоссарий в system prompt |

### Repositories (`infrastructure/repositories/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля репозиториев |
| `auth_repository.py` | Хранение пользователей в MongoDB (bcrypt-хеши паролей); TTL на refresh_tokens/sessions |
| `file_journal_repository.py` | Сохранение журнала операций в JSON-файлы с датой в имени |
| `glossary_collection_repository.py` | Репозиторий коллекций глоссариев (InMemory по умолчанию для "default" коллекции) |
| `in_memory_glossary_repository.py` | In-Memory хранилище глоссария для разработки без MySQL |
| `in_memory_job_repository.py` | In-Memory хранилище заданий для тестирования |
| `in_memory_journal_repository.py` | In-Memory хранили журнала для тестирования |
| `mysql_glossary_repository.py` | MySQL глоссарий: 33 таблицы (glossary + glossary_{collection}), _validate_table_name() против SQL-инъекций, аудит-колонки в add()/update() |
| `redis_job_repository.py` | Хранение заданий в Redis с TTL: 1 час (3600с) для всех статусов |

### Translators (`infrastructure/translators/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля переводчиков |
| `docx_translator.py` | Обработка DOCX через Okapi Tikal CLI: _extract_from_file (header/footer/textbox fix), _merge_runs_within_paragraph (по группам rPr), _post_process_docx (CJK → Arial + exact → atLeast), передача _document_path в merge |
| `dxf_translator.py` | Перевод DXF-файлов: парсинг, интеграция с OpenAI, сборка обратно |
| `xlsx_translator.py` | Перевод XLSX через ZIP + lxml: shared strings, inline strings, comments, rich text, .xls → .xlsx через LibreOffice |
| `okapi_service.py` | Обёртка Okapi Tikal CLI: _temp_work_dir() context manager, _sanitize_filename() (ASCII-only для Tikal), merge_from_xliff() с original_path, inline-code distribution, рекурсивный split (depth ≤ 3) |

---

## Presentation слой (`file_translator/presentation/`)

### API (`presentation/api/`)

| Файл | Описание |
|------|----------|
| `__init__.py` | Инициализация модуля API |
| `app.py` | FastAPI: `/jobs`, `/job/{id}`, `/glossary`, `/translate`, `/support/feedback` (POST — auth, GET — admin), `_cleanup_orphaned_temp_dirs()` (сканирует `translator_*`/`docx_okapi_*`/`tikal_*` старше 1ч, каждые 30 мин), download cleanup через BackgroundTasks |
| `auth_router.py` | Аутентикационные эндпоинты: POST `/api/auth/login`, `/refresh`, `/me`, `/logout` с prefix=/api/auth |
| `dependencies.py` | FastAPI-зависимости: require_permission (403/401), get_auth_service, get_translation_service |
| `middleware.py` | AuthMiddleware: пропускает PUBLIC_PATHS, валидирует JWT для всех остальных запросов, исключение для `/job/{id}/cancel` |

---

## Фронтенд (`static/`)

| Файл | Описание |
|------|----------|
| `index.html` | SPA (vanilla HTML/CSS/JS + Tailwind CDN): drag-and-drop загрузка, polling с exponential backoff (2→4→8→16→30с), стили (TECHNICAL/LEGAL/MIXED), коллекции глоссариев (dropdown только если есть), проверка sourceLang≠targetLang, Active Jobs (auto-remove через 5с при fail/cancel, сразу при download, 1ч safety net), тёмная/светлая тема, Omsk Yellow/Grey/Black бренд-цвета, footer (Документация, Политика конфиденциальности), "File Translator" → `<a href="/">` |
| `support.html` | Страница поддержки: форма отправки отзыва (любой auth), список сообщений (только admin), header "File Translator" → `/` |
| `privacy.html` | Политика конфиденциальности 152-ФЗ: типы данных, сроки хранения, права субъектов; header "File Translator" → `/` |

---

## Тесты (`file_translator/tests/`)

### Конфигурация

| Файл | Описание |
|------|----------|
| `conftest.py` | Pytest-фикстуры: mock OpenAI, Redis, MongoDB, Okapi Tikal для unit-тестов |

### E2E тесты (`tests/e2e/`)

| Файл | Описание |
|------|----------|
| `test_api_endpoints.py` | Интеграционные тесты API: health check (1 passed), translate endpoint skipped (нужен Tikal CLI) |

### Integration тесты (`tests/integration/`)

| Файл | Описание |
|------|----------|
| `test_docx_pipeline.py` | 26 тестов DOCX pipeline: can_process (7), _post_process_docx (8), extract→translate→save (7), full pipeline mock (4) |
| `test_translation_service.py` | Тесты service-слоя: полный pipeline с моками, обработка ошибок |

### Unit тесты (`tests/unit/`)

| Файл | Описание |
|------|----------|
| `test_docx_translator.py` | 7 тестов: SUPPORTED_FORMATS, can_process, strip (think/```) |
| `test_domain_models.py` | 7 тестов: LanguageCode, TextUnit, TranslationRequest |
| `test_okapi_service.py` | 28 тестов: XLIFF parse/save (10), _get_plain_text (7), _set_target_with_inline_codes (7), XliffUnit (4), _temp_work_dir (3) |
| `test_translation_provider.py` | 3 теста: default config, custom config, JSON fix |

---

## Служебные файлы (генерируемые)

| Файл | Описание |
|------|----------|
| `file_translator/__pycache__/*.pyc` | Скомпилированные Python-байткод файлы |
| `.pytest_cache/` | Кэш pytest для ускорения повторных запусков тестов |
| `logs/journal_YYYY-MM-DD.json` | Файлы журнала операций по дням |
| `*.deb` пакеты | OpenSSL и SSL библиотеки для Docker-контейнера |
| `uploads/`, `output/` | Временные директории для загруженных и обработанных файлов (очистка через 1 час) |
