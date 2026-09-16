"""PDF document translator — converts PDF→DOCX, then delegates to Okapi.

The PDF pipeline is a thin orchestration around the ``first-pdf-converter``
HTTP service (First PDF 6.4 driven via UI Automation on the ``fpc-converter``
VM) and the existing Okapi-based ``DocxTranslator``:

  extract()  → PDF→DOCX via PdfToDocxConverter → DocxTranslator.extract()
  translate() → DocxTranslator.translate()  (XLIFF <target> update)
  save()     → DocxTranslator.save()        (Tikal merge → DOCX)

Composition (not inheritance) keeps ``PdfTranslator`` tiny and avoids the
base ``extract()`` lifecycle issue: ``DocxTranslator.extract()`` calls
``self._cleanup()`` and recreates its temp dir, which would delete a
just-converted DOCX if we subclassed.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from file_translator.domain.errors import ConversionError, DocumentOpenError
from file_translator.domain.interfaces import DocumentTranslator
from file_translator.domain.models import DocumentFormat
from file_translator.infrastructure.converters.pdf_to_docx_converter import (
    PdfToDocxConverter,
)
from file_translator.infrastructure.translators.docx_translator import DocxTranslator

logger = logging.getLogger(__name__)


class PdfTranslator(DocumentTranslator):
    """Translator implementation for PDF documents.

    Converts the PDF to DOCX through the First PDF converter service, then
    reuses the Okapi DOCX pipeline (extract/translate/save) so headers,
    footers, tables, text boxes, CJK font substitution and table-height
    post-processing all apply to the converted document.
    """

    SUPPORTED_FORMATS = {DocumentFormat.PDF}

    def __init__(self, converter: PdfToDocxConverter | None = None,
                 docx_translator: DocxTranslator | None = None):
        self._converter = converter or PdfToDocxConverter()
        self._docx = docx_translator or DocxTranslator()
        self._temp_dir: Path | None = None
        self._converted_path: Path | None = None

    @classmethod
    def supported_formats(cls) -> set[DocumentFormat]:
        return cls.SUPPORTED_FORMATS.copy()

    def can_process(self, file_path: Path) -> bool:
        return file_path.exists() and file_path.suffix.lower() == ".pdf"

    def extract(self, file_path: Path,
                source_lang: str = "en",
                target_lang: str = "ru") -> dict:
        """Extract text units from a PDF.

        1. Convert the PDF to DOCX via the First PDF converter service into
           this translator's temp dir (``pdf_convert_*``).
        2. Delegate the DOCX half to ``DocxTranslator.extract()`` (Tikal →
           XLIFF → TextUnit list).
        3. Return the dict unchanged, plus ``pdf_convert_dir`` so the job
           metadata records this temp dir for the orphan sweep.

        Raises:
            DocumentOpenError: If the PDF cannot be converted by the service.
        """
        logger.info(f"Converting PDF via First PDF service: {file_path}")

        self._cleanup()
        self._temp_dir = Path(tempfile.mkdtemp(prefix="pdf_convert_"))

        try:
            converted = self._converter.convert_sync(file_path, self._temp_dir)
        except ConversionError as e:
            logger.error(f"PDF conversion failed: {e}")
            self._cleanup()
            raise DocumentOpenError(
                file_path=str(file_path), reason=str(e),
            ) from e

        self._converted_path = converted
        logger.info(f"PDF converted to DOCX: {converted}")

        extracted = self._docx.extract(
            converted,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        # Record the conversion temp dir so the periodic orphan sweep knows
        # this job owns it (the docx translator's own temp dir already flows
        # through extracted["temp_dir"]).
        extracted["pdf_convert_dir"] = str(self._temp_dir)
        extracted["converted_docx"] = str(converted)
        return extracted

    def translate(self, extracted_data: dict, translations: dict[str, str],
                  supports_tags: bool = False) -> dict:
        """Apply translations to the XLIFF — delegate to DocxTranslator."""
        return self._docx.translate(extracted_data, translations, supports_tags)

    def save(self, translated_data: dict, output_path: Path) -> Path:
        """Merge translated XLIFF back to DOCX — delegate to DocxTranslator."""
        try:
            return self._docx.save(translated_data, output_path)
        finally:
            self._cleanup()

    def _cleanup(self):
        """Clean up the PDF conversion temp dir. Never raises."""
        try:
            if self._temp_dir and self._temp_dir.exists():
                shutil.rmtree(str(self._temp_dir))
                logger.debug(f"Cleaned up PDF conversion dir: {self._temp_dir}")
        except Exception as e:
            logger.warning(f"PDF conversion cleanup failed: {e}")
        self._temp_dir = None
        self._converted_path = None