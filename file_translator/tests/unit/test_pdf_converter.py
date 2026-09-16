"""Unit tests for PdfToDocxConverter — deadline math and HTTP client behaviour.

The converter's blocking core is ``convert_sync`` (httpx.Client) — the async
``convert`` wrapper just runs it in a worker thread so the event loop stays
free. The HTTP-level tests therefore mock ``httpx.Client``, which is the
actual primitive exercised by the pipeline; one extra test asserts the async
wrapper delegates to ``convert_sync`` via ``asyncio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import file_translator.infrastructure.converters.pdf_to_docx_converter as mod
from file_translator.domain.errors import ConversionError, ConversionTimeoutError
from file_translator.infrastructure.converters.pdf_to_docx_converter import (
    FLOOR_SECONDS,
    SECONDS_PER_MB,
    PdfToDocxConverter,
)

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_file(tmp_path) -> Path:
    p = tmp_path / "input.pdf"
    p.write_bytes(b"%PDF-1.4 fake content")
    return p


def _mock_client(monkeypatch, response=None, side_effect=None):
    """Patch httpx.Client and return the inner client instance mock."""
    client = MagicMock()
    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        client.post.return_value = response
    client_cm = MagicMock()
    client_cm.__enter__.return_value = client
    monkeypatch.setattr(httpx, "Client", MagicMock(return_value=client_cm))
    return client


def _ok_response(content: bytes = b"DOCX-BYTES"):
    response = MagicMock()
    response.status_code = 200
    response.content = content
    response.read = MagicMock()
    return response


# ---------------------------------------------------------------------------
# 8.1 Deadline math
# ---------------------------------------------------------------------------

class TestEstimateDeadline:
    def test_floor_on_tiny_file(self):
        # 0 bytes → estimate = FLOOR, deadline = 2×FLOOR (slack), below cap.
        assert PdfToDocxConverter.estimate_deadline(0, 3600) == pytest.approx(
            FLOOR_SECONDS * 2
        )

    def test_linear_growth(self):
        size = 10 * MB
        expected = (FLOOR_SECONDS + 10 * SECONDS_PER_MB) * 2.0
        assert PdfToDocxConverter.estimate_deadline(size, 3600) == pytest.approx(expected)

    def test_double_slack(self):
        """Deadline is exactly 2× the raw estimate (before the cap)."""
        size = 20 * MB
        raw = FLOOR_SECONDS + (size / MB) * SECONDS_PER_MB
        assert PdfToDocxConverter.estimate_deadline(size, 10_000) == pytest.approx(raw * 2.0)

    def test_hard_cap(self):
        # ~2 GB PDF would estimate ~14800 s with slack → capped at 3600.
        size = 2000 * MB
        assert PdfToDocxConverter.estimate_deadline(size, 3600) == pytest.approx(3600.0)

    def test_default_cap_from_env(self, monkeypatch):
        monkeypatch.setenv("FIRST_PDF_CONVERTER_MAX_TIMEOUT", "100")
        assert PdfToDocxConverter.estimate_deadline(2000 * MB) == pytest.approx(100.0)

    def test_manual_override_disables_size_model(self):
        conv = PdfToDocxConverter(base_url="http://x", timeout=50.0, max_timeout=3600.0)
        assert conv.resolve_deadline(500 * MB) == pytest.approx(50.0)

    def test_manual_override_capped_by_max(self):
        conv = PdfToDocxConverter(base_url="http://x", timeout=9999.0, max_timeout=3600.0)
        assert conv.resolve_deadline(1) == pytest.approx(3600.0)

    def test_env_url_and_timeout(self, monkeypatch):
        monkeypatch.setenv("FIRST_PDF_CONVERTER_URL", "http://custom:9000/")
        monkeypatch.setenv("FIRST_PDF_CONVERTER_TIMEOUT", "42")
        conv = PdfToDocxConverter()
        assert conv.base_url == "http://custom:9000"
        assert conv.timeout == pytest.approx(42.0)
        assert conv.resolve_deadline(1) == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# 8.3 HTTP client behaviour (httpx.Client mocked)
# ---------------------------------------------------------------------------

class TestConvertSync:
    def test_success_writes_file_and_sends_header(self, monkeypatch, pdf_file, tmp_path):
        response = _ok_response(b"converted-docx")
        client = _mock_client(monkeypatch, response=response)
        out_dir = tmp_path / "out"
        conv = PdfToDocxConverter(base_url="http://converter", max_timeout=3600.0)

        result = conv.convert_sync(pdf_file, out_dir)

        assert result == out_dir / "input.docx"
        assert result.read_bytes() == b"converted-docx"

        url = client.post.call_args.args[0]
        assert url == "http://converter/convert"

        kwargs = client.post.call_args.kwargs
        expected_deadline = PdfToDocxConverter.estimate_deadline(
            pdf_file.stat().st_size, 3600.0
        )
        assert kwargs["headers"]["X-Timeout-Seconds"] == str(int(expected_deadline))

        sent_name, _, sent_type = kwargs["files"]["file"]
        assert sent_name == "input.pdf"
        assert sent_type == "application/pdf"

    def test_convert_timeout_maps_to_timeout_error(self, monkeypatch, pdf_file, tmp_path):
        response = MagicMock()
        response.status_code = 504
        response.json.return_value = {"code": "CONVERT_TIMEOUT", "message": "too slow"}
        _mock_client(monkeypatch, response=response)
        conv = PdfToDocxConverter(base_url="http://converter")

        with pytest.raises(ConversionTimeoutError) as exc:
            conv.convert_sync(pdf_file, tmp_path / "out")

        assert "Превышено время конвертации PDF" in str(exc.value)
        assert exc.value.error_code == "CONVERT_TIMEOUT"
        assert exc.value.status_code == 504

    def test_convert_failed_maps_to_conversion_error(self, monkeypatch, pdf_file, tmp_path):
        response = MagicMock()
        response.status_code = 502
        response.json.return_value = {"code": "CONVERT_FAILED", "message": "gui crashed"}
        _mock_client(monkeypatch, response=response)
        conv = PdfToDocxConverter(base_url="http://converter")

        with pytest.raises(ConversionError) as exc:
            conv.convert_sync(pdf_file, tmp_path / "out")

        assert "Ошибка конвертации PDF в DOCX" in str(exc.value)
        assert exc.value.error_code == "CONVERT_FAILED"

    def test_non_json_error_body_falls_back_to_text(self, monkeypatch, pdf_file, tmp_path):
        response = MagicMock()
        response.status_code = 500
        response.json.side_effect = ValueError("not json")
        response.text = "internal boom"
        _mock_client(monkeypatch, response=response)
        conv = PdfToDocxConverter(base_url="http://converter")

        with pytest.raises(ConversionError) as exc:
            conv.convert_sync(pdf_file, tmp_path / "out")

        assert "internal boom" in str(exc.value)

    def test_connection_error_maps_to_conversion_error(self, monkeypatch, pdf_file, tmp_path):
        _mock_client(monkeypatch, side_effect=httpx.ConnectError("refused"))
        conv = PdfToDocxConverter(base_url="http://converter")

        with pytest.raises(ConversionError) as exc:
            conv.convert_sync(pdf_file, tmp_path / "out")

        assert "Сервис конвертации недоступен" in str(exc.value)
        assert exc.value.error_code == "CONVERTER_UNREACHABLE"

    def test_empty_result_raises(self, monkeypatch, pdf_file, tmp_path):
        _mock_client(monkeypatch, response=_ok_response(b""))
        conv = PdfToDocxConverter(base_url="http://converter")

        with pytest.raises(ConversionError) as exc:
            conv.convert_sync(pdf_file, tmp_path / "out")

        assert "пустой результат" in str(exc.value)

    def test_missing_pdf_raises(self, tmp_path):
        conv = PdfToDocxConverter(base_url="http://converter")
        with pytest.raises(ConversionError) as exc:
            conv.convert_sync(tmp_path / "nope.pdf", tmp_path / "out")
        assert exc.value.error_code == "FILE_NOT_FOUND"


class TestConvertAsyncWrapper:
    @pytest.mark.asyncio
    async def test_delegates_to_convert_sync_in_thread(self, tmp_path):
        conv = PdfToDocxConverter(base_url="http://converter")
        sentinel = tmp_path / "out.docx"
        with patch.object(mod.asyncio, "to_thread", new=AsyncMock(return_value=sentinel)) as tt:
            result = await conv.convert(tmp_path / "in.pdf", tmp_path / "out")
        assert result == sentinel
        assert tt.await_count == 1
        assert tt.await_args is not None
        assert tt.await_args.args[0] == conv.convert_sync
