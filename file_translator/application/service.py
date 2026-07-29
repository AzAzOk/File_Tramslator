"""Translation service - Core business logic orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

# Diagnostic: MTEXT AutoCAD format codes pattern for Task A hypothesis testing
_MTEXT_PATTERN = re.compile(r'\\[A-Za-z]|%%[cdpo]|\\U\+[0-9A-Fa-f]{4}')


def _has_mtext_codes(text: str) -> bool:
    """Return True if text contains AutoCAD MTEXT format codes."""
    return bool(_MTEXT_PATTERN.search(text))


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
                raise ValueError(f"Не найден подходящий переводчик для формата: {input_path.suffix}")
            
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

            if not input_path.exists():
                logger.error(f"File disappeared before extract(): {input_path}")
                raise DocumentOpenError(
                    file_path=str(input_path),
                    reason="File not found at extract time",
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
            provider = await self.get_provider()
            # Diagnostic: accumulate original texts of all failed units for aggregate MTEXT stats
            _all_failed_original_texts: list[str] = []
            _diagnostic_path = Path(f"/app/logs/failed_units_diagnostic_{job_id}.jsonl")
            
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
                    
                    # Log what we're sending to LLM
                    for u in batch.text_units:
                        preview = u.original_text[:50]
                        if len(u.original_text) > 50:
                            preview += "..."
                        logger.info("→ LLM input %s: %r", u.id, preview)

                    # Call LLM provider
                    translations = await provider.translate_batch(batch_data)
                    
                    # Log what came back from LLM
                    for t in translations:
                        preview = t["text"][:50]
                        if len(t["text"]) > 50:
                            preview += "..."
                        logger.info("← LLM output %s: %r", t["id"], preview)

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
                         
                except TranslationError as e:
                    # Don't fail entire job because of one bad batch —
                    # log it, keep the untranslated units as-is, and
                    # continue with remaining batches.  This protects
                    # large files (2600+ batches) from losing hours of
                    # work due to a single problematic text unit.
                    failed_unit_ids = [u.id for u in batch.text_units]
                    logger.warning(
                        f"Batch {i + 1}/{total_batches} failed after retries "
                        f"({len(failed_unit_ids)} units will keep original text): {e}"
                    )
                    await self.journal_service.log_warning(
                        JournalStage.TRANSLATION,
                        f"Batch {i + 1}/{total_batches} failed: {e} — "
                        f"{len(failed_unit_ids)} units untranslated",
                        filename=filename,
                        details={"failed_batch": i + 1, "failed_units": failed_unit_ids},
                    )

                    # ── Diagnostic: per-unit MTEXT analysis for failed batch ──
                    try:
                        _diag_lines: list[str] = []
                        for u in batch.text_units:
                            txt = getattr(u, 'original_text', '') or ''
                            has_bs = '\\' in txt
                            has_mtext = _has_mtext_codes(txt)
                            _all_failed_original_texts.append(txt)
                            rec = {
                                "job_id": job_id,
                                "batch": i + 1,
                                "unit_id": u.id,
                                "length": len(txt),
                                "has_backslash": has_bs,
                                "has_mtext_code": has_mtext,
                                "preview": txt[:80],
                            }
                            _diag_lines.append(json.dumps(rec, ensure_ascii=False))
                        _diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_diagnostic_path, 'a', encoding='utf-8') as _f:
                            _f.write('\n'.join(_diag_lines) + '\n')
                        logger.info(
                            "Wrote %d diagnostic lines to %s", len(_diag_lines), _diagnostic_path
                        )
                    except Exception as diag_exc:
                        logger.debug("MTEXT diagnostic write failed: %s", diag_exc)
                    # ── end diagnostic ──

                    # Skip this batch — its units won't appear in all_translations
                    # and will retain their original_text during document assembly.
                
                except ModelUnavailableError as e:
                    logger.error(f"Batch {i + 1} translation failed (model unavailable): {e}")
                    await self.journal_service.log_error(
                        JournalStage.TRANSLATION,
                        f"Batch {i + 1}/{total_batches} failed: {e}",
                        filename=filename,
                    )
                    raise
                
                # Small delay between batches to avoid overwhelming the model
                if i < len(batches) - 1:
                    await asyncio.sleep(0.5)

            # ── Gap-fill: retry missing units after main pass ──
            missing_units = [u for u in translatable_units if u.id not in all_translations]
            if missing_units:
                from file_translator.infrastructure.config import (
                    GAP_FILL_MAX_ROUNDS,
                    GAP_FILL_BATCH_SIZE,
                )

                # ── Task 4: Diagnostic for partial batch loss ──
                try:
                    _diag_lines: list[str] = []
                    for u in missing_units:
                        txt = getattr(u, 'original_text', '') or ''
                        rec = {
                            "job_id": job_id,
                            "unit_id": u.id,
                            "length": len(txt),
                            "has_backslash": '\\' in txt,
                            "has_mtext_code": _has_mtext_codes(txt),
                            "preview": txt[:80],
                            "loss_type": "partial_batch_loss",
                        }
                        _diag_lines.append(json.dumps(rec, ensure_ascii=False))
                    _diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(_diagnostic_path, 'a', encoding='utf-8') as _f:
                        _f.write('\n'.join(_diag_lines) + '\n')
                    logger.info(
                        "Wrote %d partial-loss diagnostic lines to %s",
                        len(_diag_lines), _diagnostic_path,
                    )
                except Exception as diag_exc:
                    logger.debug("Partial-loss diagnostic write failed: %s", diag_exc)

                logger.warning(
                    f"Partial-loss diagnostic: {len(missing_units)} units missing from "
                    f"successful batches. MTEXT codes present in "
                    f"{sum(1 for u in missing_units if _has_mtext_codes(u.original_text))}/"
                    f"{len(missing_units)} of them. IDs: {[u.id for u in missing_units]}"
                )

                # ── Task 1: Gap-fill retry rounds ──
                for round_num in range(1, GAP_FILL_MAX_ROUNDS + 1):
                    if not missing_units:
                        break

                    logger.warning(
                        f"Gap-fill round {round_num}/{GAP_FILL_MAX_ROUNDS}: retrying "
                        f"{len(missing_units)} units missed during the main pass "
                        f"({[u.id for u in missing_units]})"
                    )

                    mini_batches = self._create_batches(
                        missing_units, GAP_FILL_BATCH_SIZE,
                        source_lang, target_lang,
                        translation_style, translation_mode,
                        request.use_glossary,
                    )

                    still_missing: list[TextUnit] = []
                    for mini_batch in mini_batches:
                        if await self.job_manager.is_cancelled(job_id):
                            logger.warning(
                                f"Job {job_id} cancelled during gap-fill, stopping"
                            )
                            break

                        mini_batch_data = {
                            "batch": mini_batch,
                            "source_language": source_lang,
                            "target_language": target_lang,
                            "translation_style": translation_style,
                            "translation_mode": translation_mode,
                            "use_glossary": request.use_glossary,
                            "glossary_entries": glossary_entries,
                        }
                        try:
                            # Log gap-fill input
                            for u in mini_batch.text_units:
                                preview = u.original_text[:50]
                                if len(u.original_text) > 50:
                                    preview += "..."
                                logger.info("→ GAP-FILL LLM input %s: %r", u.id, preview)

                            translations = await provider.translate_batch(
                                mini_batch_data
                            )

                            # Log gap-fill output
                            for t in translations:
                                preview = t["text"][:50]
                                if len(t["text"]) > 50:
                                    preview += "..."
                                logger.info("← GAP-FILL LLM output %s: %r", t["id"], preview)

                            for t in translations:
                                all_translations[t["id"]] = t["text"]
                            for u in mini_batch.text_units:
                                if u.id not in all_translations:
                                    still_missing.append(u)
                        except TranslationError as e:
                            logger.warning(
                                f"Gap-fill mini-batch failed: {e}"
                            )
                            still_missing.extend(
                                u for u in mini_batch.text_units
                                if u.id not in all_translations
                            )

                    missing_units = still_missing

                if missing_units:
                    logger.warning(
                        f"Gap-fill exhausted after {GAP_FILL_MAX_ROUNDS} rounds: "
                        f"{len(missing_units)} units still untranslated, keeping "
                        f"original text: {[u.id for u in missing_units]}"
                    )

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

            # ── Diagnostic: aggregate MTEXT pattern statistics ──
            if _all_failed_original_texts or translatable_units:
                all_texts = [getattr(u, 'original_text', '') or '' for u in translatable_units]
                all_mtext_count = sum(1 for t in all_texts if _has_mtext_codes(t))
                all_mtext_pct = (all_mtext_count / len(all_texts) * 100) if all_texts else 0.0
                failed_mtext_count = sum(1 for t in _all_failed_original_texts if _has_mtext_codes(t))
                failed_mtext_pct = (failed_mtext_count / len(_all_failed_original_texts) * 100) if _all_failed_original_texts else 0.0
                logger.warning(
                    f"MTEXT diagnostic: {all_mtext_pct:.1f}% of ALL units ({all_mtext_count}/{len(all_texts)}) "
                    f"contain MTEXT codes; "
                    f"{failed_mtext_pct:.1f}% of FAILED units ({failed_mtext_count}/{len(_all_failed_original_texts)}) "
                    f"contain MTEXT codes"
                )
            # ── end aggregate diagnostic ──
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
