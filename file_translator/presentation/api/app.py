"""FastAPI application - API endpoints and routing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pymysql
from fastapi import Depends, FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi import status

from file_translator.domain.auth import AuthCredentials

from file_translator.infrastructure.auth.jwt_auth_provider import JwtAuthProvider
from file_translator.infrastructure.auth.ldap_service import LdapService
from file_translator.infrastructure.providers.mongo_provider import MongoProvider
from file_translator.infrastructure.repositories.auth_repository import (
    MongoSessionRepository,
    MongoUserRepository,
)
from file_translator.infrastructure.repositories.redis_auth_repository import RedisTokenBlacklist

from file_translator.application.schemas import (
    BatchJobCreateResponseSchema,
    BatchJobItemSchema,
    FeedbackCreateSchema,
    FeedbackEntrySchema,
    GlossaryCollectionListResponseSchema,
    GlossaryCollectionSchema,
    GlossaryCreateSchema,
    GlossaryEntrySchema,
    GlossaryImportResponseSchema,
    GlossaryListResponseSchema,
    GlossaryUpdateSchema,
    HealthCheckResponseSchema,
    JobCreateResponseSchema,
    JobStatusSchema,
    JournalEntrySchema,
    JournalResponseSchema,
    LoginRequestSchema,
    RefreshTokenRequestSchema,
    RefreshTokenResponseSchema,
    TranslationRequestSchema,
    TranslationResponseSchema,
    UserCreateSchema,
    UserSchema,
    ValidationReportSchema,
)
from file_translator.application.auth_service import AuthService
from file_translator.application.service import TranslationService
from file_translator.application.user_queue import UserJobQueue
from file_translator.domain.auth import Permission, RoleType
from file_translator.domain.glossary import GlossaryEntry
from file_translator.domain.job import Job, JobStatus
from file_translator.presentation.api.dependencies import get_current_user, require_permission
from file_translator import __version__

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create FastAPI application
app = FastAPI(
    title="File Translator API",
    description="Industrial document translation system with LLM support",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Configuration from environment
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "file_translator_auth")

# JWT_SECRET is required — fail at startup if missing
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is required. Set it in .env file.")

DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(128 * 1024 * 1024)))  # 128 MB

_HELP_URL = os.getenv("HELP_URL", "")

_CREDENTIAL_WARNINGS_SHOWN = False

# Add CORS middleware - restrict origins, methods, and headers
if not CORS_ORIGINS:
    logger.warning("CORS_ORIGINS not set. For production, set it explicitly! For dev: CORS_ORIGINS=http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
    max_age=86400,
)

# Initialize MongoDB provider
mongo_provider = MongoProvider()

# Initialize service (singleton pattern)
translation_service = TranslationService()


# --- Background translation job runner ---


def _safe_filename(filename: str) -> str:
    """Sanitize a user-supplied filename against path traversal and reserved names.

    Strips directory components, null bytes, and rejects Windows reserved names
    (CON, NUL, LPTx, etc.) to prevent filesystem abuse.
    """
    # Strip null bytes and any path components
    clean = Path(filename).name.replace("\x00", "")
    if not clean:
        return "unnamed_file"

    # Reject Windows reserved names (CON, PRN, AUX, NUL, COMx, LPTx)
    stem = Path(clean).stem.upper().rstrip(" .")
    if stem in ("CON", "PRN", "AUX", "NUL", "CLOCK$") or re.match(
        r"^(COM|LPT)\d+$", stem
    ):
        raise ValueError(f"Filename '{filename}' uses a reserved system name")

    return clean


async def _check_file_size(file: UploadFile) -> None:
    """Check file size without reading the entire file into memory."""
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {MAX_UPLOAD_SIZE // (1024*1024)} MB",
        )


async def _run_translation_job(job_id: str, file_path: str, request: TranslationRequestSchema) -> None:
    """Run a translation job in the background.

    Used by POST /jobs to process documents asynchronously.
    Temp directory is cleaned up on failure/cancellation, or after download.
    
    Explicitly transitions to RUNNING before processing to prevent status
    inconsistency if server crashes between queue dequeue and start of work.
    """
    temp_dir = str(Path(file_path).parent)
    
    try:
        # Check cancellation BEFORE starting any work (job might have been cancelled while in queue)
        job = await translation_service.job_manager.get_job(job_id)
        if job and job.status.value == "cancelled":
            logger.info(f"Job {job_id} was cancelled before processing started")
            return
        
        # Explicitly transition to RUNNING - prevents inconsistency on crash
        running_job = await translation_service.job_manager.start_job(job_id)
        if not running_job:
            logger.warning(f"Could not start job {job_id}, it may have been deleted")
            return
        if running_job.status.value == "cancelled":
            logger.info(f"Job {job_id} was cancelled after starting")
            return
            
        logger.info(f"Background job {job_id} started: {file_path}")
        result = await translation_service.translate_document(
            file_path, request, job_id=job_id,
        )
        if result.success:
            logger.info(f"Background job {job_id} completed successfully")
            # Store temp_dir in job metadata so download endpoint can clean up
            job = await translation_service.job_manager.get_job(job_id)
            if job:
                job.metadata["temp_dir"] = temp_dir
                await translation_service.job_manager.repository.update(job)
        else:
            errors = "; ".join(result.errors) if result.errors else "Unknown error"
            logger.error(f"Background job {job_id} failed: {errors}")
            _cleanup_temp_dir(temp_dir, job_id)
    except Exception as e:
        logger.error(f"Background job {job_id} crashed: {e}", exc_info=True)
        try:
            await translation_service.job_manager.fail_job(job_id, str(e))
            _cleanup_temp_dir(temp_dir, job_id)
        except Exception:
            pass


def _cleanup_temp_dir(temp_dir: str, job_id: str) -> None:
    """Clean up temp directory with proper error logging (no silent ignores)."""
    try:
        shutil.rmtree(temp_dir)
    except Exception as cleanup_error:
        logger.critical(f"Failed to cleanup temp_dir {temp_dir} for job {job_id}: {cleanup_error}")


# Per-user job queue for sequential processing
user_job_queue = UserJobQueue(process_func=_run_translation_job)


# --- Periodic cleanup: temp dirs (1h TTL) + expired tokens ---

async def _cleanup_orphaned_temp_dirs(interval: int = 1800) -> None:
    """Background task that deletes ALL translator_* temp dirs older than 1 hour
    and removes stale terminal jobs from Redis.

    Filesystem scan catches everything — error, completed, downloaded, orphaned.
    Redis TTL (JOB_TTL_SECONDS=3600) handles job record cleanup automatically;
    this is a safety net for edge cases.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            cutoff_ts = now.timestamp() - 3600  # 1 hour ago

            # ── Filesystem scan: delete ALL translator_*/docx_okapi_*/tikal_*
            #    dirs older than 1 hour. Covers every temp dir the system creates. ──
            temp_root = Path(tempfile.gettempdir())
            cleaned_dirs = 0
            for prefix_pattern in ("translator_*", "docx_okapi_*", "tikal_*"):
                for d in temp_root.glob(prefix_pattern):
                    try:
                        mtime = d.stat().st_mtime
                        if mtime < cutoff_ts:
                            _cleanup_temp_dir(str(d), "auto_cleanup")
                            cleaned_dirs += 1
                    except OSError:
                        pass

            if cleaned_dirs:
                logger.info(f"Periodic cleanup removed {cleaned_dirs} temp dir(s) older than 1 hour")

            # ── Redis safety net: delete jobs older than 1h from creation ──
            try:
                jobs = await translation_service.job_manager.get_recent_jobs(limit=500)
                cleaned_jobs = 0
                for job in jobs:
                    created = getattr(job, 'created_at', None)
                    if created:
                        try:
                            created_dt = datetime.fromisoformat(created)
                            if (now - created_dt).total_seconds() > 3600:
                                await translation_service.job_manager.delete_job(job.job_id)
                                cleaned_jobs += 1
                        except (ValueError, TypeError):
                            pass
                if cleaned_jobs:
                    logger.info(f"Periodic cleanup removed {cleaned_jobs} stale job(s) from Redis")
            except Exception as e:
                logger.debug(f"Redis job cleanup skipped: {e}")

            # ── Clean expired refresh tokens ──
            try:
                session_repo = app.state.session_repo if hasattr(app.state, "session_repo") else None
                if session_repo:
                    deleted_tokens = await session_repo.cleanup_expired_refresh_tokens()
                    if deleted_tokens:
                        logger.info(f"Periodic cleanup removed {deleted_tokens} expired refresh tokens")
            except Exception as e:
                logger.debug(f"Refresh token cleanup skipped: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}")


@app.on_event("startup")
async def _startup() -> None:
    # Start periodic cleanup
    asyncio.create_task(_cleanup_orphaned_temp_dirs())

    # Warn about missing required env vars
    if not DEFAULT_ADMIN_PASSWORD:
        logger.warning("DEFAULT_ADMIN_PASSWORD not set — admin user disabled. Set DEFAULT_ADMIN_PASSWORD.")
    if DEFAULT_ADMIN_PASSWORD and DEFAULT_ADMIN_PASSWORD == "admin":
        logger.warning("DEFAULT_ADMIN_PASSWORD is set to default 'admin' — CHANGE IT in production!")
    if len(DEFAULT_ADMIN_PASSWORD) > 72:
        logger.warning("DEFAULT_ADMIN_PASSWORD exceeds 72 bytes — bcrypt will truncate it.")

    # Connect MongoDB
    try:
        await mongo_provider.connect(MONGO_URI, MONGO_DB_NAME)
        user_repo = MongoUserRepository(mongo_provider.db)
        session_repo = MongoSessionRepository(mongo_provider.db)
        
        # Initialize Redis-backed token blacklist
        token_blacklist = RedisTokenBlacklist()
        jwt_provider = JwtAuthProvider(JWT_SECRET, user_repo, token_blacklist)

        app.state.auth_service = AuthService(
            auth_provider=jwt_provider,
            user_repository=user_repo,
        )
        app.state.auth_service.session_repo = session_repo
        app.state.jwt_provider = jwt_provider
        app.state.user_repo = user_repo
        app.state.session_repo = session_repo

        ldap_service = LdapService.from_env()
        if ldap_service:
            app.state.ldap_service = ldap_service
            app.state.auth_service.ldap_service = ldap_service
            logger.info("LDAP authentication enabled")

        from file_translator.domain.auth import User as AuthUser, RoleType
        import uuid
        from datetime import datetime, timezone

        existing = await user_repo.get_by_username("admin")
        if not existing:
            admin = AuthUser(
                user_id=str(uuid.uuid4()),
                username="admin",
                display_name="Administrator",
                role=RoleType.ADMIN,
                is_active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            admin.password_hash = jwt_provider.hash_password(DEFAULT_ADMIN_PASSWORD)
            await user_repo.create(admin)
            logger.info("Default admin user created")

        logger.info("Auth system initialized with MongoDB + JWT")

        # Fail orphaned jobs from a previous server restart
        try:
            jobs = await translation_service.job_manager.get_recent_jobs(limit=100)
            orphaned = [j for j in jobs if j.status in (JobStatus.RUNNING, JobStatus.PENDING)]
            for job in orphaned:
                await translation_service.job_manager.fail_job(
                    job.job_id,
                    "Сервер был перезапущен — задача прервана. Отправьте файл снова.",
                )
                logger.warning(f"Orphaned job {job.job_id} failed on startup recovery")
            if orphaned:
                logger.info(f"Startup recovery: failed {len(orphaned)} orphaned job(s)")
        except Exception as e:
            logger.warning(f"Job recovery skipped: {e}")

    except Exception as e:
        logger.warning(f"MongoDB not available, using dev auth: {e}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await mongo_provider.close()


# Auth router
from file_translator.presentation.api.auth_router import router as auth_router
app.include_router(auth_router)

# Auth middleware
from file_translator.presentation.api.middleware import AuthMiddleware
app.add_middleware(AuthMiddleware)


# --- Translation endpoints ---


@app.post("/jobs", response_model=JobCreateResponseSchema, status_code=201)
async def create_translation_job(
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.TRANSLATE)),
    file: UploadFile = File(...),
    source_language: str = Form("en"),
    target_language: str = Form("ru"),
    translation_style: str = Form("technical"),
    translation_mode: str = Form("full"),
    use_glossary: bool = Form(False),
    collection_id: str = Form(""),
    batch_size: int = Form(50),
):
    """Submit a document for async translation.

    Creates a translation job and returns immediately with a job_id.
    Files for the same user are processed sequentially.
    Poll GET /job/{job_id} for progress, cancel via POST /job/{job_id}/cancel,
    and download the result from GET /job/{job_id}/download when completed.
    """
    if not 10 <= batch_size <= 200:
        raise HTTPException(
            status_code=400,
            detail=f"batch_size must be between 10 and 200, got {batch_size}",
        )
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="Filename is required"
        )
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.docx', '.doc', '.xlsx', '.xls', '.dxf', '.dwg'):
        raise HTTPException(
            status_code=400,
            detail="Only DOCX, DOC, XLSX, XLS, DXF and DWG files are currently supported"
        )

    await _check_file_size(file)

    temp_dir = Path(tempfile.mkdtemp(prefix="translator_"))
    input_file = temp_dir / _safe_filename(file.filename)

    content = await file.read()
    input_file.write_bytes(content)

    request_schema = TranslationRequestSchema(
        source_language=source_language,
        target_language=target_language,
        translation_style=translation_style,
        translation_mode=translation_mode,
        use_glossary=use_glossary,
        collection_id=collection_id or None,
        batch_size=batch_size,
    )

    user_id = request.state.auth.user.user_id

    job = await translation_service.job_manager.create_job(
        filename=file.filename,
        source_language=source_language,
        target_language=target_language,
        translation_style=translation_style,
        user_id=user_id,
    )
    job.metadata["temp_dir"] = str(temp_dir)
    await translation_service.job_manager.repository.update(job)
    queue_position = await user_job_queue.enqueue(
        user_id, job.job_id, str(input_file), request_schema,
    )

    logger.info(f"Translation job {job.job_id} created for {file.filename} (queue pos {queue_position})")
    return JobCreateResponseSchema(
        job_id=job.job_id, status=job.status.value, queue_position=queue_position,
    )


@app.post("/jobs/batch", response_model=BatchJobCreateResponseSchema, status_code=201)
async def create_batch_translation_jobs(
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.TRANSLATE)),
    files: list[UploadFile] = File(...),
    source_language: str = Form("en"),
    target_language: str = Form("ru"),
    translation_style: str = Form("technical"),
    translation_mode: str = Form("full"),
    use_glossary: bool = Form(False),
    collection_id: str = Form(""),
    batch_size: int = Form(50),
):
    """Submit multiple documents for async translation in one request.

    Each file gets its own job_id. Files for the same user are processed
    sequentially in queue order (per-user FIFO queue).
    Returns all job IDs immediately for individual polling/download.
    """
    if not 10 <= batch_size <= 200:
        raise HTTPException(
            status_code=400,
            detail=f"batch_size must be between 10 and 200, got {batch_size}",
        )

    MAX_BATCH_FILES = 50
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_FILES} files per batch request",
        )

    user_id = request.state.auth.user.user_id
    request_schema = TranslationRequestSchema(
        source_language=source_language,
        target_language=target_language,
        translation_style=translation_style,
        translation_mode=translation_mode,
        use_glossary=use_glossary,
        collection_id=collection_id or None,
        batch_size=batch_size,
    )

    jobs: list[BatchJobItemSchema] = []

    for file in files:
        if not file.filename:
            continue
        ext = Path(file.filename).suffix.lower()
        if ext not in ('.docx', '.doc', '.xlsx', '.xls'):
            logger.warning(f"Skipping unsupported file: {file.filename}")
            continue

        await _check_file_size(file)

        temp_dir = Path(tempfile.mkdtemp(prefix="translator_"))
        input_file = temp_dir / _safe_filename(file.filename)
        content = await file.read()
        input_file.write_bytes(content)

        job = await translation_service.job_manager.create_job(
            filename=file.filename,
            source_language=source_language,
            target_language=target_language,
            translation_style=translation_style,
            user_id=user_id,
        )
        job.metadata["temp_dir"] = str(temp_dir)
        await translation_service.job_manager.repository.update(job)

        queue_position = await user_job_queue.enqueue(
            user_id, job.job_id, str(input_file), request_schema,
        )

        logger.info(f"Batch job {job.job_id} created for {file.filename} (queue pos {queue_position})")
        jobs.append(BatchJobItemSchema(
            job_id=job.job_id,
            filename=file.filename,
            status=job.status.value,
            queue_position=queue_position,
        ))

    if not jobs:
        raise HTTPException(status_code=400, detail="No supported files provided")

    return BatchJobCreateResponseSchema(jobs=jobs, total=len(jobs))


@app.get("/health", response_model=HealthCheckResponseSchema)
async def health_check():
    """Health check endpoint."""
    model_available = None
    
    try:
        # Check if translation provider is available
        provider = await translation_service.get_provider()
        model_available = provider.is_available()
    except Exception as e:
        logger.warning(f"Model availability check failed: {e}")
        model_available = False
    
    return HealthCheckResponseSchema(
        status="healthy",
        model_available=model_available,
        version=__version__,
    )


@app.get("/version")
async def get_version():
    """Return version info and help link."""
    return {
        "version": __version__,
        "help_url": _HELP_URL,
    }


@app.get("/help", include_in_schema=True)
async def redirect_to_help():
    """Redirect to the user documentation / help page."""
    if _HELP_URL:
        return RedirectResponse(url=_HELP_URL)
    return JSONResponse(content={"detail": "Help URL not configured"}, status_code=404)


# --- User management endpoints (admin only) ---

@app.get("/auth/users", response_model=list[UserSchema])
async def list_users(
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_USERS)),
):
    """List all registered users (admin only)."""
    svc = getattr(app.state, "auth_service", None)
    if not svc:
        raise HTTPException(status_code=500, detail="Auth service not available")
    users = await svc.list_users()
    return [
        UserSchema(
            user_id=u.user_id,
            username=u.username,
            display_name=u.display_name,
            role=u.role.value,
            is_active=u.is_active,
            created_at=u.created_at,
            last_login_at=u.last_login_at or "",
            ldap_groups=getattr(u, "ldap_groups", None),
        )
        for u in users
    ]


@app.post("/auth/users", response_model=UserSchema, status_code=201)
async def create_user(
    request: Request,
    user_data: UserCreateSchema,
    _: AuthCredentials = Depends(require_permission(Permission.MANAGE_USERS)),
):
    """Create a new user (admin only)."""
    svc = getattr(app.state, "auth_service", None)
    if not svc:
        raise HTTPException(status_code=500, detail="Auth service not available")

    valid_roles = {"admin": RoleType.ADMIN, "operator": RoleType.OPERATOR,
                   "viewer": RoleType.VIEWER, "api": RoleType.API}
    role = valid_roles.get(user_data.role.lower())
    if not role:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{user_data.role}'. Must be one of: {', '.join(valid_roles)}",
        )

    user = await svc.create_user(
        username=user_data.username,
        password=user_data.password,
        role=role,
        display_name=user_data.display_name,
    )
    return UserSchema(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@app.post("/translate")
async def translate_document(
    _: AuthCredentials = Depends(require_permission(Permission.TRANSLATE)),
    file: UploadFile = File(...),
    source_language: str = Form("en"),
    target_language: str = Form("ru"),
    translation_style: str = Form("technical"),
    translation_mode: str = Form("full"),
    use_glossary: bool = Form(False),
    collection_id: str = Form(""),
    batch_size: int = Form(50),
):
    """Translate a document from one language to another.
    
    Supports DOCX and DOC files with full formatting preservation.
    DOC files are automatically converted to DOCX before translation.
    Uses LLM (qwen3-based) for translation.
    
    Args:
        file: Uploaded DOCX or DOC document file.
        source_language: Source language code (en, ru, sr, zh, auto).
        target_language: Target language code (en, ru, sr, zh).
        translation_style: Translation style (technical, legal, mixed).
        translation_mode: Translation mode (full, filter_source).
        use_glossary: Enable glossary-based term substitution before translation.
        batch_size: Number of text units per LLM request (10-200).
        
    Returns:
        Translated document file for download.
    """
    if not 10 <= batch_size <= 200:
        raise HTTPException(
            status_code=400,
            detail=f"batch_size must be between 10 and 200, got {batch_size}",
        )
    # Validate file extension
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="Filename is required"
        )
    allowed_extensions = ('.docx', '.doc', '.xlsx', '.xls')
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Only {', '.join(allowed_extensions)} files are supported"
        )
    
    logger.info(f"Received translation request: {file.filename}")

    await _check_file_size(file)
    
    # Create temporary directory for processing
    temp_dir = Path(tempfile.mkdtemp(prefix="translator_"))
    input_file = temp_dir / _safe_filename(file.filename)
    
    try:
        # Save uploaded file
        with open(input_file, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # Create translation request schema
        request = TranslationRequestSchema(
            source_language=source_language,
            target_language=target_language,
            translation_style=translation_style,
            translation_mode=translation_mode,
            use_glossary=use_glossary,
            collection_id=collection_id or None,
            batch_size=batch_size,
        )
        
        # Execute translation
        result = await translation_service.translate_document(
            str(input_file),
            request,
        )
        
        if not result.success:
            raise HTTPException(
                status_code=500,
                detail=result.errors[0] if result.errors else "Translation failed"
            )
        
        # Generate cleanup ID for temp dir tracking
        import uuid
        cleanup_id = str(uuid.uuid4())
        
        # Build download filename
        original_stem = Path(file.filename).stem
        download_filename = f"{original_stem}_translated.docx"
        output_path = Path(result.output_file_path)
        
        if not output_path.exists():
            raise HTTPException(status_code=500, detail="Translated file not found")
        
        # Return file for download with metadata in headers
        background_tasks = BackgroundTasks()
        background_tasks.add_task(_cleanup_temp_dir, str(temp_dir), cleanup_id)
        
        return FileResponse(
            path=output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=download_filename,
            headers={
                "X-Translation-Status": "success",
                "X-Text-Units-Translated": str(result.text_units_translated),
                "X-Total-Text-Units": str(result.total_text_units),
                "X-Duration-Seconds": str(result.duration_seconds),
            },
            background=background_tasks,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Translation failed: {e}", exc_info=True)
        # Cleanup on error
        if temp_dir.exists():
            _cleanup_temp_dir(str(temp_dir), cleanup_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/supported-formats")
async def supported_formats():
    """Return list of supported document formats."""
    return {
        "formats": ["docx", "doc", "xlsx", "xls"],
        "dxf_stub": True,
        "dxf_note": "Architecture in place, translation logic pending implementation",
        "coming_soon": ["pdf", "dwg"],
    }


@app.post("/validate", response_model=ValidationReportSchema)
async def validate_document(
    _: AuthCredentials = Depends(require_permission(Permission.TRANSLATE)),
    file: UploadFile = File(...),
    source_language: str = Form("en"),
    target_language: str = Form("ru"),
):
    """Validate a document before translation.
    
    Checks file size, accessibility, structure, languages.
    Returns a report with errors and warnings.
    """
    if not file.filename:
        return ValidationReportSchema(
            passed=False,
            errors=[{"code": "NO_FILENAME", "message": "Filename is required",
                     "severity": "error"}],
        )
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.docx', '.doc', '.xlsx', '.xls'):
        return ValidationReportSchema(
            passed=False,
            errors=[{"code": "UNSUPPORTED_FORMAT", "message": "Only DOCX, DOC, XLSX, and XLS files are currently supported",
                     "severity": "error"}],
        )
    
    await _check_file_size(file)

    temp_dir = Path(tempfile.mkdtemp(prefix="validate_"))
    input_file = temp_dir / _safe_filename(file.filename)
    
    try:
        with open(input_file, "wb") as f:
            content = await file.read()
            f.write(content)
        
        from file_translator.application.validators import (
            ConcurrentJobValidator, FileAccessValidator, FileSizeValidator,
            FileStructureValidator, LanguageMismatchValidator, ValidationChain,
        )
        
        chain = ValidationChain()
        chain.add_validator(FileSizeValidator())
        chain.add_validator(FileAccessValidator())
        chain.add_validator(FileStructureValidator())
        chain.add_validator(LanguageMismatchValidator())
        chain.add_validator(ConcurrentJobValidator())
        
        context = {
            "source_language": source_language,
            "target_language": target_language,
            "filename": file.filename,
        }
        
        report = await chain.validate_all(input_file, context)
        
        return ValidationReportSchema.from_domain(report)
    finally:
        if temp_dir.exists():
            _cleanup_temp_dir(str(temp_dir), "validation")


# --- Glossary endpoints ---

@app.get("/glossary/collections", response_model=GlossaryCollectionListResponseSchema)
async def list_glossary_collections(
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_GLOSSARY)),
):
    """List glossary collections accessible by the current user's AD groups."""
    auth: AuthCredentials = request.state.auth
    ldap_groups = getattr(auth.user, "ldap_groups", None)
    glossary_service = await translation_service.get_glossary_service()
    collections = await glossary_service.get_accessible_collections(ldap_groups)
    return GlossaryCollectionListResponseSchema(
        collections=[
            GlossaryCollectionSchema(id=c.id, name=c.name, description=c.description)
            for c in collections
        ],
    )


@app.get("/glossary", response_model=GlossaryListResponseSchema)
async def list_glossary_entries(
    request: Request,
    collection_id: str = "",
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_GLOSSARY)),
):
    """List all glossary entries, optionally filtered by collection.
    
    If collection_id is omitted, returns entries from all accessible collections.
    """
    auth: AuthCredentials = request.state.auth
    ldap_groups = getattr(auth.user, "ldap_groups", None)
    svc = await translation_service.get_glossary_service()

    if collection_id:
        allowed = svc._access_resolver.is_collection_allowed(collection_id, ldap_groups)
        if not allowed:
            raise HTTPException(status_code=403, detail=f"Collection '{collection_id}' not accessible")
        entries = await svc.get_all_entries(collection_id=collection_id)
    else:
        entries = await svc.get_all_entries()

    return GlossaryListResponseSchema(
        entries=[
            GlossaryEntrySchema(
                id=e.id,
                ru_word=e.ru_word,
                en_word=e.en_word,
                sb_word=e.sb_word,
                ch_word=e.ch_word,
                collection_id=getattr(e, "collection_id", "default"),
            )
            for e in entries
        ],
        total=len(entries),
    )


@app.post("/glossary", response_model=GlossaryEntrySchema, status_code=201)
async def create_glossary_entry(
    request: Request,
    entry: GlossaryCreateSchema,
    collection_id: str = "default",
    _: AuthCredentials = Depends(require_permission(Permission.EDIT_GLOSSARY)),
):
    """Create a new glossary entry.
    
    All four language fields (ru, en, sb, ch) are required.
    Optional collection_id (default: "default").
    Returns the created entry with its auto-generated ID.
    """
    from file_translator.domain.journal import JournalStage

    auth: AuthCredentials = request.state.auth
    ldap_groups = getattr(auth.user, "ldap_groups", None)
    svc = await translation_service.get_glossary_service()
    allowed = svc._access_resolver.is_collection_allowed(
        collection_id, ldap_groups,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Collection '{collection_id}' not accessible")

    username = getattr(auth.user, "username", "")
    try:
        result = await svc.add_entry(entry, collection_id=collection_id, created_by=username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await translation_service.journal_service.log_info(
        JournalStage.GLOSSARY,
        f"Glossary entry created: [{result.id}] ru={result.ru_word} / en={result.en_word}",
        details={"entry_id": result.id, "ru_word": result.ru_word, "en_word": result.en_word, "created_by": username},
    )
    return GlossaryEntrySchema(
        id=result.id,
        ru_word=result.ru_word,
        en_word=result.en_word,
        sb_word=result.sb_word,
        ch_word=result.ch_word,
        collection_id=collection_id,
    )


@app.get("/glossary/export")
async def export_glossary(
    request: Request,
    collection_id: str = "default",
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_GLOSSARY)),
):
    """Export glossary entries to CSV file."""
    from file_translator.domain.journal import JournalStage

    auth = request.state.auth
    svc = await translation_service.get_glossary_service()

    # Check collection access permissions
    ldap_groups = getattr(auth.user, "ldap_groups", None) if hasattr(auth, 'user') else None
    if ldap_groups and not svc._access_resolver.is_collection_allowed(collection_id, ldap_groups):
        raise HTTPException(status_code=403, detail=f"Access denied to collection: {collection_id}")

    entries = await svc.get_all_entries(collection_id)

    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ru_word", "en_word", "sb_word", "ch_word"])
    for e in entries:
        writer.writerow([getattr(e, "ru_word", ""), getattr(e, "en_word", ""), getattr(e, "sb_word", ""), getattr(e, "ch_word", "")])

    content = output.getvalue().encode("utf-8-sig")
    filename = f"glossary_{collection_id}.csv" if collection_id else "glossary.csv"

    await translation_service.journal_service.log_info(
        JournalStage.GLOSSARY,
        f"Glossary exported: {len(entries)} entries from '{collection_id}'",
        details={
            "collection_id": collection_id,
            "entry_count": len(entries),
            "exported_by": getattr(auth, "username", ""),
        },
    )

    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""},
    )


@app.post("/glossary/import", response_model=GlossaryImportResponseSchema)
async def import_glossary(
    request: Request,
    file: UploadFile = File(...),
    collection_id: str = Form("default"),
    new_collection_name: str = Form(""),
    _: AuthCredentials = Depends(require_permission(Permission.EDIT_GLOSSARY)),
):
    """Import glossary entries from a CSV file (appends to existing)."""
    from file_translator.domain.journal import JournalStage
    from file_translator.application.schemas import GlossaryCreateSchema
    from pydantic import ValidationError

    auth = request.state.auth

    # Architectural stub: new_collection_name would trigger table creation
    if new_collection_name:
        # TODO: Create new collection table (glossary_{new_collection_name})
        # Requires: CREATE TABLE LIKE, collection registration, ACL update
        pass

    svc = await translation_service.get_glossary_service()

    # Verify collection exists before importing
    if collection_id and collection_id != "default":
        collections = await svc.collection_repository.find_all()
        if not any(c.id == collection_id for c in collections):
            raise HTTPException(status_code=404, detail=f"Collection not found: {collection_id}")

    content = await file.read()

    import csv
    import io

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    decoded = decoded.lstrip("\ufeff")

    reader = csv.DictReader(io.StringIO(decoded))
    required = {"ru_word", "en_word", "sb_word", "ch_word"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise HTTPException(
            status_code=400,
            detail=f"CSV must contain columns: {', '.join(sorted(required))}. Found: {reader.fieldnames}",
        )

    svc = await translation_service.get_glossary_service()
    errors: list[str] = []

    rows_for_insert: list[tuple[int, str, str, str, str]] = []
    for row_num, row in enumerate(reader, start=2):
        ru = (row.get("ru_word") or "").strip()
        en = (row.get("en_word") or "").strip()
        sb = (row.get("sb_word") or "").strip()
        ch = (row.get("ch_word") or "").strip()
        if not (ru and en and sb and ch):
            errors.append(f"Строка {row_num}: все поля (ru_word, en_word, sb_word, ch_word) обязательны")
            continue
        rows_for_insert.append((row_num, ru, en, sb, ch))

    # Check for within-file duplicates
    seen_per_column: dict[str, dict[str, int]] = {"ru_word": {}, "en_word": {}, "sb_word": {}, "ch_word": {}}
    deduped_rows: list[tuple[int, str, str, str, str]] = []
    for row_num, ru, en, sb, ch in rows_for_insert:
        row_dup = False
        for col_name, col_value in [("ru_word", ru), ("en_word", en), ("sb_word", sb), ("ch_word", ch)]:
            prev_row = seen_per_column[col_name].get(col_value)
            if prev_row is not None:
                col_label = {"ru_word": "Русское слово", "en_word": "Английское слово", "sb_word": "Сербское слово", "ch_word": "Китайское слово"}.get(col_name, col_name)
                errors.append(
                    f"Строка {row_num}: {col_label} '{col_value}' повторяется (строка {prev_row})"
                )
                row_dup = True
                break
        if not row_dup:
            seen_per_column["ru_word"][ru] = row_num
            seen_per_column["en_word"][en] = row_num
            seen_per_column["sb_word"][sb] = row_num
            seen_per_column["ch_word"][ch] = row_num
            deduped_rows.append((row_num, ru, en, sb, ch))

    count = 0
    for row_num, ru, en, sb, ch in deduped_rows:
        try:
            entry_schema = GlossaryCreateSchema(ru_word=ru, en_word=en, sb_word=sb, ch_word=ch)
            await svc.add_entry(entry_schema, collection_id, getattr(auth, "username", ""))
            count += 1
        except ValidationError as exc:
            errors.append(f"Строка {row_num}: ошибка валидации — {exc.errors(include_context=False)}")
        except ValueError as exc:
            errors.append(f"Строка {row_num}: {exc}")
        except Exception as exc:
            logger.exception(f"Unexpected error importing row {row_num}")
            errors.append(f"Строка {row_num}: непредвиденная ошибка — {exc}")

    await translation_service.journal_service.log_info(
        JournalStage.GLOSSARY,
        f"CSV import: {count} entries into '{collection_id}' ({len(errors)} errors)",
        details={
            "collection_id": collection_id,
            "imported": count,
            "errors": errors,
            "imported_by": getattr(auth, "username", ""),
        },
    )

    return GlossaryImportResponseSchema(
        imported=count,
        collection_id=collection_id,
        errors=errors,
    )


@app.get("/glossary/{entry_id}", response_model=GlossaryEntrySchema)
async def get_glossary_entry(
    entry_id: str,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_GLOSSARY)),
):
    """Get a specific glossary entry by ID."""
    svc = await translation_service.get_glossary_service()
    result = await svc.repository.find_by_id(entry_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Glossary entry not found: {entry_id}")
    return GlossaryEntrySchema(
        id=result.id,
        ru_word=result.ru_word,
        en_word=result.en_word,
        sb_word=result.sb_word,
        ch_word=result.ch_word,
        collection_id=getattr(result, "collection_id", "default"),
    )


@app.put("/glossary/{entry_id}", response_model=GlossaryEntrySchema)
async def update_glossary_entry(
    request: Request,
    entry_id: str,
    entry: GlossaryUpdateSchema,
    collection_id: str = "default",
    _: AuthCredentials = Depends(require_permission(Permission.EDIT_GLOSSARY)),
):
    """Update an existing glossary entry.
    
    All four language fields are required (full replacement).
    """
    from file_translator.domain.journal import JournalStage

    svc = await translation_service.get_glossary_service()
    existing = await svc.repository.find_by_id(entry_id, table_name=svc._table_for(collection_id))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Glossary entry not found: {entry_id}")

    entry_data = GlossaryEntry(
        id=int(entry_id),
        ru_word=entry.ru_word,
        en_word=entry.en_word,
        sb_word=entry.sb_word,
        ch_word=entry.ch_word,
        collection_id=collection_id,
    )
    auth: AuthCredentials = request.state.auth
    username = getattr(auth.user, "username", "")
    try:
        result = await svc.update_entry(entry_data, collection_id=collection_id, updated_by=username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not result:
        raise HTTPException(status_code=404, detail=f"Glossary entry not found: {entry_id}")

    await translation_service.journal_service.log_info(
        JournalStage.GLOSSARY,
        f"Glossary entry updated: [{entry_id}] ru={entry.ru_word} / en={entry.en_word}",
        details={"entry_id": int(entry_id), "ru_word": entry.ru_word, "en_word": entry.en_word, "updated_by": username},
    )
    return GlossaryEntrySchema(
        id=result.id,
        ru_word=result.ru_word,
        en_word=result.en_word,
        sb_word=result.sb_word,
        ch_word=result.ch_word,
        collection_id=getattr(result, "collection_id", "default"),
    )


@app.delete("/glossary/{entry_id}")
async def delete_glossary_entry(
    entry_id: str,
    collection_id: str = "default",
    _: AuthCredentials = Depends(require_permission(Permission.EDIT_GLOSSARY)),
):
    """Delete a glossary entry by ID."""
    from file_translator.domain.journal import JournalStage

    svc = await translation_service.get_glossary_service()
    existing = await svc.repository.find_by_id(entry_id, table_name=svc._table_for(collection_id))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Glossary entry not found: {entry_id}")

    await svc.delete_entry(entry_id, collection_id=collection_id)
    await translation_service.journal_service.log_info(
        JournalStage.GLOSSARY,
        f"Glossary entry deleted: [{entry_id}] ru={existing.ru_word} / en={existing.en_word}",
        details={"entry_id": int(entry_id), "ru_word": existing.ru_word, "en_word": existing.en_word},
    )
    return {"detail": f"Glossary entry {entry_id} deleted"}


# --- Journal endpoints ---

@app.get("/journal/{date}", response_model=JournalResponseSchema)
async def get_journal(
    date: str,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_JOURNAL)),
):
    """Get processing journal for a specific date (YYYY-MM-DD)."""
    journal = await translation_service.journal_service.get_journal_for_date(date)
    if not journal:
        raise HTTPException(status_code=404, detail=f"Journal not found for date: {date}")
    return JournalResponseSchema(
        date=journal.date,
        entries=[
            JournalEntrySchema(
                timestamp=e.timestamp,
                level=e.level.value,
                stage=e.stage.value,
                message=e.message,
                filename=e.filename,
            )
            for e in journal.entries
        ],
        total=journal.entry_count,
    )


@app.get("/journal", response_model=JournalResponseSchema)
async def get_latest_journal(
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_JOURNAL)),
):
    """Get the most recent processing journal."""
    journals = await translation_service.journal_service.get_recent_journals(limit=1)
    if not journals:
        raise HTTPException(status_code=404, detail="No journals found")
    journal = journals[0]
    return JournalResponseSchema(
        date=journal.date,
        entries=[
            JournalEntrySchema(
                timestamp=e.timestamp,
                level=e.level.value,
                stage=e.stage.value,
                message=e.message,
                filename=e.filename,
            )
            for e in journal.entries
        ],
        total=journal.entry_count,
    )


# --- Job / async processing endpoints ---

def _job_to_schema(job) -> JobStatusSchema:
    """Convert a domain Job to JobStatusSchema."""
    return JobStatusSchema(
        job_id=job.job_id,
        user_id=job.user_id,
        status=job.status.value,
        progress=job.progress,
        current_stage=job.current_stage.value,
        total_batches=job.total_batches,
        completed_batches=job.completed_batches,
        total_text_units=job.total_text_units,
        translated_text_units=job.translated_text_units,
        eta_seconds=job.eta_seconds,
        elapsed_seconds=job.elapsed_seconds,
        error_message=job.error_message,
        output_file_path=job.output_file_path,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _check_job_owner(job: Job, auth: AuthCredentials) -> None:
    """Raise 403 if the authenticated user does not own the job and is not ADMIN.
    
    The user must either:
    - Have MANAGE_SYSTEM permission (admin), OR
    - Be the owner of the job (job.user_id == auth.user.user_id)
    """
    if auth.user.has_permission(Permission.MANAGE_SYSTEM):
        return
    if hasattr(job, "user_id") and job.user_id and job.user_id == auth.user.user_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this job",
    )


@app.get("/jobs", response_model=list[JobStatusSchema])
async def list_jobs(
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_JOBS)),
):
    """List recent translation jobs."""
    jobs = await translation_service.job_manager.get_recent_jobs(limit=50)
    return [_job_to_schema(j) for j in jobs]


@app.get("/job/{job_id}", response_model=JobStatusSchema)
async def get_job_status(
    job_id: str,
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_JOBS)),
):
    """Get job status, progress, ETA, and queue position."""
    job = await translation_service.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    _check_job_owner(job, request.state.auth)
    schema = _job_to_schema(job)
    schema.queue_position = await user_job_queue.get_position(job_id)
    return schema


@app.post("/job/{job_id}/cancel", response_model=JobStatusSchema)
async def cancel_job(
    job_id: str,
    request: Request,
    _: AuthCredentials = Depends(require_permission(Permission.CANCEL_JOBS)),
):
    """Cancel an active or queued translation job."""
    job = await translation_service.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    _check_job_owner(job, request.state.auth)
    job = await translation_service.job_manager.cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    # Clean up queue tracking if job was still pending
    await user_job_queue.cancel_pending(job_id)
    # Clean up temp dir immediately for never-started jobs
    temp_dir = job.metadata.get("temp_dir", "")
    if temp_dir and Path(temp_dir).exists():
        _cleanup_temp_dir(temp_dir, job_id)
        logger.info(f"Cleaned up temp dir for cancelled job {job_id}: {temp_dir}")
    return _job_to_schema(job)


@app.get("/job/{job_id}/download")
async def download_job_result(
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_JOBS)),
):
    """Download the result of a completed job.

    The translated file is deleted from the server after download completes.
    """
    job = await translation_service.job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    _check_job_owner(job, request.state.auth)
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"Job is not completed (status: {job.status.value})")
    output_path = Path(job.output_file_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Translated file not found on server")

    # Delete the entire temp directory after serving the file
    temp_dir = job.metadata.get("temp_dir", str(output_path.parent))
    background_tasks.add_task(_cleanup_temp_dir, temp_dir, job_id)

    logger.info(f"Serving and cleaning up job {job_id}: {temp_dir}")
    return FileResponse(
        path=output_path,
        filename=output_path.name,
        background=background_tasks,
    )


# ── Feedback / Support ──

_GLOSSARY_DB_PASSWORD = os.environ.get("GLOSSARY_DB_PASSWORD", "")
if not _GLOSSARY_DB_PASSWORD:
    raise RuntimeError(
        "GLOSSARY_DB_PASSWORD environment variable is required. "
        "Set it in .env file for MySQL glossary/feedback database access."
    )

_GLOSSARY_DB_ARGS = {
    "host": os.environ.get("GLOSSARY_DB_HOST", "dbserver"),
    "port": int(os.environ.get("GLOSSARY_DB_PORT", "3306")),
    "user": os.environ.get("GLOSSARY_DB_USER", "glossary"),
    "password": _GLOSSARY_DB_PASSWORD,
    "database": os.environ.get("GLOSSARY_DB_NAME", "glossary"),
    "cursorclass": pymysql.cursors.DictCursor,
}


async def _feedback_db(query: str, params: tuple = ()) -> list[dict]:
    def _sync() -> list[dict]:
        conn = pymysql.connect(**_GLOSSARY_DB_ARGS)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                return cur.fetchall()
        finally:
            conn.close()
    return await asyncio.to_thread(_sync)


async def _feedback_insert(query: str, params: tuple = ()) -> int:
    def _sync() -> int:
        conn = pymysql.connect(**_GLOSSARY_DB_ARGS)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                return cur.lastrowid
        finally:
            conn.close()
    return await asyncio.to_thread(_sync)


@app.post("/support/feedback", response_model=FeedbackEntrySchema, status_code=201)
async def send_feedback(
    body: FeedbackCreateSchema,
    auth: AuthCredentials = Depends(get_current_user),
):
    new_id = await _feedback_insert(
        "INSERT INTO feedback (user_id, username, message, created_at) VALUES (%s, %s, %s, NOW())",
        (auth.user.user_id, auth.user.username, body.message),
    )
    rows = await _feedback_db("SELECT * FROM feedback WHERE id = %s", (new_id,))
    row = rows[0]
    return FeedbackEntrySchema(
        id=row["id"],
        user_id=row["user_id"],
        username=row["username"],
        message=row["message"],
        created_at=row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
    )


@app.get("/support/feedback", response_model=list[FeedbackEntrySchema])
async def list_feedback(
    _: AuthCredentials = Depends(require_permission(Permission.VIEW_FEEDBACK)),
):
    rows = await _feedback_db("SELECT * FROM feedback ORDER BY created_at DESC")
    return [
        FeedbackEntrySchema(
            id=r["id"],
            user_id=r["user_id"],
            username=r["username"],
            message=r["message"],
            created_at=r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
        )
        for r in rows
    ]


# Serve static frontend
_static_dir = Path(__file__).resolve().parent.parent.parent.parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "file_translator.presentation.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
