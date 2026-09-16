"""PDF→DOCX converter client — talks to the first-pdf-converter HTTP service.

The converter service runs on the `fpc-converter` VM (First PDF 6.4 driven
through UI Automation). This client:

- POSTs a PDF to ``POST /convert`` with an ``X-Timeout-Seconds`` header so
  the service sizes its watchdog to the expected conversion length,
- streams the DOCX response to ``out_dir / <stem>.docx``,
- maps converter structured errors (``CONVERT_TIMEOUT``, ``CONVERT_FAILED``,
  400/502/504, connection refused) to :class:`ConversionError` /
  :class:`ConversionTimeoutError` with Russian-language messages.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

from file_translator.domain.errors import (
    ConversionError,
    ConversionTimeoutError,
)

logger = logging.getLogger(__name__)

DEFAULT_CONVERTER_URL = "http://172.17.106.164:8001"
DEFAULT_MAX_TIMEOUT = 3600.0  # seconds; hard cap for any single conversion

# Smart timeout model (spike-proven: 23 MB PDF → ~85 s)
FLOOR_SECONDS = 30.0
SECONDS_PER_MB = 3.7
SLACK_MULTIPLIER = 2.0
NETWORK_MARGIN_SECONDS = 30.0

_TIMEOUT_HEADER = "X-Timeout-Seconds"

# Converter error code → user-facing Russian message
_ERROR_MESSAGES = {
    "CONVERT_TIMEOUT": "Превышено время конвертации PDF",
    "CONVERT_FAILED": "Ошибка конвертации PDF в DOCX",
    "BAD_REQUEST": "Некорректный запрос конвертации PDF",
    "INTERNAL": "Ошибка конвертации PDF в DOCX",
}


def _max_timeout_from_env() -> float:
    raw = os.environ.get("FIRST_PDF_CONVERTER_MAX_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("Invalid FIRST_PDF_CONVERTER_MAX_TIMEOUT=%r, using default", raw)
    return DEFAULT_MAX_TIMEOUT


class PdfToDocxConverter:
    """HTTP client for the First PDF conversion service."""

    def __init__(self, base_url: str | None = None,
                 timeout: float | None = None,
                 max_timeout: float | None = None):
        """Configure from explicit args or env.

        Args:
            base_url: Converter service base URL. Defaults to
                ``FIRST_PDF_CONVERTER_URL`` env or the deployed VM URL.
            timeout: Optional manual override for the conversion deadline.
                When set, the size-based estimate is disabled. Defaults to
                ``FIRST_PDF_CONVERTER_TIMEOUT`` env.
            max_timeout: Hard cap on any computed deadline. Defaults to
                ``FIRST_PDF_CONVERTER_MAX_TIMEOUT`` env or 3600 s.
        """
        self.base_url = (base_url or os.environ.get("FIRST_PDF_CONVERTER_URL")
                         or DEFAULT_CONVERTER_URL).rstrip("/")
        self.timeout = (timeout if timeout is not None
                        else self._float_env("FIRST_PDF_CONVERTER_TIMEOUT"))
        self.max_timeout = (max_timeout if max_timeout is not None
                            else _max_timeout_from_env())

    @staticmethod
    def _float_env(name: str) -> float | None:
        raw = os.environ.get(name)
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            logger.warning("Invalid %s=%r, ignoring", name, raw)
            return None

    @staticmethod
    def estimate_deadline(file_size: int, max_timeout: float | None = None) -> float:
        """Estimate a conversion deadline from the input size.

        Linear model from the spike (23 MB → ~85 s), floored, with 2× slack
        and a hard cap so a genuinely hung conversion still finishes.

        Args:
            file_size: Size of the PDF in bytes.
            max_timeout: Hard cap in seconds. Defaults to the env/default cap.

        Returns:
            Deadline in seconds.
        """
        cap = max_timeout if max_timeout is not None else _max_timeout_from_env()
        estimate = FLOOR_SECONDS + (file_size / (1024 * 1024)) * SECONDS_PER_MB
        deadline = max(FLOOR_SECONDS, estimate * SLACK_MULTIPLIER)
        return min(cap, deadline)

    def resolve_deadline(self, file_size: int) -> float:
        """Return the effective deadline for a conversion of ``file_size`` bytes."""
        if self.timeout is not None:
            # Manual override disables the size-based model.
            return min(self.timeout, self.max_timeout)
        return self.estimate_deadline(file_size, self.max_timeout)

    async def convert(self, pdf_path: Path, out_dir: Path) -> Path:
        """Convert ``pdf_path`` to DOCX via the converter service.

        Thin async wrapper around :meth:`convert_sync` — the blocking HTTP
        upload/download runs in a worker thread so the event loop stays free.

        Args:
            pdf_path: Path to the uploaded PDF.
            out_dir: Directory to write the converted DOCX into.

        Returns:
            Path to the converted DOCX file.

        Raises:
            ConversionError: On any converter failure.
            ConversionTimeoutError: On CONVERT_TIMEOUT.
        """
        return await asyncio.to_thread(self.convert_sync, pdf_path, out_dir)

    def convert_sync(self, pdf_path: Path, out_dir: Path) -> Path:
        """Blocking conversion — used by the (synchronous) translator pipeline.

        The ``DocumentTranslator.extract()`` interface is synchronous and runs
        inside an already-running event loop, so ``asyncio.run()`` is not
        available here. ``httpx.Client`` blocks; callers (tests, CLI) use this
        directly, async callers use :meth:`convert`.
        """
        if not pdf_path.exists():
            raise ConversionError("Файл PDF не найден", status_code=0, error_code="FILE_NOT_FOUND")

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{pdf_path.stem}.docx"
        deadline = self.resolve_deadline(pdf_path.stat().st_size)

        headers = {_TIMEOUT_HEADER: str(int(deadline))}
        timeout = httpx.Timeout(deadline + NETWORK_MARGIN_SECONDS, connect=30.0)
        logger.info(
            "Converting %s via %s (deadline=%.0fs)", pdf_path.name, self.base_url, deadline
        )

        try:
            with httpx.Client(timeout=timeout) as client:
                with pdf_path.open("rb") as fh:
                    response = client.post(
                        self.base_url + "/convert",
                        files={"file": (pdf_path.name, fh, "application/pdf")},
                        headers=headers,
                    )

                if response.status_code != 200:
                    raise self._map_error(response)

                response.read()
                with out_path.open("wb") as out:
                    out.write(response.content)
        except httpx.HTTPError as exc:
            raise ConversionError(
                "Сервис конвертации недоступен",
                status_code=0,
                error_code="CONVERTER_UNREACHABLE",
                context={"detail": str(exc)},
            ) from exc

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise ConversionError(
                "Ошибка конвертации PDF в DOCX: пустой результат",
                status_code=502,
                error_code="CONVERT_EMPTY",
            )

        logger.info("Converted %s → %s (%d bytes)", pdf_path.name, out_path.name, out_path.stat().st_size)
        return out_path

    def _map_error(self, response: httpx.Response) -> ConversionError:
        """Raise a ConversionError from a non-2xx converter response.

        The converter returns structured JSON errors: ``{"code": ..., "message": ...}``.
        """
        code = ""
        detail = ""
        try:
            body = response.json()
            code = str(body.get("code", ""))
            detail = str(body.get("message", ""))
        except Exception:
            body_text = response.text[:300]
            detail = body_text if body_text else f"HTTP {response.status_code}"

        message = _ERROR_MESSAGES.get(code)
        if not message:
            message = f"Ошибка конвертации PDF в DOCX: {detail}"

        if code == "CONVERT_TIMEOUT":
            return ConversionTimeoutError(
                message, status_code=response.status_code, error_code=code,
                context={"detail": detail},
            )

        return ConversionError(
            message, status_code=response.status_code, error_code=code,
            context={"detail": detail},
        )