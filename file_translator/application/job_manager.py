"""Job manager - Async job lifecycle with cancellation and progress tracking."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from file_translator.domain.interfaces import JobRepository
from file_translator.domain.job import Job, JobStatus, ProcessingStage

logger = logging.getLogger(__name__)


class JobManager:
    """Manages async translation job lifecycle.
    
    - Creates jobs with unique IDs for each translation request
    - Tracks progress through processing stages
    - Supports cancellation (graceful stop at next safe point)
    - Provides status, progress, and ETA queries
    """
    
    def __init__(self, repository: JobRepository | None = None):
        self._repository = repository
    
    @property
    def repository(self) -> JobRepository:
        """Get the job repository instance."""
        if not self._repository:
            raise RuntimeError("JobRepository is not configured")
        return self._repository
    
    async def create_job(
        self,
        filename: str,
        source_language: str,
        target_language: str,
        translation_style: str = "technical",
        user_id: str = "",
    ) -> Job:
        """Create a new translation job with a unique ID."""
        job = Job(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            status=JobStatus.PENDING,
            filename=filename,
            source_language=source_language,
            target_language=target_language,
            translation_style=translation_style,
            current_stage=ProcessingStage.QUEUED,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await self.repository.create(job)
        logger.info(f"Job created: {job.job_id} for {filename}")
        return job
    
    async def start_job(self, job_id: str) -> Job | None:
        """Mark a job as running (transition from PENDING to RUNNING)."""
        job = await self.repository.get(job_id)
        if not job:
            return None
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc).isoformat()
        job.current_stage = ProcessingStage.RECEIVED
        await self.repository.update(job)
        return job
    
    async def update_progress(
        self,
        job_id: str,
        stage: ProcessingStage,
        batch_index: int = 0,
        total_batches: int = 0,
        total_text_units: int = 0,
        translated_text_units: int = 0,
    ) -> Job | None:
        """Update job progress and recompute ETA."""
        job = await self.repository.get(job_id)
        if not job or not job.is_active:
            return job
        
        job.current_stage = stage
        job.total_batches = total_batches or job.total_batches
        job.completed_batches = batch_index
        job.total_text_units = total_text_units or job.total_text_units
        job.translated_text_units = translated_text_units or job.translated_text_units
        
        job.update_progress(stage, batch_index, job.total_batches)
        
        # Update elapsed and ETA
        if job.started_at:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job.started_at)).total_seconds()
            job.elapsed_seconds = elapsed
            if stage == ProcessingStage.TRANSLATION and batch_index > 0:
                job.eta_seconds = job.estimate_eta(elapsed, batch_index, job.total_batches)
        
        await self.repository.update(job)
        return job
    
    async def complete_job(self, job_id: str, output_file_path: str = "") -> Job | None:
        """Mark a job as completed successfully."""
        job = await self.repository.get(job_id)
        if not job:
            return None
        job.status = JobStatus.COMPLETED
        job.current_stage = ProcessingStage.COMPLETED
        job.progress = 1.0
        job.completed_at = datetime.now(timezone.utc).isoformat()
        job.output_file_path = output_file_path
        if job.started_at:
            job.elapsed_seconds = (
                datetime.now(timezone.utc) - datetime.fromisoformat(job.started_at)
            ).total_seconds()
        await self.repository.update(job)
        logger.info(f"Job completed: {job_id}")
        return job
    
    async def fail_job(self, job_id: str, error_message: str) -> Job | None:
        """Mark a job as failed."""
        job = await self.repository.get(job_id)
        if not job:
            return None
        job.status = JobStatus.FAILED
        job.current_stage = ProcessingStage.FAILED
        job.progress = 1.0
        job.error_message = error_message
        job.completed_at = datetime.now(timezone.utc).isoformat()
        await self.repository.update(job)
        logger.info(f"Job failed: {job_id} - {error_message}")
        return job
    
    async def cancel_job(self, job_id: str) -> Job | None:
        """Cancel an active job.
        
        Only running jobs can be cancelled. The cancellation is
        respected at the next safe checkpoint during processing.
        """
        job = await self.repository.get(job_id)
        if not job:
            return None
        if not job.can_cancel:
            logger.warning(f"Job {job_id} cannot be cancelled (status: {job.status.value})")
            return job
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(timezone.utc).isoformat()
        await self.repository.update(job)
        logger.info(f"Job cancelled: {job_id}")
        return job
    
    async def get_job(self, job_id: str) -> Job | None:
        """Get job status and progress."""
        return await self.repository.get(job_id)
    
    async def get_active_jobs(self) -> list[Job]:
        """List all currently active jobs."""
        return await self.repository.list_active()
    
    async def get_recent_jobs(self, limit: int = 10) -> list[Job]:
        """List most recent jobs."""
        return await self.repository.list_recent(limit)
    
    async def is_cancelled(self, job_id: str) -> bool:
        """Check if a job has been cancelled (polled during processing)."""
        job = await self.repository.get(job_id)
        return job is not None and job.status == JobStatus.CANCELLED

    async def delete_job(self, job_id: str) -> bool:
        """Permanently delete a job from the repository."""
        return await self.repository.delete(job_id)
