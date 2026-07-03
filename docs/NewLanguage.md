1. file_translator/domain/models.py:18
Добавить в LanguageCode:
DE = "de"
2. file_translator/domain/glossary.py:9-14
Добавить маппинг на колонку в MySQL:
LanguageCode.DE: "de_word",
3. file_translator/infrastructure/providers/openai_provider.py:39-44
Маппинг для промпта LLM:
LanguageCode.DE: "German",
4. file_translator/application/schemas.py:14-15
Обновить Field(description="...") — там перечислены коды.
5. file_translator/presentation/api/app.py
Обновить Form("...") дефолты и docstring во всех 5 эндпоинтах (сейчас везде en→ru).
6. okapi_service.py:127-128
Tikal принимает BCP-47 коды (de, fr, ja). Языки уже передаются в extract_to_xliff — ничего менять не надо, Tikal сам поддерживает.