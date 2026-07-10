# Universal Document Translation Platform — Architecture & Implementation Plan

> Изначально проект задумывался как переводчик DXF/DWG. Архитектура ниже сохраняет CAD как первый (и приоритетный) формат, но перестаёт завязывать pipeline на конкретный формат: DXF/DWG становится одной из реализаций общего контракта `IParser` / `IUpdater`, а не центральной концепцией системы. Это даёт возможность добавить DOCX, XLSX, PPTX, SVG и т.д., написав только новый парсер и апдейтер — без изменения ядра.

---

## 0. Единая модель документа (Document Model)

Ключевое изменение относительно предыдущей версии: вместо того чтобы каждый парсер формировал собственные структуры, вводится единый контракт, с которым работают все последующие этапы pipeline (`RuleEngine`, `TokenProtector`, `LLMAdapter`, `Validator`, `IntegrityChecker`). Они ничего не знают о том, что документ — это DXF, DOCX или XLSX.

```text
Document
├── schema_version        # версия Document Model (см. ниже) — например "1.0"
├── metadata              # формат, версия исходного файла, кодировка, единицы измерения и т.п.
├── entities[]            # список TranslatableEntity
├── resources             # шрифты, стили, схемы, блоки — всё, что не текст, но нужно для восстановления
└── diagnostics           # предупреждения парсера, потери информации, fallback-события
```

```text
TranslatableEntity
├── id                    # стабильный внутренний id
├── handle                # нативный идентификатор формата (DXF handle, Word bookmark, ячейка XLSX...)
├── type                  # TEXT | MTEXT | ATTRIB | TABLE_CELL | PARAGRAPH | FORMULA | ...
├── text                  # исходный текст
├── metadata              # layer, style, cell address, run formatting и т.п. — формато-специфично
├── protected_tokens[]     # результат работы TokenProtector
└── translation_status     # PENDING | SKIPPED | TRANSLATED | FAILED
```

Это позволяет строить единый Entity Index (id/handle → entity) независимо от формата и переиспользовать весь pipeline от Classifier до IntegrityChecker без ветвлений `if format == "dxf"`.

### Версионирование модели (Versioned Document Model)

`Document` несёт поле `schema_version` (например, `"1.0"`). Это защищает от ситуации, когда через год потребуется добавить новое обязательное поле (например, поддержку вложенных документов или revision-истории), а старые парсеры/апдейтеры при этом сломаются.

Правила эволюции модели:
- Минорная версия (`1.0` → `1.1`) — только добавление опциональных полей, старые плагины продолжают работать без изменений.
- Мажорная версия (`1.x` → `2.0`) — breaking change; `FormatRegistry` может держать параллельно парсеры под разные мажорные версии на время миграции.
- `document_translator.py` (оркестратор) проверяет `schema_version` на входе pipeline и явно отклоняет несовместимые версии, а не падает где-то в середине обработки.

---

## 1. Плагины вместо `if format == ...`

Формато-специфичный код сведён к двум интерфейсам на формат:

```text
IParser
 ├── DxfParser     (через EzdxfBackend)
 ├── DwgParser     (через ODA SDK / ConversionBackend)
 ├── DocxParser
 ├── XlsxParser
 └── PptxParser

IUpdater
 ├── DxfUpdater
 ├── DwgUpdater
 ├── DocxUpdater
 ├── XlsxUpdater
 └── PptxUpdater
```

```python
class IParser(ABC):
    def parse(self, path: Path) -> Document: ...
    def capabilities(self) -> set[Capability]: ...

class IUpdater(ABC):
    def apply(self, document: Document, results: list[TranslationResult]) -> None: ...
    def save(self, document: Document, path: Path) -> None: ...
```

Добавление нового формата = регистрация `IParser` + `IUpdater` в `FormatRegistry`. Остальной pipeline (Classifier → RuleEngine → TokenProtector → LLM → Validators → IntegrityChecker) не меняется.

`CADBackend` (см. §3) остаётся как есть — это конкретная реализация `IParser`/`IUpdater` для DXF/DWG, просто теперь она соответствует общему интерфейсу, а не является единственной точкой абстракции в системе.

---

## 1a. FormatRegistry — полноценный компонент

Раньше `FormatRegistry` только упоминался как идея. Делаем его отдельным, тестируемым компонентом, а не статическим словарём "по случаю":

```python
class FormatRegistry:
    def register(self, extension: str, parser: type[IParser], updater: type[IUpdater]) -> None: ...
    def get(self, extension: str) -> tuple[IParser, IUpdater]: ...
    def supported_extensions(self) -> set[str]: ...
```

Использование:

```python
FormatRegistry.register(".dxf", parser=DxfParser, updater=DxfUpdater)
FormatRegistry.register(".xlsx", parser=XlsxParser, updater=XlsxUpdater)

parser, updater = registry.get(".xlsx")
```

`application/service.py` (и любой другой вызывающий код) обращается только к `FormatRegistry.get(extension)` и вообще не знает о существовании `DxfParser`, `ezdxf` или конкретных классов апдейтеров. Регистрация плагинов происходит один раз при старте приложения (например, в `bootstrap.py` или через entry points), что также упрощает добавление форматов сторонними модулями без правки ядра.

Регистрация нового плагина обязана пройти `ParserContractTest`/`UpdaterContractTest` (см. §"Тесты контрактов") — без прохождения этих тестов `FormatRegistry.register()` не должен пропускать плагин в продакшн-конфигурацию.

---

## 2. Capability System

Не все форматы поддерживают одинаковые возможности, и жёсткое кодирование этого в виде условий по всему коду плохо масштабируется. Каждый `IParser` объявляет декларативно, что он умеет:

```text
supports:
- tables
- blocks
- attributes
- xdata
- formulas
```

| Возможность            | DOCX | XLSX | PPTX | DXF | DWG |
| ----------------------- | ---- | ---- | ---- | --- | --- |
| Таблицы                 | ✅   | ✅   | ⚠️   | ⚠️  | ⚠️  |
| Форматирование текста    | ✅   | ✅   | ✅   | ✅  | ✅  |
| Формулы                 | ❌   | ✅   | ❌   | ❌  | ❌  |
| Блоки                   | ❌   | ❌   | ❌   | ✅  | ✅  |
| XData                   | ❌   | ❌   | ❌   | ❌  | ✅  |

`RuleEngine`, `BatchBuilder` и `Validator`ы читают `capabilities()` парсера и включают/выключают соответствующие проверки и стратегии группировки, вместо того чтобы содержать разбросанные по коду условия вида `if isinstance(backend, EzdxfBackend)`.

---

## Architecture Overview (pipeline)

```
                    Input File
                        │
                        ▼
                 File Validator
                        │
                        ▼
                 Format Detector
              (выбирает IParser по расширению/сигнатуре)
                        │
              ┌─────────┼─────────┬──────────┐
              ▼         ▼         ▼          ▼
          DxfParser DocxParser XlsxParser  ...
              │         │         │          │
              └─────────┴────┬────┴──────────┘
                              ▼
                          Document
                    (entities[] + Entity Index)
                              │
                              ▼
                       Entity Scanner
                              │
                              ▼
                    Content Classifier
                    (категоризирует entity)
                              │
                              ▼
                        Rule Engine
             (DN100, Ø20, ISO9001 → skip; учитывает capabilities)
                              │
                              ▼
                       Token Protector
                  ({\f...;\P...} → [[FMT1]]...)
                              │
                              ▼
                       Text Extractor
                              │
                              ▼
                    Translation Memory
                       │        │
                  HIT  ▼        ▼ MISS
                  Reuse     Glossary Engine
                    │        (Valve→Клапан)
                    │          │
                    └────┬─────┘
                         ▼
                   Batch Builder
                         │
                         ▼
                   LLM Adapter
              (OpenAI / Ollama / Gemini)
                         │
                         ▼
              Translation Validator
              (плейсхолдеры, счётчики строк)
                         │
                         ▼
               Structural Validator
           (структура документа не изменилась)
                         │
                         ▼
                Semantic Validator
          (коды типа DN100, ISO9001 не переведены)
                         │
                         ▼
                   Token Restorer
              ([[FMT1]] → {\f...;...})
                         │
                         ▼
                    Entity Updater
                   (IUpdater.apply, через Entity Index)
                         │
                         ▼
                  Integrity Checker
            (handle, layer, color, count...)
                         │
                         ▼
                   Metrics Collector
                         │
                         ▼
                    Audit Logger
            (handle → text → translated → model)
                         │
                         ▼
                     Save File
                  (IUpdater.save)
```

---

## CADBackend — реализация IParser/IUpdater для DXF/DWG

`CADBackend` остаётся ключевой абстракцией внутри CAD-плагина, но теперь она — частный случай общего контракта, а не единственная точка расширения всей системы.

```python
class CADBackend(ABC):
    def open(self, path: Path) -> CADDocument: ...
    def iter_entities(self, doc: CADDocument) -> Iterator[CADEntity]: ...
    def get_text(self, entity: CADEntity) -> str: ...
    def set_text(self, entity: CADEntity, text: str) -> None: ...
    def get_handle(self, entity: CADEntity) -> str: ...
    def get_layer(self, entity: CADEntity) -> str: ...
    def get_entity_count(self, doc: CADDocument) -> int: ...
    def save(self, doc: CADDocument, path: Path) -> None: ...
    def close(self, doc: CADDocument) -> None: ...
```

Реализации:
- **EzdxfBackend** — для DXF через ezdxf
- **TeighaBackend** — для DWG через ODA SDK / RealDWG (**основной путь**, когда доступен)
- **ConversionBackend** — для DWG через DWG→DXF→DWG (**явный Fallback Backend**, а не основная реализация)

`DxfParser`/`DxfUpdater` — тонкие обёртки над `CADBackend`, преобразующие `CADEntity` ↔ `TranslatableEntity` и объявляющие `capabilities()` (blocks, xdata, attributes и т.д.). Entity Index строится при `open()`, даёт O(1) доступ по handle.

**Стратегия выбора backend'а для DWG** зафиксирована явно, чтобы при появлении доступа к ODA SDK/RealDWG не пришлось менять pipeline — только порядок приоритета в `DwgParser`:

```
DWG
 │
 ▼
TeighaBackend (ODA SDK / RealDWG) ── доступен? ──► использовать как основной
 │
 └── недоступен ──► ConversionBackend (DWG→DXF→DWG) ── с warning в Document.diagnostics
```

`DwgParser` пробует `TeighaBackend` первым; при недоступности SDK на окружении явно переключается на `ConversionBackend` и пишет предупреждение в `diagnostics`, а не молча деградирует.

---

## Компоненты (в порядке pipeline)

### 1. File Validator
Проверяет: файл существует, не повреждён, не нулевой, имеет поддерживаемое расширение.

### 2. Format Detector
Определяет формат файла (по расширению + сигнатуре) и выбирает соответствующий `IParser` из `FormatRegistry`.

### 3. IParser (DxfParser / DocxParser / ... ) + Entity Index
- `parser.parse()` → строит `Document` (entities + resources + metadata)
- Внутри CAD-плагина: `EzdxfBackend.open()` → читает DXF через ezdxf
- Строит `EntityIndex: dict[str, TranslatableEntity]` (id/handle → entity)
- Сканирует modelspace, paperspace, блоки (для DXF/DWG); секции/строки/ячейки (для DOCX/XLSX)

### 4. Entity Scanner
Проходит по всем entity документа. Группирует по типу.

### 5. Content Classifier
Категоризирует каждую entity. Для CAD:

| Тип | Условие | Категория |
|-----|---------|-----------|
| TEXT | всегда | TRANSLATE |
| MTEXT | всегда | TRANSLATE |
| ATTRIB value | всегда | TRANSLATE |
| ATTRIB tag | всегда | METADATA (skip) |
| ATTDEF default | всегда | TRANSLATE |
| ATTDEF tag | всегда | METADATA (skip) |
| DIMENSION | есть override text | TRANSLATE |
| DIMENSION | нет override | MEASUREMENT (skip) |
| FIELD | всегда | FORMULA (skip) |
| MLEADER | всегда | TRANSLATE |
| TABLE cell | всегда | TRANSLATE |
| TOLERANCE | всегда | TRANSLATE |
| MLINESTYLE desc | всегда | TRANSLATE |

Для других форматов (DOCX/XLSX/PPTX) классификатор реализуется отдельно, но использует тот же интерфейс `ContentClassifier.classify(entity) -> Category`.

### 6. Rule Engine
Правила вида "этот текст не переводить". Выполняется **после** Classifier, учитывает `capabilities()` активного парсера (например, правила для формул применяются только если формат их поддерживает).

Примеры встроенных правил:
- `\d+[×xх]\d+` — размеры (100x200)
- `DN\d+` — номинальные диаметры
- `Ø\d+` — диаметры
- `ISO\d+`, `GOST\s*\d+` — стандарты
- `^\d+$` — чистые числа
- `^[A-Z]{2,}\d*$` — коды/артикулы (VALVE001)

Rule Engine может:
1. **Пропустить entity** (не отправлять в LLM)
2. **Заменить текст заранее** (DN100 → DN100 без перевода)
3. **Добавить контекст в промпт** (сохранить как есть)

Правила конфигурируемы (JSON/yaml) и не зависят от формата — работают с `TranslatableEntity`.

### 7. Token Protector
Заменяет форматные коды (MTEXT-коды, Word-разметку runs, XLSX rich-text) на плейсхолдеры **до** LLM.

**Encode** (перед LLM):
```
{\fArial|b0|i0;Valve\PSize}  →  [[FMT_a1b2]]Valve[[NL_a1b2]]Size[[FMT_a1b2_end]]
```

**Decode** (после LLM):
```
[[FMT_a1b2]]Клапан[[NL_a1b2]]Размер[[FMT_a1b2_end]]  →  {\fArial|b0|i0;Клапан\PРазмер}
```

Плейсхолдеры уникальны per-entity (id/handle + counter) — коллизий нет даже в batch.

**Защищаемые паттерны (DXF/DWG)**:
`{\f...}`, `\P`, `\S...^...`, `\A`, `\H`, `\Q`, `\W`, `\T`, `\_`, `\O`, `\L`, `\~`

### 8. Text Extractor
Собирает `TextUnit[]` из entity с категорией `TRANSLATE`. Пропускает METADATA, MEASUREMENT, FORMULA.

### 9. Translation Memory
Кэш: `normalized_text → translated_text`. Перед отправкой в LLM проверяет, не переводили ли уже такой текст. Не зависит от формата документа.

Экономит токены: если "Valve" встречается 3000 раз, LLM видит его один раз.

### 10. Glossary Engine
Принудительные терминологические замены. Интегрируется с существующим `GlossaryService`.

```
Valve → Клапан  (always, regardless of LLM output)
```

Выполняется **до** BatchBuilder: если термин найден, подставляется сразу, без LLM.

### 11. Batch Builder
Группирует TextUnit в батчи. Стратегии зависят от `capabilities()` активного формата:
- По **layers** (CAD, связанный контекст)
- По **блокам** (CAD, атрибуты одного блока вместе)
- По **географической близости** (CAD, есть координаты)
- По **секциям/абзацам** (DOCX)
- По **листам/диапазонам** (XLSX)

### 12. LLM Adapter
Абстракция над LLM. Даёт возможность сменить модель без изменения pipeline.

```python
class LLMAdapter(ABC):
    async def translate(self, batch: TranslationBatch) -> list[TranslationResult]: ...
```

Реализации:
- `OpenAIAdapter` (существующий `OpenAITranslationProvider`)
- `OllamaAdapter`
- `GeminiAdapter`

### 13–15. Валидация (разделена на три независимых уровня)

Вместо одного универсального валидатора — три независимых проверки, каждая со своей зоной ответственности. Это упрощает локализацию ошибок: если что-то не так, сразу понятно, на каком уровне.

Все три валидатора возвращают **один и тот же формальный контракт** — `ValidationReport`, а не произвольные структуры/строки:

```text
ValidationReport
├── status               # PASS | WARN | FAIL
├── validator_name        # "translation" | "structural" | "semantic"
├── messages[]            # человекочитаемые описания найденных проблем
└── metrics               # опционально: числа, полезные для MetricsCollector (например, mismatched_count)
```

Единый контракт даёт две вещи: во-первых, оркестратор может агрегировать статусы всех валидаторов по общему правилу (см. ниже), не зная деталей каждого; во-вторых, `AuditLogger` и `MetricsCollector` подписываются на один и тот же тип объекта независимо от того, какой валидатор его произвёл.

Агрегация: итоговый статус документа = наихудший из статусов трёх валидаторов, по приоритету `FAIL > WARN > PASS`.

**13. Translation Validator** — не потерялись ли маркеры и плейсхолдеры:
- Количество плейсхолдеров совпадает
- Количество строк в MTEXT/абзаце совпадает
- Количество строк/колонок таблицы совпадает
- Нет `[[...]]` в output (значит не всё раскрыто)
- Нет raw `{\f...` в output (значит TokenProtector пропустил)

**14. Structural Validator** — не изменилась ли структура документа:
- Количество entity/абзацев/ячеек совпадает до и после перевода
- Порядок и вложенность элементов сохранены
- Ни один элемент не был случайно удалён или продублирован при обработке батчей

**15. Semantic Validator** — нет ли подозрительных изменений в защищённых кодах:
- Коды вида `DN100`, `ISO9001`, `M12`, `Ø20` не были переведены/изменены LLM
- Числовые значения (размеры, версии, артикулы) идентичны исходным
- Флагует случаи, когда Rule Engine пропустил код, а LLM всё равно его "перевёл"

Каждый валидатор возвращает `ValidationReport` со своим статусом (`PASS` / `WARN` / `FAIL`), которые агрегируются перед записью в файл.

### 16. Token Restorer
Обратная операция TokenProtector: `[[FMT_a1b2]]` → `{\fArial|b0|i0;}`.

### 17. Entity Updater (IUpdater.apply)
Применяет translated_text к entity через `IUpdater` конкретного формата (для CAD — через `CADBackend` + Entity Index, O(1) по handle).

### 18. Integrity Checker
Пост-сохранение. Сравнивает исходный и переведённый файл. Для CAD:

- Количество entity совпадает
- Количество block совпадает
- Количество layer совпадает
- Handle не изменились
- Layer assignment не изменился
- Color не изменился
- Linetype не изменился
- XData сохранена
- Extension dictionaries сохранены

Для других форматов набор проверок определяется через `capabilities()` (например, для XLSX — формулы и стили ячеек, для DOCX — стили и нумерация).

### 19. Metrics Collector

Новый компонент, собирающий статистику по каждому запуску pipeline — полезно и для оптимизации, и для отчётности/дипломной работы:

- сколько сущностей найдено (по категориям);
- сколько переведено / пропущено / провалено;
- сколько найдено через Translation Memory (cache hit rate);
- сколько терминов подставлено из глоссария;
- сколько токенов отправлено в LLM (per batch и суммарно);
- среднее время обработки одного объекта и всего документа;
- количество WARN/FAIL по каждому из трёх валидаторов.

Метрики сохраняются вместе с audit-логом и доступны через `MetricsReport` (JSON), который можно агрегировать по множеству прогонов.

### 20. Audit Logger
Запись per-entity:

```json
{
  "handle": "4A52",
  "original_text": "Valve",
  "translated_text": "Клапан",
  "model": "gpt-4o-mini",
  "timestamp": "2026-07-03T12:00:00Z",
  "validator_status": {
    "translation": "PASS",
    "structural": "PASS",
    "semantic": "PASS"
  }
}
```

Хранится в journal (существующий `JournalService`).

---

## Контракты между этапами

Чтобы менять реализации (`ezdxf` → ODA SDK, `Ollama` → `OpenAI`, `MySQL` → `PostgreSQL`) без изменения остального pipeline, контракты между этапами фиксируются формально — не только "вход/выход", но и какие поля обязательны, какие опциональны, и какие инварианты должны соблюдаться:

| Этап | Вход | Выход | Обязательные поля выхода | Опциональные поля | Инвариант |
|------|------|-------|---------------------------|--------------------|-----------|
| IParser | `Path` | `Document` | `schema_version`, `entities[].id`, `entities[].type`, `entities[].text` | `metadata`, `resources`, `diagnostics` | Каждый `entity.id` уникален в пределах документа |
| ContentClassifier | `Document` | `dict[entity_id, Category]` | покрытие всех `entities[].id` | — | Каждому entity сопоставлена ровно одна категория |
| RuleEngine | `Document`, categories, `capabilities()` | отфильтрованный список entity + pre-filled переводы | `entity_id`, `action` (SKIP/PREFILL/PASS) | `context_hint` | SKIP/PREFILL entity никогда не попадают в TextExtractor |
| TokenProtector | `TranslatableEntity` | `TranslatableEntity` с `protected_tokens` | `protected_tokens[].placeholder`, `.original` | — | Плейсхолдеры уникальны в рамках batch |
| TextExtractor | `Document` (TRANSLATE-only) | `list[TextUnit]` | `entity_id`, `text` | `context` | Ни один TextUnit не ссылается на несуществующий entity_id |
| TranslationMemory / GlossaryEngine | `list[TextUnit]` | `hits[]`, `misses[]` | `hits[].translated_text` | — | `hits ∪ misses == вход`, пересечения нет |
| BatchBuilder | `misses[]`, `capabilities()` | `list[TranslationBatch]` | `batch.items[]` | `batch.context` | Каждый TextUnit из входа попадает ровно в один batch |
| LLMAdapter | `TranslationBatch` | `list[TranslationResult]` | `entity_id`, `translated_text`, `status` | `raw_model_output` | Размер результата == размеру батча |
| Translation/Structural/Semantic Validator | `Document`, `list[TranslationResult]` | `ValidationReport` | `status`, `validator_name`, `messages[]` | `metrics` | `status` строго один из PASS/WARN/FAIL |
| TokenRestorer | `TranslationResult` | восстановленный текст | — | — | Отсутствие `[[...]]` в результате при успешном restore |
| IUpdater.apply | `Document`, `list[TranslationResult]` | обновлённый `Document` | — | — | Количество entity не меняется |
| IntegrityChecker | исходный и обновлённый `Document`/файл | `IntegrityReport` | `status`, `diffs[]` | — | Пустой `diffs[]` при `status == PASS` |
| MetricsCollector | события всех этапов | `MetricsReport` | счётчики по категориям | тайминги | Отчёт формируется даже при частичном отказе pipeline |
| AuditLogger | все отчёты + результаты | journal entry | `handle`, `original_text`, `translated_text`, `validator_status` | `model` | Одна journal entry на одну translated entity |

Пока эти контракты стабильны, реализация каждого этапа может меняться независимо от остальных.

### Contract-тесты — обязательное условие регистрации плагина

Тесты контрактов — не рекомендация, а жёсткое требование: плагин, не прошедший `ParserContractTest`/`UpdaterContractTest`, не допускается до регистрации в `FormatRegistry` в продакшн-конфигурации.

```python
class ParserContractTest:
    def test_parse_returns_valid_document(self): ...
    def test_entity_ids_are_unique(self): ...
    def test_capabilities_declared(self): ...

class UpdaterContractTest:
    def test_apply_preserves_entity_count(self): ...
    def test_save_produces_valid_file(self): ...
    def test_round_trip(self):
        # parse → apply(no-op translation) → save → parse снова →
        # документ должен быть структурно идентичен исходному
        ...
```

Round-trip тест (`parse → save → parse` без перевода) — минимальный барьер входа для любого нового `IParser`/`IUpdater`: если он не проходит, значит парсер теряет информацию уже на этапе чтения/записи, до всякого перевода.

### Event Bus (опционально, не для MVP)

Не обязателен для первой версии, но закладывается как возможное направление эволюции: вместо того чтобы `document_translator.py` явно вызывал `MetricsCollector`, `AuditLogger` и прочие "наблюдателей" по цепочке, каждый этап pipeline может публиковать событие (`ParserCompleted`, `ValidationCompleted`, `EntityUpdated` и т.п.) в общую шину:

```
ParserCompleted → [MetricsCollector, AuditLogger]
ValidationCompleted → [MetricsCollector, AuditLogger, AlertingService]
```

Это упрощает добавление новых подписчиков (например, будущий realtime-дашборд прогресса перевода) без изменения кода оркестратора. Осознанно вынесено за рамки MVP: пока количество подписчиков мало (Metrics + Audit), прямые вызовы из `document_translator.py` проще отлаживать, чем шину событий.

---

## Файловая структура

```
infrastructure/
├── document/
│   ├── __init__.py
│   ├── document_model.py           # Document, TranslatableEntity, Capability (+ schema_version)
│   └── format_registry.py          # FormatRegistry: register()/get()/supported_extensions()
│
├── parsers/
│   ├── __init__.py
│   ├── i_parser.py                 # IParser ABC
│   ├── dxf_parser.py                # DxfParser (обёртка над CADBackend)
│   ├── dwg_parser.py                # DwgParser: TeighaBackend → ConversionBackend fallback
│   ├── docx_parser.py
│   └── xlsx_parser.py
│
├── updaters/
│   ├── __init__.py
│   ├── i_updater.py                 # IUpdater ABC
│   ├── dxf_updater.py
│   ├── dwg_updater.py
│   ├── docx_updater.py
│   └── xlsx_updater.py
│
├── backends/
│   ├── __init__.py
│   ├── cad_backend.py               # CADBackend ABC (используется DxfParser/DxfUpdater)
│   ├── ezdxf_backend.py             # DXF через ezdxf
│   ├── teigha_backend.py            # DWG через ODA SDK / RealDWG (основной путь)
│   └── conversion_backend.py        # DWG→DXF→DWG (явный Fallback Backend)
│
├── classifiers/
│   ├── __init__.py
│   ├── cad_content_classifier.py    # entity → category (CAD)
│   └── cad_token_protector.py       # MTEXT коды → плейсхолдеры
│
├── engines/
│   ├── __init__.py
│   ├── cad_rule_engine.py           # "не переводить DN100"
│   ├── translation_memory.py        # dedup cache (формато-независимый)
│   └── glossary_engine.py           # принудительная терминология
│
├── adapters/
│   ├── __init__.py
│   ├── llm_adapter.py               # LLMAdapter ABC
│   └── llm_openai_adapter.py        # OpenAI/Ollama impl
│
├── validators/
│   ├── __init__.py
│   ├── validation_report.py         # ValidationReport (status/validator_name/messages/metrics)
│   ├── translation_validator.py     # плейсхолдеры/счётчики строк
│   ├── structural_validator.py      # структура документа не изменилась
│   ├── semantic_validator.py        # коды (DN100, ISO9001) не переведены
│   └── cad_integrity_checker.py     # структурный diff (пост-сохранение)
│
├── translators/
│   └── document_translator.py       # оркестратор pipeline (формато-независимый)
│
├── services/
│   ├── __init__.py
│   ├── entity_scanner.py            # iterate + group entities
│   ├── text_extractor.py            # TextUnit builder
│   ├── batch_builder.py             # умная группировка
│   ├── entity_updater.py            # запись через IUpdater
│   ├── metrics_collector.py         # сбор статистики по прогону
│   └── audit_logger.py              # per-entity аудит
│
tests/
├── contracts/
│   ├── parser_contract_test.py      # обязателен для любого нового IParser
│   └── updater_contract_test.py     # обязателен для любого нового IUpdater (+ round-trip)
```

---

## Изменения в существующих файлах

| Файл | Изменение |
|------|-----------|
| `domain/dxf_models.py` | DxfEntity → CADEntity → маппится в TranslatableEntity. Убрать original_entity. Добавить handle. |
| `domain/interfaces.py` | Добавить `IParser`, `IUpdater`, `CADBackend` (CADBackend — конкретная реализация под CAD) |
| `domain/document_model.py` | Новый файл: `Document`, `TranslatableEntity`, `Capability` (Enum) |
| `application/service.py` | `_find_translator` → выбор через `FormatRegistry` по расширению, не через if/elif |
| `requirements.txt` | +`ezdxf>=1.3.0` |
| `AGENTS.md` | Добавить архитектуру Document Model + плагинную систему |

---

## Порядок реализации

### Шаг 0 — Document Model + контракты
1. `domain/document_model.py` — `Document` (с `schema_version`), `TranslatableEntity`, `Capability`
2. `domain/interfaces.py` — `IParser`, `IUpdater` ABC
3. `infrastructure/document/format_registry.py` — `FormatRegistry` (register/get/supported_extensions)
4. Формально зафиксировать таблицу контрактов (вход/выход/обязательные поля/инварианты, см. раздел выше) в `AGENTS.md`
5. `tests/contracts/parser_contract_test.py`, `updater_contract_test.py` — базовый набор, включая round-trip

### Шаг 1 — CADBackend как реализация IParser/IUpdater
6. `infrastructure/backends/cad_backend.py` — CADEntity, CADDocument DTOs
7. `infrastructure/backends/ezdxf_backend.py` — реализация через ezdxf
8. `infrastructure/parsers/dxf_parser.py` — DxfParser (CADEntity → TranslatableEntity, объявляет capabilities)
9. `infrastructure/updaters/dxf_updater.py` — DxfUpdater
10. `domain/dxf_models.py` — переименовать, убрать лишнее
11. Прогнать `DxfParser`/`DxfUpdater` через `ParserContractTest`/`UpdaterContractTest` из Шага 0 — регистрация в `FormatRegistry` только после прохождения

### Шаг 2 — Scanner + Classifier + TokenProtector ✅
12. ✅ `infrastructure/services/entity_scanner.py`
13. ✅ `infrastructure/classifiers/cad_content_classifier.py`
14. ✅ `infrastructure/classifiers/cad_token_protector.py`

### Шаг 3 — RuleEngine + TextExtractor ✅
15. ✅ `infrastructure/engines/cad_rule_engine.py` (учитывает capabilities)
16. ✅ `infrastructure/services/text_extractor.py`

### Шаг 4 — TM + Glossary + Batch ✅
17. ✅ `infrastructure/engines/translation_memory.py` — кэш с нормализацией, bulk_store, stats
18. ✅ `infrastructure/engines/glossary_engine.py` — word-boundary замена через load_entries()
19. ✅ `infrastructure/services/batch_builder.py` — FLAT / BY_LAYER стратегии, настраиваемый batch_size

### Шаг 5 — LLM Adapter ✅
20. ✅ `infrastructure/adapters/llm_adapter.py` — LLMAdapter ABC (translate)
21. ✅ `infrastructure/adapters/llm_openai_adapter.py` — обёртка OpenAITranslationProvider для батчей

### Шаг 6 — Три валидатора + Updater + Integrity
22. `infrastructure/validators/validation_report.py` — формальная модель `ValidationReport` (status/validator_name/messages/metrics)
23. `infrastructure/validators/translation_validator.py`
24. `infrastructure/validators/structural_validator.py`
25. `infrastructure/validators/semantic_validator.py`
26. `infrastructure/services/entity_updater.py`
27. `infrastructure/validators/cad_integrity_checker.py`

### Шаг 7 — Metrics + Auditor + Translator
28. `infrastructure/services/metrics_collector.py`
29. `infrastructure/services/audit_logger.py`
30. `infrastructure/translators/document_translator.py` — оркестратор pipeline (формато-независимый), агрегирует `ValidationReport` по правилу FAIL > WARN > PASS

### Шаг 8 — DWG (MVP)
31. `infrastructure/backends/teigha_backend.py` — ODA SDK / RealDWG (основной путь для DWG)
32. `infrastructure/backends/conversion_backend.py` — явный Fallback Backend (DWG→DXF→DWG)
33. `infrastructure/parsers/dwg_parser.py` — приоритет TeighaBackend → ConversionBackend с записью в `diagnostics`
34. Регистрация обоих путей в `FormatRegistry` для `.dwg`

### Шаг 9 — Tests
35. Unit-тесты для каждого компонента, включая три валидатора отдельно
36. Integration test для полного pipeline (через ezdxf temp DXF)
37. Contract-тесты уже обязательны с Шага 1 (см. `tests/contracts/`) — здесь добавляется прогон для DWG-парсера и для round-trip через ConversionBackend

### Шаг 10 — Второй формат (доказательство расширяемости)
38. Реализовать `DocxParser`/`DocxUpdater` как первый некад-формат, чтобы подтвердить, что pipeline действительно формато-независим
39. Прогнать тот же pipeline (Classifier → ... → IntegrityChecker) без изменений в ядре
40. Обязательно прогнать `DocxParser`/`DocxUpdater` через `ParserContractTest`/`UpdaterContractTest` — это первая реальная проверка, что контракты действительно формато-независимы, а не подогнаны под CAD

---

## Принципы

| Принцип | Как соблюдается |
|---------|-----------------|
| **SRP** | Каждый компонент делает одну вещь (Classifier, Protector, три отдельных Validator) |
| **OCP** | Новый формат = новый `IParser`/`IUpdater`, pipeline не меняется |
| **LSP** | Любой `IParser`/`IUpdater`/`CADBackend` взаимозаменяем через ABC |
| **ISP** | Интерфейсы имеют минимально необходимые методы; `capabilities()` вместо разрастания методов "на всякий случай" |
| **DIP** | Pipeline зависит от `IParser`/`IUpdater`/`LLMAdapter` ABC, не от ezdxf/OpenAI напрямую |
| **DRY** | TranslationMemory исключает повторные переводы; Document Model исключает дублирование логики между форматами |

---

## Риски

| Риск | Impact | Mitigation |
|------|--------|------------|
| ezdxf не читает DIMENSION override text | Medium | Ручной парсинг group code 1 |
| MTEXT вложенность > 5 уровней | Low | TokenProtector: limit=5 |
| MLEADER формат разный в ACAD версиях | Medium | Тесты на AC1027+ |
| ODA SDK/RealDWG недоступен для DWG в окружении | High | Явный fallback на ConversionBackend (DWG→DXF→DWG) с записью warning в `Document.diagnostics`; при появлении SDK смена приоритета не требует изменений в pipeline, т.к. выбор backend'а инкапсулирован в `DwgParser` |
| `schema_version` Document Model меняется мажорно, ломая старые плагины | Medium | Явная проверка версии в `document_translator.py` на входе pipeline; поддержка нескольких мажорных версий в `FormatRegistry` на время миграции |
| Плагин зарегистрирован в `FormatRegistry` без прохождения contract-тестов | Medium | `FormatRegistry.register()` в продакшн-конфигурации не принимает плагин без пройденных `ParserContractTest`/`UpdaterContractTest` |
| Производительность на 100K+ entity | Medium | Batch processing по 1000 |
| Document Model окажется недостаточно общей для DOCX/XLSX и потребует breaking changes | Medium | Проектировать модель, начиная со второго формата (Шаг 10), а не только под CAD |
| Capability System приведёт к разрастанию флагов вместо упрощения кода | Low | Ограничить набор capability до тех, что реально влияют на поведение engine/validator'ов |
| Разделение на три валидатора усложнит агрегацию статусов и отчётность | Low | Единый `ValidationReport` с приоритетом FAIL > WARN > PASS при агрегации |
