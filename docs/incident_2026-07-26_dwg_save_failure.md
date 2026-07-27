# Инцидент: Совм ОТ 17-07.dwg — ошибка сохранения

**Дата**: 2026-07-26  
**Job ID**: (первый запуск, journal_2026-07-26.json)  
**Файл**: Совм ОТ 17-07.dwg (67.6 MB)  
**Время обработки**: 02:33:57 — 07:10:38 UTC (~4.5 часа)

---

## Хронология (из journal)

### Приём и извлечение
```
02:33:57 — Translation request received: ru -> en
02:33:57 — Validating input file
02:33:57 — Extracting text using DxfTranslator
02:35:34 — Extracted 129838 text units
```
- DWG → DXF конвертация через ODA HTTP: OK (22 сек)
- ezdxf открыл DXF: OK
- Парсинг: 129,838 entities

### Фильтрация и батчи
```
02:35:34 — FILTER_BY_SOURCE: server-side filter skipped 39070/129838 units (detected language != ru)
02:35:34 — Created 1816 translation batches (size=50)
```
- К переводу: 90,768 юнитов (остальные — не русские, пропущены фильтром)

### Перевод (батчи 1–1816)
- Успешно переведено: **54,212 / 90,768** юнитов (59.7%)
- Не переведено (ошибки JSON): **36,556** юнитов
- Причина ошибок: `Expecting ',' delimiter` — LLM возвращает невалидный JSON, split depth limit (3) превышен
- Последний батч: `07:09:25 — Batch 1816/1816 translated (18 units)`

### Итог перевода
```
07:09:25 — Incomplete translation: 54212/90768 units (LLM may have skipped some)
```

### Ошибка сохранения
```
07:10:38 — ERROR — Failed to save document:
  Не удалось сохранить документ: /tmp/translator_6lyzkr0d/Совм ОТ 17-07_translated.dwg
  — [Errno 2] No such file or directory:
    '/tmp/translator_6lyzkr0d/Совм ОТ 17-07_translated.dxf'
```

---

## Код сохранения (до исправления)

```python
# EzdxfBackend.save()
def save(self, doc, path):
    path = Path(path)
    if path.suffix.lower() == ".dwg":
        tmp_dxf = path.with_suffix(".dxf")                         # Совм ОТ 17-07_translated.dxf
        doc.saveas(str(tmp_dxf))                                   # ← НЕ создал файл
        from ... import dxf_to_dwg
        if not dxf_to_dwg(tmp_dxf, path):                         # ← False (файла нет)
            fallback_path = path.with_suffix(".dxf")               # = tmp_dxf (тот же путь)
            tmp_dxf.rename(fallback_path)                          # ← [Errno 2] No such file or directory
            return fallback_path
        tmp_dxf.unlink(missing_ok=True)
        return path
```

```python
# dxf_to_dwg() — до исправления
def dxf_to_dwg(dxf_path, output_dwg, timeout=120):
    return _run_converter(
        input_dir=dxf_path.parent.resolve(),     # /tmp/translator_6lyzkr0d/
        output_dir=output_dwg.parent.resolve(),   # /tmp/translator_6lyzkr0d/  ← ОДНА И ТА ЖЕ ДИРЕКТОРИЯ
        output_format="DWG",
        ...
    )
```

---

## Что пошло не так (факты)

1. **`doc.saveas(tmp_dxf)` не создал файл** `Совм ОТ 17-07_translated.dxf` — ezdxf не смог записать DXF (~479 MB) на диск.

2. **`dxf_to_dwg()` вернул `False`** — файл-источник для конвертации отсутствовал.

3. **`tmp_dxf.rename(fallback_path)` — self-rename** — обе переменные указывали на один и тот же путь `Совм ОТ 17-07_translated.dxf`. Rename самого на себя → `[Errno 2] No such file or directory` (файл не существует).

4. **`dxf_to_dwg()` перед фиксом** использовал одну директорию для input и output — ODA сканировала все файлы в `/tmp/translator_6lyzkr0d/`.

---

## Размеры файлов в процессе

| Файл | Размер |
|------|--------|
| Исходный DWG | 67.6 MB |
| Конвертированный DXF (ODA DWG→DXF) | ~479 MB |
| Промежуточный DXF (doc.saveas) | не создан |
| Результирующий DWG | не создан |

---

## Исправления (после инцидента)

1. **`EzdxfBackend.save()`** — добавлена проверка `doc.saveas()` + existence check. Self-rename заменён на `return tmp_dxf`.

2. **`dxf_to_dwg()`** — раздельные temp-директории для input/output (раньше одна и та же). Авто-таймаут `max(300, 120 + size_mb/4)` вместо фиксированных 120 секунд.

3. **`dwg_to_dxf()`** — аналогичный авто-таймаут.

4. **Логирование** — добавлен вывод размера промежуточного DXF перед конвертацией.

---

## Второй запуск (после деплоя)

```
08:33:57 — Job created: ff96e021-8238-4741-a049-ef07fafb951b for Совм ОТ 17-07.dwg
08:33:57 — Starting translation: /tmp/translator_xyuwfoan/Совм ОТ 17-07.dwg
08:34:19 — ODA HTTP conversion successful (DWG → DXF)
08:35:33 — Parsed 129838 entities
08:35:34 — Filtered: 90768 to translate, 1816 batches
08:36:16 — Batch 1/1816 translated
08:37:52 — Batch 2/1816 translated
```
Прерван деплоем API в 08:38:20. Do尚未 дошёл до save.
