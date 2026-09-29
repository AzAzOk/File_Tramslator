# -*- coding: utf-8 -*-
"""Unit tests for numeric/normative-reference fidelity helpers."""

from file_translator.diagnostics.numeric_fidelity import (
    protect_numerics,
    restore_numerics,
    verify_numeric,
    repair_numeric,
)


def test_protect_restore_roundtrip():
    src = "Приказ МВД РК от 24.10.2014 г. №732, таблица 3.1, 120 км"
    protected, tokens = protect_numerics(src)
    assert "732" not in protected
    assert "\\u27e6" not in "x"  # placeholder sanity
    assert all(t.raw for t in tokens)

    target = ("Order No. \u27e6NUM0\u27e7 dated \u27e6NUM1\u27e7, "
              "Table \u27e6NUM2\u27e7, \u27e6NUM3\u27e7 km")
    restored = restore_numerics(target, tokens)
    assert "24.10.2014" in restored
    assert "№732" in restored
    assert "3.1" in restored
    assert "120" in restored


def test_no_tokens_is_identity():
    protected, tokens = protect_numerics("Просто текст без цифр")
    assert protected == "Просто текст без цифр"
    assert tokens == []
    assert restore_numerics("Просто текст без цифр", tokens) == "Просто текст без цифр"


def test_kinds_classified():
    _, tokens = protect_numerics("№732 от 24.10.2014, табл. 3.1, 120 км")
    kinds = sorted(t.kind for t in tokens)
    assert "ref" in kinds
    assert "date" in kinds
    assert "sub" in kinds
    assert "num" in kinds


def test_verify_detects_ref_drift_without_flagging_legit_date_format():
    src = "Приказ МВД РК от 24.10.2014 г. №732"
    target = ("The distance, as defined by Order No. 107 of the Ministry "
              "dated October 24, 2014")
    mismatches = verify_numeric(src, target)
    refs = [m for m in mismatches if m.kind == "ref"]
    assert len(refs) == 1
    assert refs[0].source == "732"
    assert refs[0].target == "107"
    # Date reformat to month names must NOT be reported.
    assert not any(m.kind == "date" for m in mismatches)


def test_repair_substitutes_source_number():
    src = "Приказ МВД РК от 24.10.2014 г. №732"
    _, tokens = protect_numerics(src)
    target = ("The distance, as defined by Order No. 107 of the Ministry "
              "dated October 24, 2014")
    mismatches = verify_numeric(src, target)
    repaired, unresolved = repair_numeric(target, mismatches, tokens)
    assert unresolved == []
    assert "107" not in repaired
    assert "732" in repaired


def test_verify_no_mismatch_on_same_numbers():
    src = "Приказ МВД РК от 24.10.2014 г. №732 и таблица 3.1"
    target = "Order No. 732 of 24.10.2014 and Table 3.1"
    assert verify_numeric(src, target) == []


def test_unit_values_not_treated_as_designators():
    # Dotted values with a unit suffix (1.4 kPa) are data, not section numbers.
    assert verify_numeric("Давление 1.4 кПа", "Pressure 1.4 kPa") == []
    assert verify_numeric("До 2.1 МПа и 1.28 kW", "Up to 2.1 MPa and 1.28 kW") == []
    # Dotted numbers followed by heading text stay designators.
    _, tokens = protect_numerics("3.1 Перечень опасных участков")
    assert any(t.kind == "sub" and t.normalized == "3.1" for t in tokens)