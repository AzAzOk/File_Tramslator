"""Translation service - Core business logic orchestration."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

from file_translator.application.schemas import TranslationRequestSchema, TranslationResponseSchema
from file_translator.domain.document_model import Document
from file_translator.domain.errors import (
    DocumentOpenError,
    ModelUnavailableError,
    TranslationError,
)
from file_translator.domain.interfaces import DocumentTranslator, IUpdater, TranslationProvider
from file_translator.domain.job import Job, JobStatus, ProcessingStage as JobStage
from file_translator.domain.journal import JournalStage
from file_translator.domain.models import (
    LanguageCode,
    TextUnit,
    TranslationBatch,
    TranslationMode,
    TranslationRequest,
    TranslationStyle,
)
from file_translator.domain.validation import ValidationError, ValidationReport, ValidationSeverity
from file_translator.infrastructure.language_validator import detect_language as _detect_lang, LINGUA_AVAILABLE as _LINGUA_AVAILABLE

logger = logging.getLogger(__name__)


class TranslationService:
    """Main service orchestrating document translation.
    
    Coordinates between document translators and LLM providers
    to perform end-to-end translation workflows.
    """
    
    # Output format mapping per ТЗ 9.2-9.5:
    # Input suffix -> output suffix for translated files
    _OUTPUT_FORMAT_MAP = {
        ".dxf": ".dwg",
        ".dwg": ".dwg",
        ".pdf": ".pdf",
        ".docx": ".docx",
        ".doc": ".docx",
        ".xlsx": ".xlsx",
        ".xls": ".xlsx",
    }
    
    @staticmethod
    def _resolve_output_suffix(input_suffix: str) -> str:
        """Return output file suffix per ТЗ 9.2-9.5.
        
        DXF -> .dxf
        DWG -> .dwg
        PDF -> .pdf
        DOCX/DOC -> .docx
        XLSX/XLS -> .xlsx
        Unknown -> preserve input suffix
        """
        return TranslationService._OUTPUT_FORMAT_MAP.get(input_suffix, input_suffix)
    
    def __init__(self, translator_factory: Any = None, provider: TranslationProvider | None = None,
                 glossary_service: Any = None, journal_service: Any = None,
                 job_manager: Any = None, validation_chain: Any = None):
        self._translator_factory = translator_factory or self._default_translator_factory
        self._format_registry = None  # lazy init
        self._provider = provider
        self._glossary_service = glossary_service
        self._journal_service = journal_service
        self._job_manager = job_manager
        self._validation_chain = validation_chain
        self._init_lock = threading.Lock()
        self._async_init_lock = asyncio.Lock()
    
    async def get_provider(self) -> TranslationProvider:
        """Get the translation provider instance (lazy init)."""
        if self._provider is not None:
            return self._provider
        async with self._async_init_lock:
            if self._provider is None:
                from file_translator.infrastructure.config import LLMConfig
                from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider
                self._provider = OpenAITranslationProvider(LLMConfig.from_env())
        return self._provider
    
    async def get_glossary_service(self):
        """Get the glossary service instance (lazy init)."""
        if self._glossary_service is not None:
            return self._glossary_service
        async with self._async_init_lock:
            if self._glossary_service is None:
                from file_translator.application.glossary_service import GlossaryService
                from file_translator.infrastructure.auth.glossary_access_resolver import GlossaryAccessResolver
                from file_translator.infrastructure.repositories.glossary_collection_repository import (
                    InMemoryGlossaryCollectionRepository,
                )
                from file_translator.infrastructure.repositories.mysql_glossary_repository import MySQLGlossaryRepository
                mysql_repo = MySQLGlossaryRepository()
                collection_repo = InMemoryGlossaryCollectionRepository(mysql_repo)
                access_resolver = GlossaryAccessResolver()
                self._glossary_service = GlossaryService(
                    repository=mysql_repo,
                    collection_repository=collection_repo,
                    access_resolver=access_resolver,
                )
        return self._glossary_service
    
    @property
    def journal_service(self) -> Any:
        """Get the journal service instance (lazy init)."""
        if self._journal_service is not None:
            return self._journal_service
        with self._init_lock:
            if self._journal_service is None:
                from file_translator.application.journal_service import JournalService
                from file_translator.infrastructure.repositories.file_journal_repository import FileJournalRepository
                self._journal_service = JournalService(repository=FileJournalRepository())
        return self._journal_service
    
    @property
    def job_manager(self) -> Any:
        """Get the job manager instance (lazy init)."""
        if self._job_manager is not None:
            return self._job_manager
        with self._init_lock:
            if self._job_manager is None:
                from file_translator.application.job_manager import JobManager
                from file_translator.infrastructure.repositories.redis_job_repository import RedisJobRepository
                self._job_manager = JobManager(repository=RedisJobRepository())
        return self._job_manager
    
    @property
    def validation_chain(self) -> Any:
        """Get the validation chain instance (lazy init)."""
        if self._validation_chain is not None:
            return self._validation_chain
        with self._init_lock:
            if self._validation_chain is None:
                from file_translator.application.validators import (
                    FileAccessValidator, FileSizeValidator,
                    FileStructureValidator, LanguageMismatchValidator, ValidationChain,
                )
                chain = ValidationChain()
                chain.add_validator(FileSizeValidator())
                chain.add_validator(FileAccessValidator())
                chain.add_validator(FileStructureValidator())
                chain.add_validator(LanguageMismatchValidator())
                self._validation_chain = chain
        return self._validation_chain

    @property
    def format_registry(self):
        """Get the FormatRegistry instance (lazy init)."""
        if self._format_registry is not None:
            return self._format_registry
        with self._init_lock:
            if self._format_registry is None:
                from file_translator.infrastructure.document.format_registry import FormatRegistry
                from file_translator.infrastructure.parsers.dxf_parser import DxfDocumentParser
                from file_translator.infrastructure.updaters.dxf_updater import DxfUpdater
                registry = FormatRegistry()
                registry.register(".dxf", parser=DxfDocumentParser, updater=DxfUpdater)
                registry.register(".dwg", parser=DxfDocumentParser, updater=DxfUpdater)
                self._format_registry = registry
        return self._format_registry

    async def translate_document(self, file_path: str, request: TranslationRequestSchema,
                                  job_id: str | None = None) -> TranslationResponseSchema:
        """Translate a document from source to target language.
        
        Args:
            file_path: Path to the input document.
            request: Translation request parameters.
            job_id: Optional job ID for progress tracking. Created if not provided.
            
        Returns:
            Translation response with results and metadata.
        """
        start_time = time.time()
        output_file = ""
        filename = Path(file_path).name
        
        logger.info(f"Starting translation: {file_path}")
        logger.info(f"Source: {request.source_language} -> Target: {request.target_language}")
        logger.info(f"Translation style: {request.translation_style}")
        
        # Create/init job for progress tracking
        if not job_id:
            job = await self.job_manager.create_job(
                filename=filename,
                source_language=request.source_language,
                target_language=request.target_language,
                translation_style=request.translation_style,
            )
            job_id = job.job_id
        job = await self.job_manager.start_job(job_id)
        await self.job_manager.update_progress(job_id, JobStage.RECEIVED)
        
        await self.journal_service.log_info(
            JournalStage.RECEIVED,
            f"Translation request received: {request.source_language} -> {request.target_language}",
            filename=filename,
            details={"source": request.source_language, "target": request.target_language,
                     "style": request.translation_style, "use_glossary": request.use_glossary},
        )
        
        try:
            # Convert language codes
            source_lang = LanguageCode.from_string(request.source_language)
            target_lang = LanguageCode.from_string(request.target_language)
            translation_style = TranslationStyle(request.translation_style)
            translation_mode = TranslationMode(request.translation_mode)
            
            # Step 1: Open and validate document
            input_path = Path(file_path)
            if not input_path.exists():
                raise DocumentOpenError(file_path=str(input_path), reason="File not found")
            
            # Validate the input file
            await self.job_manager.update_progress(job_id, JobStage.VALIDATION)
            await self.journal_service.log_info(
                JournalStage.EXTRACTION, f"Validating input file", filename=filename,
            )
            validation_context = {
                "source_language": request.source_language,
                "target_language": request.target_language,
                "filename": filename,
            }
            report = await self.validation_chain.validate_all(input_path, validation_context)
            if not report.passed:
                errors_str = "; ".join(report.error_messages)
                logger.warning(f"File validation failed: {errors_str}")
                await self.journal_service.log_error(
                    JournalStage.EXTRACTION, f"Validation failed: {errors_str}", filename=filename,
                )
                await self.job_manager.fail_job(job_id, error_message=errors_str)
                raise ValidationError(report)
            
            if report.warnings:
                logger.warning(f"Validation warnings: {'; '.join(report.warning_messages)}")
            
            # Step 2: Find appropriate translator
            translator = self._find_translator(input_path)
            if not translator:
                raise ValueError(f"No suitable translator found for format: {input_path.suffix}")
            
            logger.info(f"Using translator: {translator.__class__.__name__}")
            
            await self.journal_service.log_info(
                JournalStage.EXTRACTION, f"Extracting text using {translator.__class__.__name__}",
                filename=filename,
            )
            
            # Step 3: Extract text units from document
            await self.job_manager.update_progress(job_id, JobStage.EXTRACTION)
            
            # Check cancellation before extraction
            if await self.job_manager.is_cancelled(job_id):
                logger.warning(f"Job {job_id} cancelled during extraction phase start")
                return TranslationResponseSchema(
                    success=False,
                    errors=["Operation cancelled by user"],
                    duration_seconds=time.time() - start_time,
                )
            
            extracted_data = translator.extract(
                input_path,
                source_lang=source_lang.value,
                target_lang=target_lang.value,
            )
            
            text_units = extracted_data.get("text_units", [])
            
            if not text_units:
                logger.warning("No translatable text found in document")
                await self.journal_service.log_info(
                    JournalStage.EXTRACTION, "No translatable text found", filename=filename,
                )
                return TranslationResponseSchema(
                    success=True,
                    text_units_translated=0,
                    total_text_units=0,
                    duration_seconds=time.time() - start_time,
                )
            
            # Filter only translatable units
            translatable_units = [u for u in text_units if hasattr(u, 'original_text') and u.original_text.strip()]
            
            logger.info(f"Found {len(translatable_units)} translatable text units")
            await self.journal_service.log_info(
                JournalStage.EXTRACTION, f"Extracted {len(translatable_units)} text units",
                filename=filename,
                details={"total_units": len(translatable_units)},
            )
            
            # Update job progress
            await self.job_manager.update_progress(
                job_id, JobStage.EXTRACTION,
                total_text_units=len(translatable_units),
            )
            
            # Check cancellation after extraction before glossary processing
            if await self.job_manager.is_cancelled(job_id):
                logger.warning(f"Job {job_id} cancelled after extraction")
                return TranslationResponseSchema(
                    success=False,
                    errors=["Operation cancelled by user"],
                    duration_seconds=time.time() - start_time,
                )
            
            # Step 4: Load glossary entries (passed as hints to LLM, not substituted into text)
            glossary_entries = []
            if request.use_glossary:
                await self.job_manager.update_progress(job_id, JobStage.GLOSSARY)
                try:
                    glossary_service = await self.get_glossary_service()
                    glossary_id = request.collection_id or ""
                    glossary = await glossary_service.load_glossary(glossary_id=glossary_id)
                    glossary_entries = glossary.entries
                    logger.info(f"Glossary enabled: {len(glossary_entries)} entries loaded (passed as LLM hints)")
                    await self.journal_service.log_info(
                        JournalStage.GLOSSARY, f"Loaded glossary: {len(glossary_entries)} entries",
                        filename=filename,
                        details={"entries_loaded": len(glossary_entries)},
                    )
                except Exception as e:
                    logger.warning(f"Glossary loading failed (continuing without glossary): {e}")
                    await self.journal_service.log_warning(
                        JournalStage.GLOSSARY, f"Glossary loading failed: {e}",
                        filename=filename,
                    )
            
            # Step 5: Filter units by source language (FILTER_BY_SOURCE mode)
            if translation_mode == TranslationMode.FILTER_BY_SOURCE:
                skipped_ids: set[str] = set()
                filtered: list[TextUnit] = []
                for u in translatable_units:
                    detected = _detect_lang(u.original_text)
                    if detected is None or detected == source_lang.value:
                        filtered.append(u)
                    else:
                        skipped_ids.add(u.id)
                if skipped_ids:
                    logger.info(
                        f"FILTER_BY_SOURCE: server-side filter skipped "
                        f"{len(skipped_ids)}/{len(translatable_units)} units "
                        f"(detected language ≠ {source_lang.value})"
                    )
                translatable_units = filtered

            # Step 6: Split into batches
            batch_size = request.batch_size
            batches = self._create_batches(translatable_units, batch_size, source_lang, target_lang, translation_style, translation_mode, request.use_glossary)
            
            logger.info(f"Created {len(batches)} translation batches (size={batch_size})")
            await self.journal_service.log_info(
                JournalStage.TRANSLATION, f"Split into {len(batches)} batches (size={batch_size})",
                filename=filename,
                details={"batch_count": len(batches), "batch_size": batch_size},
            )
            
            # Step 7: Translate each batch
            all_translations: dict[str, str] = {}
            total_batches = len(batches)
            
            for i, batch in enumerate(batches):
                try:
                    # Check if job was cancelled
                    if await self.job_manager.is_cancelled(job_id):
                        logger.warning(f"Job {job_id} was cancelled, stopping processing")
                        return TranslationResponseSchema(
                            success=False,
                            errors=["Operation cancelled by user"],
                            duration_seconds=time.time() - start_time,
                        )
                    
                    logger.info(f"Translating batch {i + 1}/{total_batches} ({len(batch.text_units)} units)")
                    
                    # Prepare batch data for provider
                    batch_data = {
                        "batch": batch,
                        "source_language": source_lang,
                        "target_language": target_lang,
                        "translation_style": translation_style,
                        "translation_mode": translation_mode,
                        "use_glossary": request.use_glossary,
                        "glossary_entries": glossary_entries,
                    }
                    
                    # Call LLM provider
                    provider = await self.get_provider()
                    translations = await provider.translate_batch(batch_data)
                    
                    # Store results
                    for translation in translations:
                        all_translations[translation["id"]] = translation["text"]
                    
                    # Update job progress for this batch
                    await self.job_manager.update_progress(
                        job_id, JobStage.TRANSLATION,
                        batch_index=i + 1,
                        total_batches=total_batches,
                        translated_text_units=len(all_translations),
                    )
                    
                    await self.journal_service.log_info(
                        JournalStage.TRANSLATION,
                        f"Batch {i + 1}/{total_batches} translated ({len(translations)} units)",
                        filename=filename,
                    )
                         
                except (ModelUnavailableError, TranslationError) as e:
                    logger.error(f"Batch {i + 1} translation failed: {e}")
                    await self.journal_service.log_error(
                        JournalStage.TRANSLATION,
                        f"Batch {i + 1}/{total_batches} failed: {e}",
                        filename=filename,
                    )
                    raise
                
                # Small delay between batches to avoid overwhelming the model
                if i < len(batches) - 1:
                    await asyncio.sleep(0.5)
            

            # Verify translation completeness
            expected_units = len(translatable_units)
            translated_count = len(all_translations)
            if translated_count < expected_units:
                missed_units = expected_units - translated_count
                logger.warning(
                    f"Translation incomplete: {translated_count}/{expected_units} units "
                    f"translated ({missed_units} units missed, possibly skipped by LLM)"
                )
                await self.journal_service.log_warning(
                    JournalStage.TRANSLATION,
                    f"Incomplete translation: {translated_count}/{expected_units} units (LLM may have skipped some)",
                    filename=filename,
                    details={"translatable": expected_units, "actual": translated_count},
                )
            else:
                logger.info(f"Translation complete: all {translated_count}/{expected_units} units translated")
            
            # Check cancellation before applying translations back to document
            if await self.job_manager.is_cancelled(job_id):
                logger.warning(f"Job {job_id} cancelled after translation, before applying")
                return TranslationResponseSchema(
                    success=False,
                    errors=["Operation cancelled by user"],
                    duration_seconds=time.time() - start_time,
                )
            
            # Step 8: Apply translations back to document
            logger.info(f"Applying {len(all_translations)} translations")
            translated_data = translator.translate(
                extracted_data, all_translations,
            )
            
            # Step 9: Save the translated document
            output_suffix = self._resolve_output_suffix(input_path.suffix.lower())
            output_path = input_path.parent / f"{input_path.stem}_translated{output_suffix}"
            
            try:
                saved_path = translator.save(translated_data, output_path)
                output_file = str(saved_path)

                await self.job_manager.update_progress(job_id, JobStage.SAVE)
                
                await self.journal_service.log_info(
                    JournalStage.SAVE,
                    f"Document saved: {Path(output_file).name}",
                    filename=filename,
                    details={"output_file": output_file},
                )
            except Exception as save_error:
                logger.error(f"Failed to save translated document: {save_error}")
                await self.journal_service.log_error(
                    JournalStage.SAVE,
                    f"Failed to save document: {save_error}",
                    filename=filename,
                    details={"error": str(save_error)},
                )
                if job_id:
                    await self.job_manager.fail_job(job_id, f"Save failed: {str(save_error)}")
                return TranslationResponseSchema(
                    success=False,
                    errors=[f"Failed to save translated document: {str(save_error)}"],
                    duration_seconds=time.time() - start_time,
                )
            
            duration = time.time() - start_time
            
            await self.journal_service.log_info(
                JournalStage.COMPLETED,
                f"Translation completed: {len(all_translations)} units in {duration:.2f}s",
                filename=filename,
                details={"units_translated": len(all_translations), "duration_s": round(duration, 2)},
            )
            
            await self.job_manager.complete_job(job_id, output_file_path=output_file)
            
            # Cleanup old journals after successful translation
            await self.journal_service.cleanup_old_journals()
            
            return TranslationResponseSchema(
                success=True,
                text_units_translated=len(all_translations),
                total_text_units=len(translatable_units),
                duration_seconds=round(duration, 3),
                output_file_path=output_file,
                glossary_applied=len(glossary_entries),
                job_id=job_id,
            )
            
        except (DocumentOpenError, ValueError, ValidationError) as e:
            logger.error(f"Translation failed - initialization error: {e}")
            await self.journal_service.log_error(
                JournalStage.FAILED,
                f"Translation failed: {e}",
                filename=filename,
                details={"error": str(e)},
            )
            if job_id:
                await self.job_manager.fail_job(job_id, str(e))
            errors = []
            if isinstance(e, ValidationError):
                errors = e.report.error_messages
            return TranslationResponseSchema(
                success=False,
                errors=errors or [str(e)],
                duration_seconds=time.time() - start_time,
            )
        
        except Exception as e:
            logger.error(f"Translation failed unexpectedly: {e}", exc_info=True)
            await self.journal_service.log_error(
                JournalStage.FAILED,
                f"Unexpected error: {e}",
                filename=filename,
                details={"error": str(e)},
            )
            if job_id:
                await self.job_manager.fail_job(job_id, str(e))
            return TranslationResponseSchema(
                success=False,
                errors=[f"Unexpected error: {str(e)}"],
                duration_seconds=time.time() - start_time,
            )
    
    def _find_translator(self, file_path: Path) -> DocumentTranslator | None:
        """Find appropriate translator for the given file."""
        # Try the new FormatRegistry first (DXF, DWG, etc.)
        registry = self.format_registry
        if registry.can_process(file_path):
            # For FormatRegistry-based formats, return a legacy-compatible wrapper
            from file_translator.infrastructure.translators.dxf_translator import DxfTranslator
            return DxfTranslator()

        # Fallback to existing translators for DOCX/XLSX
        from file_translator.infrastructure.translators.docx_translator import DocxTranslator
        from file_translator.infrastructure.translators.xlsx_translator import XlsxTranslator
        
        translators = [DocxTranslator(), XlsxTranslator()]
        for translator in translators:
            if translator.can_process(file_path):
                return translator
        
        return None
    
    def _create_batches(self, text_units: list[TextUnit], batch_size: int,
                        source_language: LanguageCode, target_language: LanguageCode,
                        translation_style: TranslationStyle = TranslationStyle.TECHNICAL,
                        translation_mode: TranslationMode = TranslationMode.FULL,
                        use_glossary: bool = False) -> list[TranslationBatch]:
        """Split text units into batches."""
        batches = []
        
        for i in range(0, len(text_units), batch_size):
            batch_text_units = text_units[i:i + batch_size]
            
            if not batch_text_units:
                continue
            
            batch = TranslationBatch(
                sequence_id=len(batches) + 1,
                text_units=batch_text_units,
                source_language=source_language,
                target_language=target_language,
                translation_style=translation_style,
                translation_mode=translation_mode,
            )
            
            batches.append(batch)
        
        return batches
    
    def _default_translator_factory(self):
        """Default translator factory."""
        from file_translator.infrastructure.translators.docx_translator import DocxTranslator
        from file_translator.infrastructure.translators.xlsx_translator import XlsxTranslator
        return [DocxTranslator(), XlsxTranslator()]
