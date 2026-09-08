## Context

The glossary backend (`glossary_service.py`) currently raises `ValueError` from `_validate_language` and `_check_duplicate` on any problem, and both are surfaced to the API layer as a single HTTP 400 with one `detail` string. The frontend (`static/index.html` glossary modal) shows that `detail` in a red error area with no way to proceed. This conflates two distinct failure types — language mismatch and duplication — which must be split so the user can override only the former.

Motivation: see proposal.md — Why.

## Goals / Non-Goals

**Goals:**
- Let a user save a glossary entry that failed the language check, on their own responsibility.
- Keep the duplicate check always enforced (never bypassable).
- Clearly separate "language mismatch" from "duplicate" so the UI can act accordingly.
- Minimal, low-risk change to existing validation flow.

**Non-Goals:**
- Improving/calibrating the language detector to reduce false positives (explicitly out of scope — override is the chosen escape hatch).
- Auditing/logging that a save used an override.
- Introducing new persisted schema fields.
- Changing `_check_duplicate` behavior in any way.

## Decisions

### Decision 1: Backend override via `force_language` flag, not try/except catching the ValueError
The cleanest signal is a boolean `force_language` threaded through the service entry points. When `True`, `_validate_language` is skipped entirely; `_check_duplicate` always runs.

- **Why flag over catching the exception:** catching would require distinguishing the exact failed column and risks partially-applying validation. A flag on the request makes intent explicit and keeps both checks separate.
- **Alternative considered:** a global "disable validation" setting — rejected (too broad, affects all users).

Signature change:
```python
async def add_entry(self, entry_data, collection_id="default", created_by="", force_language=False)
async def update_entry(self, entry_data, collection_id="default", updated_by="", force_language=False)
```
Inside: `if not force_language: self._validate_language(entry_data)` then `await self._check_duplicate(...)` unconditionally.

### Decision 2: Structured error codes — split LANGUAGE_MISMATCH from DUPLICATE
Instead of a bare `ValueError` with only a message, the service/API distinguishes the two so the client can decide whether to show the override.

Design: the service raises a typed exception (e.g. `LanguageMismatchError` in `glossary_service.py` or a small module) that carries `code="LANGUAGE_MISMATCH"` and the human message. `_check_duplicate` continues raising a `ValueError` (or a distinct `DuplicateError`) mapped to `code="DUPLICATE"`. The FastAPI handlers catch these and raise `HTTPException(400, detail={"code": code, "detail": message})`.

- **Why a `code` field:** the frontend needs a machine-readable discriminator. Message-only strings are fragile.
- **Alternative considered:** distinct HTTP status codes (409 for duplicate, 422 for language). Rejected here because both are validation failures and 400 with a `code` keeps compatibility with any existing 400 handling; preserves the current contract for clients that only read `detail`.

### Decision 3: UI — contextually appearing "Save anyway" action
The glossary modal shows the language-mismatch message and, in that same error state, reveals a secondary "Save anyway" button next to the existing controls. It appears only while a `LANGUAGE_MISMATCH` error is displayed and disappears on any other error (duplicate/missing fields) or on modal close.

- **Why contextual button over a permanent checkbox:** a permanent "ignore language check" checkbox would clutter the form for users who never hit a false positive, and risks accidental toggles. A button that appears exactly when the warning is shown is the standard "override with consent" pattern.
- **Why no state persistence:** the override is a per-save decision; no need to remember it.

Flow:
1. User clicks "Save" → `POST/PUT` without `force_language`.
2. 400 with `code="LANGUAGE_MISMATCH"` → show message + reveal "Save anyway".
3. User clicks "Save anyway" → same request with `force_language: true`.
4. 400 with `code="DUPLICATE"` → show message, no override button.
5. Success → close modal.

### Decision 4: Schemas gain `force_language`
`GlossaryCreateSchema` and `GlossaryUpdateSchema` add `force_language: bool = False`. It is a client-supplied override flag, not stored.

## Risks / Trade-offs

- [User may mis-use the override to save genuinely wrong-language values] → By design; this is the explicit user choice. The warning text stays visible so the decision is informed.
- [Adding a `code` field to error `detail` could break existing frontend error rendering that expects a plain string] → The API layers that return `HTTPException(400, detail=str(exc))` today already produce a string `detail`; many callers read `detail`. Decision: keep returning a string `detail`, and add the `code` alongside (e.g. `detail` remains the human message, `code` added as a separate top-level body field). Verify frontend `authFetch`/error paths ignore unknown fields.
- [Duplicate safeguard is global, so override can't rescue a legitimately duplicated entry] → Intended; duplicates remain an error to protect data quality.

## Migration Plan

Backward compatible: existing calls to `add_entry`/`update_entry` without `force_language` behave exactly as before. The API response body is extended (new `code` field) without removing `detail`, so old clients that read `detail` keep working. Deployment is a normal backend+static redeploy; no DB migration.

## Open Questions

None — the remaining unknowns (button styling, exact error copy) are cosmetic and safe to decide during implementation without changing specs or approach.
