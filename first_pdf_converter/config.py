"""Configuration for the First PDF conversion service (env-driven, design D5)."""

from __future__ import annotations

import os
import tempfile

from .paths import ensure_long_path

DEFAULT_EXE_PATH = r"C:\Program Files (x86)\First PDF 6.4\First PDF.exe"

EXE_PATH = os.environ.get("FIRST_PDF_EXE") or DEFAULT_EXE_PATH
PORT = int(os.environ.get("FIRST_PDF_PORT", "8001"))
# First PDF rejects 8.3 paths in its custom-output textbox; always expand.
WORK_DIR = ensure_long_path(
    os.environ.get("FIRST_PDF_WORK_DIR")
    or os.path.join(tempfile.gettempdir(), "first_pdf_work")
)
TIMEOUT_SECONDS = float(os.environ.get("FIRST_PDF_TIMEOUT", "120"))
# Hard cap for any single conversion, no matter what the client requests.
MAX_TIMEOUT = float(os.environ.get("FIRST_PDF_MAX_TIMEOUT", "3600"))


def resolve_timeout(client_deadline_seconds: str | None = None) -> float:
    """Effective watchdog timeout for a request.

    Effective timeout = max(server default, min(client deadline, MAX_TIMEOUT)).

    The client (File Translator's PdfToDocxConverter) sends an
    ``X-Timeout-Seconds`` header computed from the PDF size (spike model with
    2× slack). The server never goes below its own default, and never beyond
    the hard cap — so a genuinely hung First PDF process still gets killed,
    while big PDFs get a watchdog sized to the expected conversion length.
    """
    default = TIMEOUT_SECONDS
    if not client_deadline_seconds:
        return default
    try:
        client_deadline = float(client_deadline_seconds)
    except (TypeError, ValueError):
        return default
    return max(default, min(client_deadline, MAX_TIMEOUT))