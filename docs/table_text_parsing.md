# Парсинг текста в таблицах DXF/DWG

## Общая архитектура

Парсинг текста внутри таблиц реализован в `DXFParser` (`parser.py`) и охватывает **все типы таблиц**, которые могут встретиться в DXF и DWG форматах. Основной диспетчер — `_entity_handlers()` (`parser.py:124`), который направляет сущности по типам:

| Тип сущности | Метод обработки |
|---|---|
| `ACAD_TABLE` (современные таблицы AutoCAD) | `_extract_acad_table_data()` |
| `TABLE` (классические таблицы DXF) | `_extract_table_entities()` |
| `INSERT` (блоки) + `ATTRIB` | `_extract_insert_data()` → `_extract_attrib_data()` |
| `TEXT`, `MTEXT` (внутри блоков/ячеек) | `_create_embedded_text_record()` |

---

## 1. ACAD_TABLE — современные таблицы AutoCAD

**Метод:** `_extract_acad_table_data()` (`parser.py:210`)

### Как работает:

1. **Извлечение proxy-содержимого** — используется `entity.proxy_graphic_content()`, который возвращает список из TEXT и MTEXT сущностей, визуализирующих содержимое таблицы.

2. **Сбор текстов** — проход по proxy-элементам, отбор только `TEXT` и `MTEXT`.

3. **Сортировка по координатам** — текстовые элементы сортируются по Y (убывание → строки сверху вниз) и X (возрастание → столбцы слева направо) (`parser.py:278-279`).

4. **Определение сетки таблицы:**
   - **Если известно количество колонок:** `num_cols` — текст делится на строки по формуле `row = idx // num_cols`, `col = idx % num_cols` (`parser.py:291-292`).
   - **Если колонки неизвестны:** применяется алгоритм группировки по Y-координатам с допуском `y_tolerance = 5.0`, затем X-позиции кластеризуются в колонки (`parser.py:322-477`).

5. **Многострочные ячейки** — фрагменты с одинаковой (X,Y)-позицией объединяются через `\P` (`parser.py:434`).

6. **Сохранение метаданных:** `handle` таблицы, `handle` proxy-элемента, `row`, `col`, координаты, стиль, слой.

### Покрытие:
- AutoCAD 2007+ таблицы (AcDbTable)
- Вложенные TEXT/MTEXT в ячейках
- Объединённые ячейки (обрабатываются как один proxy-элемент)

---

## 2. TABLE — классические таблицы DXF

**Метод:** `_extract_table_entities()` (`parser.py:662`)

### Два подхода:

#### A. Стандартный API ezdxf (`parser.py:675-722`)
- Использует `table_entity.rows` и `table_entity.columns`
- Обходит каждую ячейку через `table_entity.get_cell(row, col)`
- Извлекает текст через `cell.value` или `cell.text`
- Проверяет вложенные TEXT/MTEXT через `_extract_embedded_text_from_cell()`

#### B. Поиск вложенных сущностей (`parser.py:724-727`)
- Если стандартный API недоступен, ищет TEXT/MTEXT внутри block_record таблицы через `_find_embedded_entities_in_table()`.

### Покрытие:
- DXF R2000+ таблицы (AcDbTable)
- Ячейки с произвольным текстом
- Legacy-форматы таблиц

---

## 3. Вложенные TEXT/MTEXT в ячейках

**Методы:** `_extract_embedded_text_from_cell()` (`parser.py:735`) и `_create_embedded_text_record()` (`parser.py:770`)

### Сценарии обнаружения:

| Механизм | Где используется |
|---|---|
| `cell.virtual_entities()` | Ячейки с встроенными примитивами |
| `cell.block_record` → `block` | Ячейки, ссылающиеся на блоки |
| `table_entity.virtual_entities()` | Прямые TEXT/MTEXT под таблицей |
| `table_entity.block_record` → `block` | Таблицы как блок-ссылки |

### Создаваемая запись включает:
- `type`: `TABLE_TEXT` или `TABLE_MTEXT`
- `row`, `col` (или `-1` если неизвестны)
- Абсолютные и относительные координаты
- Все параметры форматирования (стиль, высота, поворот, выравнивание)
- Ссылки на `entity` и `table_entity`

---

## 4. INSERT / ATTRIB — атрибуты блоков

**Метод:** `_extract_insert_data()` → `_extract_attrib_data()` (`parser.py:200-208`)

Хотя это не «таблицы» в классическом смысле, атрибуты блоков в DXF/WBLOCK — аналог табличных данных. Каждый ATTRIB извлекается как:
- `type: INSERT_ATTRIBUTE`
- `tag`, `text`, `plain_text`
- `block_name`, координаты блока и атрибута

---

## 5. ACAD_TABLE_CELL — API-доступ (новый метод)

**Метод:** `_extract_acad_table_entities()` (`parser.py:586`)

Дополнительный метод, использующий прямой API ezdxf для перебора ячеек:
- Итерация по `table_entity` (ряды → ячейки)
- Извлечение `cell.value`
- Координаты через `cell.anchor`
- Параметры стиля через `cell.style`, `cell.text_height`

Используется, когда `proxy_graphic_content()` недоступен (определённые версии ezdxf или старые файлы).

---

## 6. Обработка в DOCX (для полноты картины)

В `translate_offic.py` (`DocxTranslator`) таблицы DOCX обрабатываются через рекурсивный сбор абзацев:

**Метод:** `_collect_paragraphs_recursive()` (`translate_offic.py:1209`)
- Обходит `container.rows` → `cell.paragraphs`
- Поддерживает вложенные таблицы (`cell.tables`)
- Предотвращает зацикливание через `collected_ids`

---

## Почему это покрывает все типы таблиц

| Тип | Формат | Как обнаруживается |
|---|---|---|
| **AcDbTable** (AutoCAD 2007+) | DXF/DWG | `ACAD_TABLE` → proxy-графика |
| **AcDbTable** (API ezdxf) | DXF/DWG | `ACAD_TABLE` → итерация ячеек |
| **TABLE** (DXF R2000) | DXF | `TABLE` → rows/columns API |
| **TABLE** (legacy) | DXF | `TABLE` → block_record + поиск |
| **Блоки с атрибутами** | DXF/DWG | `INSERT` → `ATTRIB` |
| **Вложенный текст** | Любой | `virtual_entities()` / `block_record` |
| **DOCX таблицы** | DOCX | `_collect_paragraphs_recursive()` |

### Ключевые принципы покрытия:

1. **Два уровня абстракции** — парсер пробует сначала высокоуровневый API (`rows/cols`), затем падает на низкоуровневый (proxy-графика / поиск сущностей).

2. **Координатная сетка** — если API не раскрывает структуру, используется позиционирование (X/Y) для восстановления row/col.

3. **Дедупликация с сохранением контекста** — `_deduplicate_texts()` (`parser.py:1415`) объединяет идентичные тексты из разных ячеек в один элемент с массивом `table_cells`, сохраняя все `handle`, `row`, `col`.

4. **Обработка форматирования** — DXF-коды (`\P`, `\H`, `\F`, `\C` и др.) извлекаются и сохраняются для восстановления после перевода.
