"""Debug artifact retention helpers (diagnostics group 7).

When `DEBUG_KEEP_ARTIFACTS=1` (global) or a job carries `keep_artifacts=True`
(per-job metadata) the job's temp dirs — including the OKAPI/XLIFF working
dirs — survive the post-download cleanup and the periodic orphan sweep, so
diagnostics tools (unit locator, leak scanner, page-frame rendering, catalog
report) can keep working after the job would normally be deleted.

Retained dirs are marked with a sidecar file (``RETAINED_MARKER``) that
records the owning ``job:<uuid>`` and the retention timestamp. This keeps the
job association alive even after the Redis job record expires.

Purging is bounded by two env knobs (applied during the periodic sweep):

- ``DEBUG_KEEP_ARTIFACTS_MAX_DIRS`` — keep at most N retained dirs
  (default 20)
- ``DEBUG_KEEP_ARTIFACTS_MAX_AGE_SECONDS`` — drop retained dirs older than
  this (default 7 days)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

RETAINED_MARKER = ".diagnostics-retained"

# Temp-dir prefixes the pipeline (and therefore the retention system) uses.
_TEMP_PREFIXES = ("translator_*", "docx_okapi_*", "tikal_*", "oda_*", "pdf_convert_*")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a 0/1/true/false/yes/no/on/off env var."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def retention_enabled(job: Any | None = None) -> bool:
    """Global ``DEBUG_KEEP_ARTIFACTS`` flag OR per-job ``keep_artifacts`` metadata."""
    if env_bool("DEBUG_KEEP_ARTIFACTS"):
        return True
    if job is not None and bool((getattr(job, "metadata", None) or {}).get("keep_artifacts")):
        return True
    return False


def mark_retained(dir_path: str | Path, job_id: str) -> None:
    """Write the sidecar marker that associates ``dir_path`` with ``job_id``.

    Never raises: missing dirs are silently ignored (the caller treats a
    missing dir as "already cleaned").
    """
    d = Path(dir_path)
    if not d.exists():
        return
    try:
        payload = {
            "job_id": job_id,
            "retained_at": datetime.now(timezone.utc).isoformat(),
            "note": "Retained for diagnostics; purge via purge_debug_artifacts()",
        }
        (d / RETAINED_MARKER).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Failed to mark retained dir %s: %s", dir_path, e)


def load_retained_info(d: Path) -> dict | None:
    """Read a retained-dir marker. Returns ``None`` if the dir is not retained.

    The returned dict merges the marker's JSON with ``path`` and a guaranteed
    ``job_id`` + ``retained_at`` (falling back to the dir's mtime for legacy
    or corrupted markers).
    """
    marker = d / RETAINED_MARKER
    if not marker.is_file():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    info.setdefault("job_id", "")
    try:
        info.setdefault(
            "retained_at",
            datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc).isoformat(),
        )
    except OSError:
        info.setdefault("retained_at", datetime.now(timezone.utc).isoformat())
    info["path"] = str(d)
    return info


def collect_retained_dirs(temp_root: str | Path | None = None) -> list[dict]:
    """List all retained artifact dirs under ``temp_root`` (or the system temp dir)."""
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    result: list[dict] = []
    for pattern in _TEMP_PREFIXES:
        for d in root.glob(pattern):
            info = load_retained_info(d)
            if info:
                result.append(info)
    return result


def _parse_ts(info: dict, mtime: float | None = None) -> datetime:
    try:
        return datetime.fromisoformat(info["retained_at"])
    except (KeyError, ValueError, TypeError):
        # Legacy/corrupt marker — use the directory's mtime.
        return datetime.fromtimestamp(mtime or Path(info["path"]).stat().st_mtime, tz=timezone.utc)


def purge_debug_artifacts(
    max_dirs: int | None = None,
    max_age_seconds: int | None = None,
    temp_root: str | Path | None = None,
    on_purge: Callable[[dict], None] | None = None,
) -> int:
    """Delete retained dirs beyond ``max_dirs`` or older than ``max_age_seconds``.

    Age-based purge runs first (older than ``max_age_seconds``), then the
    cap-based purge deletes the oldest dirs beyond ``max_dirs``.

    ``on_purge(info)`` is invoked with each purged dir's info dict (used by the
    API layer for the audit log). Returns the number of purged dirs.
    """
    max_dirs = max_dirs if max_dirs is not None else max(
        1, int(os.environ.get("DEBUG_KEEP_ARTIFACTS_MAX_DIRS", "20"))
    )
    max_age = max_age_seconds if max_age_seconds is not None else max(
        3600, int(os.environ.get("DEBUG_KEEP_ARTIFACTS_MAX_AGE_SECONDS", str(7 * 86400)))
    )

    retained = collect_retained_dirs(temp_root)
    now = datetime.now(timezone.utc)
    purged = 0

    survivors: list[dict] = []
    for info in retained:
        try:
            mtime = Path(info["path"]).stat().st_mtime
        except OSError:
            mtime = None
        if (now - _parse_ts(info, mtime)).total_seconds() > max_age:
            _rm_retained_dir(info["path"])
            purged += 1
            if on_purge:
                on_purge(info)
        else:
            survivors.append(info)

    # Cap-based purge: delete the oldest survivors beyond the cap.
    if len(survivors) > max_dirs:
        survivors.sort(key=lambda info: _parse_ts(info))
        for info in survivors[: len(survivors) - max_dirs]:
            _rm_retained_dir(info["path"])
            purged += 1
            if on_purge:
                on_purge(info)

    if purged:
        logger.info(
            "Debug artifact purge removed %s retained dir(s) (max_dirs=%s, max_age=%ss)",
            purged,
            max_dirs,
            max_age,
        )
    return purged


def _rm_retained_dir(path: str) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to purge retained dir %s: %s", path, e)