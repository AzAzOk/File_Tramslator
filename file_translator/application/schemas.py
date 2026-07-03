"""Application schemas - Pydantic models for API validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TranslationRequestSchema(BaseModel):
    """Schema for translation request."""
    
    source_language: str = Field(..., description="Source language code (ru, en, sr, zh, auto)")
    target_language: str = Field(..., description="Target language code (ru, en, sr, zh)")
    translation_style: str = Field(default="technical", description="Translation style: technical, legal, mixed")
    translation_mode: str = Field(default="full", description="Translation mode: full (all text), filter_source (only source language)")
    use_glossary: bool = Field(default=False, description="Enable glossary-based term substitution before translation")
    collection_id: str | None = Field(default=None, description="Glossary collection ID (AD group-specific glossary)")
    batch_size: int = Field(default=50, ge=10, le=200, description="Batch size for translation")
    
    @field_validator("source_language", "target_language")
    @classmethod
    def validate_language_code(cls, v):
        """Validate language code."""
        valid_codes = {"ru", "en", "sr", "zh", "auto"}
        if v.lower() not in valid_codes:
            raise ValueError(f"Invalid language code. Must be one of: {', '.join(valid_codes)}")
        return v.lower()
    
    @field_validator("translation_style")
    @classmethod
    def validate_translation_style(cls, v):
        """Validate translation style."""
        valid_styles = {"technical", "legal", "mixed"}
        if v.lower() not in valid_styles:
            raise ValueError(f"Invalid translation style. Must be one of: {', '.join(valid_styles)}")
        return v.lower()
    
    @field_validator("translation_mode")
    @classmethod
    def validate_translation_mode(cls, v):
        """Validate translation mode."""
        valid_modes = {"full", "filter_source"}
        if v.lower() not in valid_modes:
            raise ValueError(f"Invalid translation mode. Must be one of: {', '.join(valid_modes)}")
        return v.lower()


class TranslationResponseSchema(BaseModel):
    """Schema for translation response."""
    
    success: bool
    text_units_translated: int = 0
    total_text_units: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = []
    output_file_path: str = ""
    glossary_applied: int = 0
    job_id: str = ""


class HealthCheckResponseSchema(BaseModel):
    """Schema for health check response."""
    
    status: str
    model_available: bool | None = None
    version: str = "2.0.0"


class GlossaryEntrySchema(BaseModel):
    """Schema for a glossary entry.
    
    Column names match the actual MySQL table structure.
    """
    
    id: int = Field(default=0, description="Entry ID (auto-generated)")
    ru_word: str = Field(default="", description="Russian text")
    en_word: str = Field(default="", description="English text")
    sb_word: str = Field(default="", description="Serbian text")
    ch_word: str = Field(default="", description="Chinese text")
    collection_id: str = Field(default="default", description="Glossary collection ID")


class GlossaryCreateSchema(BaseModel):
    """Schema for creating a new glossary entry.
    
    All four language fields are required.
    """
    
    ru_word: str = Field(..., min_length=1, description="Russian text")
    en_word: str = Field(..., min_length=1, description="English text")
    sb_word: str = Field(..., min_length=1, description="Serbian text")
    ch_word: str = Field(..., min_length=1, description="Chinese text")


class GlossaryUpdateSchema(BaseModel):
    """Schema for updating an existing glossary entry.
    
    All four language fields are required (full replacement).
    """
    
    ru_word: str = Field(..., min_length=1, description="Russian text")
    en_word: str = Field(..., min_length=1, description="English text")
    sb_word: str = Field(..., min_length=1, description="Serbian text")
    ch_word: str = Field(..., min_length=1, description="Chinese text")


class GlossaryListResponseSchema(BaseModel):
    """Schema for listing glossary entries."""
    
    entries: list[GlossaryEntrySchema]
    total: int


class GlossaryCollectionSchema(BaseModel):
    """Schema for a glossary collection."""
    
    id: str
    name: str = ""
    description: str = ""


class GlossaryCollectionListResponseSchema(BaseModel):
    """Schema for listing glossary collections."""

    collections: list[GlossaryCollectionSchema]


class GlossaryImportResponseSchema(BaseModel):
    """Schema for glossary CSV import result."""

    imported: int
    collection_id: str = "default"
    errors: list[str] = []
    new_collection_created: bool = False


class JournalEntrySchema(BaseModel):
    """Schema for a single journal entry."""
    
    timestamp: str = ""
    level: str = "INFO"
    stage: str = ""
    message: str = ""
    filename: str = ""


class JournalResponseSchema(BaseModel):
    """Schema for journal response."""
    
    date: str = ""
    entries: list[JournalEntrySchema] = []
    total: int = 0


class JobCreateResponseSchema(BaseModel):
    """Schema returned after submitting a translation job."""
    
    job_id: str
    status: str = "pending"
    queue_position: int = 0
    message: str = "Translation job created"


class BatchJobItemSchema(BaseModel):
    """Schema for a single job within a batch submission."""
    
    job_id: str
    filename: str
    status: str = "pending"
    queue_position: int = 0


class BatchJobCreateResponseSchema(BaseModel):
    """Schema returned after submitting multiple files."""
    
    jobs: list[BatchJobItemSchema]
    total: int
    message: str = "Batch translation jobs created"


class JobStatusSchema(BaseModel):
    """Schema for job status query response."""

    job_id: str
    user_id: str = ""
    status: str
    progress: float = 0.0
    current_stage: str = ""
    total_batches: int = 0
    completed_batches: int = 0
    total_text_units: int = 0
    translated_text_units: int = 0
    queue_position: int | None = None
    eta_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    error_message: str = ""
    output_file_path: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""


class ValidationResultSchema(BaseModel):
    """Schema for a single validation result."""
    
    code: str
    message: str
    severity: str = "error"
    details: dict[str, Any] = {}


class ValidationReportSchema(BaseModel):
    """Schema for the full validation report."""
    
    passed: bool = True
    errors: list[ValidationResultSchema] = []
    warnings: list[ValidationResultSchema] = []
    
    @classmethod
    def from_domain(cls, report: Any) -> "ValidationReportSchema":
        """Convert from domain ValidationReport."""
        return cls(
            passed=report.passed,
            errors=[
                ValidationResultSchema(code=r.code, message=r.message, severity=r.severity.value, details=r.details)
                for r in report.errors
            ],
            warnings=[
                ValidationResultSchema(code=r.code, message=r.message, severity=r.severity.value, details=r.details)
                for r in report.warnings
            ],
        )


class LoginRequestSchema(BaseModel):
    """Schema for login request."""
    
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponseSchema(BaseModel):
    """Schema for login response."""
    
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    username: str = ""
    role: str = ""
    expires_in: int = 1800


class RefreshTokenRequestSchema(BaseModel):
    """Schema for refresh token request."""
    
    refresh_token: str


class RefreshTokenResponseSchema(BaseModel):
    """Schema for refresh token response."""
    
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 1800


class UserSchema(BaseModel):
    """Schema for user information."""
    
    user_id: str = ""
    username: str = ""
    display_name: str = ""
    role: str = "viewer"
    is_active: bool = True
    created_at: str = ""
    last_login_at: str = ""
    ldap_groups: list[str] | None = None


class UserCreateSchema(BaseModel):
    """Schema for creating a new user."""
    
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=4, max_length=256)
    display_name: str = Field(default="", max_length=200)
    role: str = Field(default="viewer", description="admin, operator, viewer, api")


class AuthCheckResponseSchema(BaseModel):
    """Schema for auth status check."""
    
    authenticated: bool
    username: str = ""
    role: str = ""
    permissions: list[str] = []



class DbHealthSchema(BaseModel):
    """Schema for database health check."""


    available: bool
    reason: str = ''
    tables: list[dict] = []



class TableStatusSchema(BaseModel):
    """Schema for individual table status."""


    table: str
    available: bool = False
    errors: list[str] = []
    row_count: int = 0


class FeedbackCreateSchema(BaseModel):
    """Schema for creating a feedback message."""
    message: str = Field(..., min_length=1, max_length=5000, description="Feedback text")


class FeedbackEntrySchema(BaseModel):
    """Schema for a feedback entry."""
    id: int
    user_id: str
    username: str
    message: str
    created_at: str