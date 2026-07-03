"""Application validators - Input file validation chain."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from file_translator.domain.interfaces import DocumentValidator
from file_translator.domain.validation import ValidationReport, ValidationResult, ValidationSeverity

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE_BYTES = 128 * 1024 * 1024  # 128 MB
_STALE_JOB_TTL_SECONDS = 300  # 5 minutes


class FileSizeValidator(DocumentValidator):
    """Check that file size does not exceed the maximum (128 MB)."""
    
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationResult:
        try:
            size = file_path.stat().st_size
            if size > _MAX_FILE_SIZE_BYTES:
                return ValidationResult(
                    code="FILE_SIZE_EXCEEDED",
                    message=f"File size ({size / 1024 / 1024:.1f} MB) exceeds maximum allowed (128 MB)",
                    severity=ValidationSeverity.ERROR,
                    details={"size_bytes": size, "max_bytes": _MAX_FILE_SIZE_BYTES},
                )
            return None
        except OSError as e:
            return ValidationResult(
                code="FILE_SIZE_ERROR",
                message=f"Cannot determine file size: {e}",
                severity=ValidationSeverity.ERROR,
            )


class FileAccessValidator(DocumentValidator):
    """Check that the file exists and is accessible for reading."""
    
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationResult:
        if not file_path.exists():
            return ValidationResult(
                code="FILE_NOT_FOUND",
                message=f"File not found: {file_path}",
                severity=ValidationSeverity.ERROR,
            )
        if not file_path.is_file():
            return ValidationResult(
                code="FILE_NOT_A_FILE",
                message=f"Path is not a file: {file_path}",
                severity=ValidationSeverity.ERROR,
            )
        try:
            with open(file_path, "rb") as f:
                pass  # Check read access
            return None
        except PermissionError:
            return ValidationResult(
                code="FILE_LOCKED",
                message="File is locked or inaccessible (permission denied)",
                severity=ValidationSeverity.ERROR,
            )
        except OSError as e:
            return ValidationResult(
                code="FILE_ACCESS_ERROR",
                message=f"Cannot access file: {e}",
                severity=ValidationSeverity.ERROR,
            )


class FileStructureValidator(DocumentValidator):
    """Check that the file has valid structure for its format.
    
    For DOCX: verifies it's a valid ZIP archive containing Word-specific files.
    """
    
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationResult:
        suffix = file_path.suffix.lower()
        
        if suffix == ".docx":
            return self._validate_docx(file_path)
        
        # Other formats: no structural validation available — warn but don't block
        return ValidationResult(
            code="STRUCTURE_NOT_CHECKED",
            message=f"Structural validation not implemented for {suffix} files",
            severity=ValidationSeverity.WARNING,
        )
    
    def _validate_docx(self, file_path: Path) -> ValidationResult:
        """Validate DOCX structure (ZIP with required XML files)."""
        try:
            import zipfile
            with zipfile.ZipFile(file_path, "r") as zf:
                names = zf.namelist()
                
                # Check for required DOCX content files
                required = ["word/document.xml"]
                for req in required:
                    if req not in names:
                        return ValidationResult(
                            code="STRUCTURE_INVALID",
                            message=f"Invalid DOCX structure: missing {req}",
                            severity=ValidationSeverity.ERROR,
                            details={"missing": req, "found_files": len(names)},
                        )
                
            return None
        except zipfile.BadZipFile:
            return ValidationResult(
                code="STRUCTURE_BAD_ZIP",
                message="File is not a valid ZIP archive (required for DOCX)",
                severity=ValidationSeverity.ERROR,
            )
        except Exception as e:
            return ValidationResult(
                code="STRUCTURE_ERROR",
                message=f"File structure validation failed: {e}",
                severity=ValidationSeverity.ERROR,
            )


class LanguageMismatchValidator(DocumentValidator):
    """Check that source and target languages are not the same."""
    
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationResult:
        source = (context or {}).get("source_language", "")
        target = (context or {}).get("target_language", "")
        
        if source and target and source.lower() == target.lower():
            return ValidationResult(
                code="LANGUAGE_MISMATCH",
                message=f"Source and target languages are the same: {source}",
                severity=ValidationSeverity.ERROR,
                details={"source": source, "target": target},
            )
        return None


class ConcurrentJobValidator(DocumentValidator):
    """Check that the same file is not already being processed.
    
    Requires a JobManager to check active jobs.
    """
    
    def __init__(self, job_manager: Any | None = None):
        self._job_manager = job_manager
    
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationResult:
        if not self._job_manager:
            return ValidationResult(
                code="CONCURRENCY_NOT_CHECKED",
                message="Concurrent job check not available",
                severity=ValidationSeverity.WARNING,
            )
        
        try:
            filename = file_path.name
            active_jobs = await self._job_manager.get_active_jobs()
            
            for job in active_jobs:
                if hasattr(job, 'filename') and job.filename == filename:
                    # Check if job is stale (running too long without updating)
                    if self._is_job_stale(job):
                        logger.warning(f"Found stale job {job.job_id} for {filename}, ignoring")
                        continue
                    return ValidationResult(
                        code="CONCURRENT_PROCESSING",
                        message=f"File '{filename}' is already being processed (job: {job.job_id})",
                        severity=ValidationSeverity.ERROR,
                        details={"job_id": job.job_id, "filename": filename},
                    )
            
            return None
        except Exception as e:
            return ValidationResult(
                code="CONCURRENCY_CHECK_ERROR",
                message=f"Concurrent check failed: {e}",
                severity=ValidationSeverity.ERROR,
            )
    
    def _is_job_stale(self, job: Any) -> bool:
        """Check if a job has been running too long without updates.
        
        A job is considered stale if it was started more than
        _STALE_JOB_TTL_SECONDS ago and hasn't progressed. This prevents
        ghost jobs from blocking new translations after a crash.
        """
        started = getattr(job, 'started_at', None) or getattr(job, 'created_at', None)
        if not started:
            return True
        try:
            started_dt = datetime.fromisoformat(started)
            elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
            return elapsed > _STALE_JOB_TTL_SECONDS
        except (ValueError, TypeError):
            return True


class ValidationChain:
    """Chain of responsibility for file validation.
    
    Runs all registered validators and collects results.
    If any validator returns an ERROR-level result, the chain stops.
    """
    
    def __init__(self):
        self._validators: list[DocumentValidator] = []
    
    def add_validator(self, validator: DocumentValidator) -> None:
        """Add a validator to the chain."""
        self._validators.append(validator)
    
    @property
    def validators(self) -> list[DocumentValidator]:
        """Get the list of registered validators."""
        return list(self._validators)
    
    async def validate_all(self, file_path: Path, context: dict[str, Any] | None = None) -> ValidationReport:
        """Run all validators and return a combined report."""
        report = ValidationReport()
        
        for validator in self._validators:
            try:
                result = await validator.validate(file_path, context)
                if result is None:
                    continue  # Validation passed, no issues
                report.add(result)
                if result.is_error:
                    logger.warning(f"Validation failed: {result.message}")
                    return report  # Stop on first error
            except Exception as e:
                report.add(ValidationResult(
                    code="VALIDATOR_ERROR",
                    message=f"Validator {validator.__class__.__name__} failed: {e}",
                    severity=ValidationSeverity.ERROR,
                ))
                return report
        
        return report
