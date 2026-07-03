"""Per-user async queue for sequential translation job processing.

Each user gets their own FIFO queue. A single background worker
processes jobs from the queue one at a time, preventing concurrent
LLM API calls for the same user.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class UserJobQueue:
    """Manages per-user queues ensuring one translation job at a time per user.
    
    Queue positions are tracked by job_id so the frontend can display
    wait status. Workers are lazily started when a job is enqueued
    and stop when the queue is empty (restarted on next enqueue).
    """

    def __init__(self, process_func: Callable[[str, str, Any], Awaitable[None]]):
        """
        Args:
            process_func: Async callable accepting (job_id, file_path, request).
                          Called for each dequeued job.
        """
        self._process_func = process_func
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._position_map: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, user_id: str, job_id: str, file_path: str, request: Any) -> int:
        """Add a job to the user's queue. Returns the 0-based queue position."""
        async with self._lock:
            if user_id not in self._queues:
                self._queues[user_id] = asyncio.Queue()

            await self._queues[user_id].put((job_id, file_path, request))
            position = self._queues[user_id].qsize() - 1
            self._position_map[job_id] = position

        if user_id not in self._workers or self._workers[user_id].done():
            self._workers[user_id] = asyncio.create_task(self._worker(user_id))

        return position

    async def get_position(self, job_id: str) -> int | None:
        """Get the current queue position for a job, or None if not queued."""
        async with self._lock:
            return self._position_map.get(job_id)

    async def cancel_pending(self, job_id: str) -> bool:
        """Mark a pending (not yet started) job as cancelled.
        
        The actual cancellation is handled by the job's status check
        in the processing pipeline. This just cleans up queue tracking.
        Returns True if the job was still in the queue.
        """
        async with self._lock:
            if job_id in self._position_map:
                del self._position_map[job_id]
                return True
            return False

    async def _worker(self, user_id: str) -> None:
        """Process jobs for a user one at a time.
        
        On asyncio.CancelledError (e.g., server shutdown), drains remaining
        queue items and cleans up position map before exiting to prevent
        task loss. On processing errors, continues to the next item instead
        of stopping.
        """
        queue = self._queues.get(user_id)
        if not queue:
            return

        while True:
            try:
                job_id, file_path, request = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # No items for 30s — check if we should stop (queue removed)
                async with self._lock:
                    if user_id not in self._queues or queue.empty():
                        if user_id in self._queues:
                            del self._queues[user_id]
                        self._workers.pop(user_id, None)
                        break
                continue
            
            async with self._lock:
                # Remove current job from position map and recalculate all positions
                self._position_map.pop(job_id, None)
                for jid in list(self._position_map.keys()):
                    if self._position_map[jid] > 0:
                        self._position_map[jid] -= 1

            try:
                logger.info(f"Queue worker started job {job_id} for user {user_id}")
                await self._process_func(job_id, file_path, request)
                logger.info(f"Queue worker completed job {job_id} for user {user_id}")
            except asyncio.CancelledError:
                # Server shutdown or task cancellation — drain remaining items
                logger.warning(f"Worker cancelled for user {user_id}, draining queue")
                while True:
                    try:
                        item = queue.get_nowait()
                        jid, _, _ = item
                        self._position_map.pop(jid, None)
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        # No more items available — stop draining but ensure consistency
                        break
                async with self._lock:
                    if user_id in self._queues and self._queues[user_id] is queue:
                        del self._queues[user_id]
                    self._workers.pop(user_id, None)
                break
            except Exception as e:
                logger.error(f"Queue worker error for user {user_id} on job {job_id}: {e}")
                # Continue to next item — don't stop the entire queue

            async with self._lock:
                if not queue.empty():
                    continue  # More items waiting, process them too
                if user_id in self._queues and self._queues[user_id] is queue:
                    del self._queues[user_id]
                self._workers.pop(user_id, None)
                break
