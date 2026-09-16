"""FastAPI service that converts PDF files to DOCX through First PDF automation.

Run locally on the Windows host:

    python -m uvicorn first_pdf_converter.server:app --port 8001

Convert:

    curl -F "file=@input.pdf" http://127.0.0.1:8001/convert -o output.docx
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTasks

from . import config
from .automation import (
    ConverterFailed,
    ConverterStartError,
    ConvertTimeout,
    convert_pdf,
)
from .paths import ensure_long_path

app = FastAPI(title="First PDF Converter", version="0.1.0")

DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
MAX_UPLOAD_BYTES = 128 * 1024 * 1024  # same cap as the File Translator app

_convert_lock = asyncio.Lock()
_work_root = Path(config.WORK_DIR)
_work_root.mkdir(parents=True, exist_ok=True)
# Re-resolve after creation so no per-request path contains 8.3 names.
_work_root = Path(ensure_long_path(str(_work_root)))


def _error(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _json_error(status_code: int, code: str, message: str, request_dir: Path):
    shutil.rmtree(request_dir, ignore_errors=True)
    return JSONResponse(status_code=status_code, content=_error(code, message))


def _cleanup(request_dir: Path) -> None:
    """Remove a request temp dir, retrying briefly (the FileResponse handle may
    still be closing on Windows). Best effort."""
    for _ in range(5):
        try:
            shutil.rmtree(request_dir)
            return
        except OSError:
            time.sleep(0.3)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc):
    # A missing/invalid multipart `file` part surfaces as a FastAPI 422;
    # the spec requires a 400 for "no file uploaded".
    return JSONResponse(
        status_code=400, content=_error("BAD_REQUEST", "Invalid request: a PDF file upload is required")
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/convert")
async def convert(
    file: UploadFile | None = File(default=None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    timeout_header: str | None = Header(default=None, alias="X-Timeout-Seconds"),
):
    if file is None or not file.filename:
        raise HTTPException(
            status_code=400, detail=_error("BAD_REQUEST", "No file uploaded")
        )
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=_error("BAD_REQUEST", f"Not a PDF file: {file.filename}"),
        )

    # Per-request watchdog: the File Translator client sizes a conversion
    # based on the PDF size; effective = max(default, min(client, MAX)).
    effective_timeout = config.resolve_timeout(timeout_header)

    request_id = uuid.uuid4().hex
    request_dir = _work_root / request_id
    src = request_dir / file.filename
    out_dir = request_dir / "out"
    try:
        request_dir.mkdir(parents=True)
        out_dir.mkdir()
        with src.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        if src.stat().st_size == 0:
            raise HTTPException(
                status_code=400, detail=_error("BAD_REQUEST", "Uploaded PDF is empty")
            )
    except HTTPException:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=_error("INTERNAL", f"Failed to store upload: {exc}"),
        ) from exc

    result_name = Path(file.filename).stem + ".docx"

    async with _convert_lock:
        try:
            out_path = await asyncio.get_running_loop().run_in_executor(
                None,
                convert_pdf,
                str(src),
                str(out_dir),
                config.EXE_PATH,
                effective_timeout,
            )
        except ConvertTimeout as exc:
            return _json_error(504, "CONVERT_TIMEOUT", str(exc), request_dir)
        except (ConverterStartError, ConverterFailed) as exc:
            return _json_error(502, "CONVERT_FAILED", str(exc), request_dir)
        except Exception as exc:  # pragma: no cover - safety net
            return _json_error(
                500, "INTERNAL", f"Unexpected conversion error: {exc}", request_dir
            )

    background_tasks.add_task(_cleanup, request_dir)
    return FileResponse(out_path, media_type=DOCX_MEDIA, filename=result_name)