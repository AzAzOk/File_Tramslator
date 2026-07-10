"""TranslationValidator — checks placeholder integrity and line counts.

Returns a ``ValidationReport`` with PASS / WARN / FAIL status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ValidationStatus(Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class ValidationReport:
    status: ValidationStatus = ValidationStatus.PASS
    validator_name: str = ""
    messages: list[str] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)


class TranslationValidator:
    """Validates that translations haven't lost placeholders or structure.

    Checks:
    1. No raw ``[[...]]`` placeholders remain in the output
    2. No raw MTEXT format codes (``{\\f``) appear in the output
    3. Line count (``\\P`` / newlines) matches between input and output
    """

    _PLACEHOLDER_RE = re.compile(r"\[\[FMT_\w+]]")
    _FMT_CODE_RE = re.compile(r"\\[PASHLQWT_OL~]|\\S[^;]*;")

    def validate(
        self,
        original_texts: list[str],
        translated_texts: list[str],
    ) -> ValidationReport:
        """Compare original vs translated for structural integrity."""
        messages: list[str] = []
        metrics = {
            "checked": len(translated_texts),
            "placeholder_issues": 0,
            "fmt_code_issues": 0,
            "line_count_mismatches": 0,
        }

        for orig, trans in zip(original_texts, translated_texts):
            # Check for leftover placeholders
            if self._PLACEHOLDER_RE.search(trans):
                metrics["placeholder_issues"] += 1

            # Check for unencoded format codes in output
            if self._FMT_CODE_RE.search(trans):
                metrics["fmt_code_issues"] += 1

            # Compare newline count
            orig_lines = orig.count("\\P") + orig.count("\n")
            trans_lines = trans.count("\\P") + trans.count("\n")
            if orig_lines != trans_lines:
                metrics["line_count_mismatches"] += 1

        metric_names = {
            "placeholder_issues": "leftover placeholders",
            "fmt_code_issues": "raw format codes in output",
            "line_count_mismatches": "line count mismatches",
        }

        for key, label in metric_names.items():
            count = metrics[key]
            if count > 0:
                messages.append(f"{label}: {count}")

        if metrics["placeholder_issues"] > 0:
            status = ValidationStatus.FAIL
        elif metrics["fmt_code_issues"] > 0 or metrics["line_count_mismatches"] > 0:
            status = ValidationStatus.WARN
        else:
            status = ValidationStatus.PASS

        return ValidationReport(
            status=status,
            validator_name="translation",
            messages=messages,
            metrics=metrics,
        )
