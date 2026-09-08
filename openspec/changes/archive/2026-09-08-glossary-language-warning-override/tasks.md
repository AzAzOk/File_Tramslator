## 1. Backend: typed error + force_language override

- [x] 1.1 Introduce a typed `LanguageMismatchError` (carrying `code="LANGUAGE_MISMATCH"` and a human message) in the glossary service layer, and raise it from `_validate_language` on mismatch; verify it is raised instead of the plain `ValueError` when a column language mismatches
- [x] 1.2 Add a `force_language: bool = False` parameter to `GlossaryService.add_entry` and `GlossaryService.update_entry`; when `True`, skip `_validate_language` but always call `_check_duplicate`; verify: with `force_language=True` a language mismatch saves, but a duplicate still raises
- [x] 1.3 Keep `_check_duplicate` raising a signal distinguishable from language mismatch (distinct `DuplicateError` or `ValueError` mapped to `code="DUPLICATE"`); verify both error classes/codes are distinct

## 2. API: structured error codes + wire force_language

- [x] 2.1 Add `force_language: bool = False` to `GlossaryCreateSchema` and `GlossaryUpdateSchema` in `schemas.py`; verify schema accepts and defaults the field
- [x] 2.2 Update `POST /glossary` handler to catch `LanguageMismatchError` → 400 with error body carrying `code="LANGUAGE_MISMATCH"` and human `detail`; catch duplicate → 400 with `code="DUPLICATE"`; pass `force_language` from the schema into `add_entry`; verify: each error path returns the expected `code` and a plain string `detail`
- [x] 2.3 Update `PUT /glossary/{entry_id}` handler the same way (language vs duplicate codes, `force_language` → `update_entry`); verify both endpoints behave consistently

## 3. Frontend: "Save anyway" override UI

- [x] 3.1 In the glossary modal, add a hidden secondary "Save anyway" button that appears only when the displayed error carries `code === "LANGUAGE_MISMATCH"`; it must stay hidden for duplicate/missing-field errors and on modal open/close; verify via manual UI or DOM inspection
- [x] 3.2 On "Save anyway" click, resubmit the same create/update request with `force_language: true`; on success close modal and reload glossary; on duplicate error keep the modal open with the message and no override button; verify end-to-end for both add and edit

## 4. Tests

- [x] 4.1 Add unit tests for `add_entry`/`update_entry`: default rejects language mismatch; `force_language=True` allows it; duplicate still blocks even with `force_language=True`; verify the unit test file passes
- [x] 4.2 Add API tests asserting `POST /glossary` and `PUT /glossary/{id}` return `code="LANGUAGE_MISMATCH"` on mismatch, `code="DUPLICATE"` on duplicate, and save with `force_language=true`; verify the API test file passes
- [x] 4.3 Run the full `pytest` suite and confirm no new regressions beyond any pre-existing failures; verify the summary output
