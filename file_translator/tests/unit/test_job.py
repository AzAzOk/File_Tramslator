"""Unit tests for job progress tracking, focusing on the PDF CONVERSION stage."""

from __future__ import annotations

import pytest

from file_translator.domain.job import Job, ProcessingStage, _STAGE_WEIGHTS


class TestConversionStage:
    def test_enum_value(self):
        assert ProcessingStage.CONVERSION.value == "conversion"

    def test_weight_between_validation_and_extraction(self):
        assert _STAGE_WEIGHTS[ProcessingStage.CONVERSION] == pytest.approx(0.12)
        assert (
            _STAGE_WEIGHTS[ProcessingStage.VALIDATION]
            < _STAGE_WEIGHTS[ProcessingStage.CONVERSION]
            < _STAGE_WEIGHTS[ProcessingStage.EXTRACTION]
        )


class TestConversionInterpolation:
    def test_sets_current_stage(self):
        job = Job(job_id="j1")
        job.update_progress(ProcessingStage.CONVERSION, 0, 120)
        assert job.current_stage == ProcessingStage.CONVERSION

    def test_start_equals_validation_weight(self):
        job = Job(job_id="j2")
        job.update_progress(ProcessingStage.CONVERSION, 0, 120)
        assert job.progress == pytest.approx(_STAGE_WEIGHTS[ProcessingStage.VALIDATION])

    def test_end_equals_extraction_weight(self):
        job = Job(job_id="j3")
        job.update_progress(ProcessingStage.CONVERSION, 120, 120)
        assert job.progress == pytest.approx(_STAGE_WEIGHTS[ProcessingStage.EXTRACTION])

    def test_mid_value_scales_linearly(self):
        job = Job(job_id="j4")
        job.update_progress(ProcessingStage.CONVERSION, 60, 120)
        expected = 0.05 + (0.20 - 0.05) * 0.5
        assert job.progress == pytest.approx(expected)

    def test_zero_total_falls_back_to_stage_weight(self):
        job = Job(job_id="j5")
        job.update_progress(ProcessingStage.CONVERSION, 0, 0)
        assert job.progress == pytest.approx(_STAGE_WEIGHTS[ProcessingStage.CONVERSION])


class TestTranslationInterpolationUnchanged:
    def test_translation_start(self):
        job = Job(job_id="j6")
        job.update_progress(ProcessingStage.TRANSLATION, 0, 10)
        assert job.progress == pytest.approx(_STAGE_WEIGHTS[ProcessingStage.EXTRACTION])

    def test_translation_mid(self):
        job = Job(job_id="j7")
        job.update_progress(ProcessingStage.TRANSLATION, 5, 10)
        expected = 0.20 + (0.95 - 0.20) * 0.5
        assert job.progress == pytest.approx(expected)
