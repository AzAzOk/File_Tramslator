"""Validation domain models for input file checking."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ValidationSeverity(Enum):
    """Severity of a validation result."""
    
    ERROR = "error"      # Blocks processing
    WARNING = "warning"  # Allows processing with notice


@dataclass
class ValidationResult:
    """Result of a single validation check."""
    
    code: str                       # Machine-readable code (e.g. "FILE_SIZE_EXCEEDED")
    message: str                    # Human-readable description
    severity: ValidationSeverity = ValidationSeverity.ERROR
    details: dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_error(self) -> bool:
        """Check if this result represents an error."""
        return self.severity == ValidationSeverity.ERROR
    
    @property
    def is_warning(self) -> bool:
        """Check if this result represents a warning."""
        return self.severity == ValidationSeverity.WARNING


@dataclass
class ValidationReport:
    """Complete report of all validation checks for a file."""
    
    passed: bool = True
    results: list[ValidationResult] = field(default_factory=list)
    
    def add(self, result: ValidationResult | None) -> None:
        """Add a validation result. Sets passed=False if it's an error.
        
        If result is None (validation passed), it is silently ignored.
        """
        if result is None:
            return
        self.results.append(result)
        if result.is_error:
            self.passed = False
    
    @property
    def errors(self) -> list[ValidationResult]:
        """Get only error-level results."""
        return [r for r in self.results if r.is_error]
    
    @property
    def warnings(self) -> list[ValidationResult]:
        """Get only warning-level results."""
        return [r for r in self.results if r.is_warning]
    
    @property
    def error_messages(self) -> list[str]:
        """Get human-readable error messages."""
        return [r.message for r in self.errors]
    
    @property
    def warning_messages(self) -> list[str]:
        """Get human-readable warning messages."""
        return [r.message for r in self.warnings]


class ValidationError(Exception):
    """Exception raised when validation fails with errors.
    
    Contains the full validation report for structured error handling.
    """
    
    def __init__(self, report: ValidationReport):
        self.report = report
        messages = "; ".join(report.error_messages)
        super().__init__(f"Validation failed: {messages}")
