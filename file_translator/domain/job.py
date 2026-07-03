"""Job management domain models for async processing with cancellation and progress."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JobStatus(Enum):
    """Status of a translation job."""
    
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingStage(Enum):
    """Stage of document processing within a job."""
    
    QUEUED = "queued"
    RECEIVED = "received"
    VALIDATION = "validation"
    EXTRACTION = "extraction"
    GLOSSARY = "glossary"
    TRANSLATION = "translation"
    SAVE = "save"
    COMPLETED = "completed"
    FAILED = "failed"


_STAGE_WEIGHTS: dict[ProcessingStage, float] = {
    ProcessingStage.QUEUED: 0.0,
    ProcessingStage.RECEIVED: 0.0,
    ProcessingStage.VALIDATION: 0.05,
    ProcessingStage.EXTRACTION: 0.20,
    ProcessingStage.GLOSSARY: 0.25,
    ProcessingStage.TRANSLATION: 0.80,
    ProcessingStage.SAVE: 0.95,
    ProcessingStage.COMPLETED: 1.0,
    ProcessingStage.FAILED: 1.0,
}


@dataclass
class Job:
    """Represents an async translation job with progress tracking.
    
    Each translation operation creates a Job that:
    - Can be queried for status/progress/ETA
    - Can be cancelled by the user
    - Tracks progress through processing stages
    """
    
    job_id: str
    user_id: str = ""
    status: JobStatus = JobStatus.PENDING
    filename: str = ""
    source_language: str = ""
    target_language: str = ""
    translation_style: str = "technical"
    current_stage: ProcessingStage = ProcessingStage.QUEUED
    progress: float = 0.0  # 0.0 to 1.0
    total_batches: int = 0
    completed_batches: int = 0
    total_text_units: int = 0
    translated_text_units: int = 0
    eta_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    error_message: str = ""
    output_file_path: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
    
    @property
    def is_active(self) -> bool:
        """Check if the job is still running or pending."""
        return self.status in (JobStatus.PENDING, JobStatus.RUNNING)
    
    @property
    def is_terminal(self) -> bool:
        """Check if the job has reached a terminal state."""
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
    
    @property
    def can_cancel(self) -> bool:
        """Check if the job can be cancelled."""
        return self.status == JobStatus.RUNNING
    
    def update_progress(self, stage: ProcessingStage, batch_index: int = 0,
                        total_batches: int = 0) -> None:
        """Update progress based on current stage and batch progress.
        
        If currently in TRANSLATION stage, progress interpolates between
        the stage start weight and the stage end weight based on batch progress.
        """
        self.current_stage = stage
        
        if stage == ProcessingStage.TRANSLATION and total_batches > 0:
            stage_start = _STAGE_WEIGHTS[ProcessingStage.EXTRACTION]
            stage_end = _STAGE_WEIGHTS[ProcessingStage.SAVE]
            batch_progress = batch_index / total_batches if total_batches > 0 else 0
            self.progress = stage_start + (stage_end - stage_start) * batch_progress
        else:
            self.progress = _STAGE_WEIGHTS.get(stage, 0.0)
    
    def estimate_eta(self, elapsed: float, batch_index: int, total_batches: int) -> float:
        """Estimate remaining time based on batch progress."""
        if batch_index <= 0 or total_batches <= 0:
            return 0.0
        per_batch = elapsed / batch_index
        remaining_batches = total_batches - batch_index
        return per_batch * remaining_batches
