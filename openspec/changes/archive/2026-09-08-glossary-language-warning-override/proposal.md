## Why

The glossary language validator (`_validate_language`) blocks adding/updating a glossary entry whenever a value doesn't match the expected language for its column (e.g. a Russian word containing Latin letters in `ru_word`). This heuristic sometimes produces false positives on legitimate input (abbreviations, transliterations, mixed terms like "Wi-Fi", short words). Today the user has no way to proceed — they must either edit the word or abandon the save. As a user, I want to be able to deliberately ignore this warning and still save the new/edited entry.

## What Changes

- Add a user-facing override: when a language-mismatch warning blocks a glossary save, show a "Save anyway" action in the modal so the user can proceed on their own responsibility.
- The override applies **only** to the language-mismatch check. The duplicate check (`_check_duplicate`) remains **always enforced** — a duplicate still blocks the save and cannot be bypassed.
- The backend distinguishes the two error types so the frontend can show the override action only for language mismatch, never for duplicates.
- No change to the validator's detection logic and no audit tracking of overrides.

## Capabilities

### New Capabilities
- `glossary`: Glossary entry management. Covers add/edit/delete/list of glossary entries, including the language-mismatch warning and the user's ability to override it when saving.

### Modified Capabilities
<!-- None — no existing capability specs exist yet (openspec/specs is empty). This change introduces the first glossary capability spec. -->

## Impact

- `file_translator/application/glossary_service.py` — `add_entry`/`update_entry` signatures accept a `force_language` flag; `_validate_language` is skipped when set, `_check_duplicate` always runs.
- `file_translator/application/schemas.py` — `GlossaryCreateSchema`/`GlossaryUpdateSchema` gain `force_language`.
- `file_translator/presentation/api/app.py` — `POST /glossary` and `PUT /glossary/{id}` return structured error codes (`LANGUAGE_MISMATCH` vs `DUPLICATE`) and propagate `force_language`.
- `static/index.html` — glossary modal shows a "Save anyway" control only on language mismatch.
- Tests: unit tests for service override logic; API tests for structured error codes.
