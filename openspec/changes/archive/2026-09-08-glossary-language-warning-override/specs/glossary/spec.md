## Purpose

Manages glossary entries (translations of terms across Russian, English, Serbian, and Chinese), enforces a language-mismatch warning when a value doesn't match its column, and lets the user deliberately override that warning when saving.

## ADDED Requirements

### Requirement: Language-mismatch warning
When adding or updating a glossary entry, the system SHALL check each populated column value against the language expected for that column. If a value does not match the expected language, the system SHALL reject the save with a language-mismatch error and MUST NOT save the entry.

#### Scenario: Valid entry saves
- **WHEN** a user adds an entry whose Russian, English, Serbian, and Chinese values match their respective columns
- **THEN** the entry is saved successfully

#### Scenario: Mismatched value blocks the save
- **WHEN** a user adds an entry where the Russian column contains a value detected as non-Russian
- **THEN** the system rejects the save with a language-mismatch error identifying the offending column and value, and does not save the entry

### Requirement: User can override the language warning on save
When a save is blocked only by a language-mismatch warning, the user SHALL be able to explicitly override it and save the entry anyway. The override applies to the entire save regardless of how many columns flagged a mismatch, and puts responsibility for the value on the user.

#### Scenario: Override succeeds
- **WHEN** a user receives a language-mismatch error and confirms they want to save anyway
- **THEN** the entry is saved with the originally entered values

#### Scenario: Override is only offered on language mismatch
- **WHEN** a save is blocked by a duplicate or by missing required fields (not by a language mismatch)
- **THEN** the system does not offer the override and the save stays blocked

### Requirement: Duplicate check always enforced
The duplicate check MUST always run on save, including when the user overrides a language warning. If any value duplicates an existing entry in the same collection, the save MUST be rejected regardless of any language override.

#### Scenario: Duplicate blocks an overridden save
- **WHEN** a user overrides a language warning, but one of the entered values duplicates an existing entry in the same collection
- **THEN** the save is rejected with a duplicate error and the entry is not saved
