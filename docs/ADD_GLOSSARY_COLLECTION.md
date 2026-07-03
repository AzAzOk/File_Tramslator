# Как добавить новую коллекцию глоссария

Допустим, нужно добавить глоссарий для отдела `NEW_DEPT`.

## 1. Создать таблицу в MySQL

```sql
CREATE TABLE glossary_new_dept LIKE glossary;
```

Таблица должна иметь ту же структуру: `id`, `ru_word`, `en_word`, `sb_word`, `ch_word`.

## 2. Прописать маппинг AD-группы

В `.env` добавить группу и её ID коллекции:

```
GLOSSARY_COLLECTION_MAP={"DTD":["dtd"],"OUP":["oup"],"NEW_DEPT":["new_dept"]}
```

Где `NEW_DEPT` — CN группы в AD, а `new_dept` — ID коллекции (соответствует имени таблицы без префикса `glossary_`).

## 3. Назначить пользователя в AD-группу

Пользователь должен быть членом группы `NEW_DEPT` в Active Directory.
