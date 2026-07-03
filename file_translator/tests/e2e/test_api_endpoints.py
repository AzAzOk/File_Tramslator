"""E2E tests for File Translator API (requires running server)."""

import httpx
import pytest
from file_translator.infrastructure.translators.okapi_service import OkapiService


# Skip translate tests if Tikal is not available
_tikal_available = pytest.mark.skipif(
    not OkapiService().check_available(),
    reason="Tikal CLI not available — full translation pipeline cannot be tested",
)


class TestAPIEndpoints:
    """End-to-end tests for API endpoints (requires running server at localhost:8000)."""

    @pytest.mark.asyncio
    async def test_health_check(self):
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8000/health")
        result = response.json()
        assert result["status"] == "healthy"
        assert "version" in result

    @_tikal_available
    @pytest.mark.asyncio
    async def test_translate_document_endpoint(self, temp_docx_file):
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(temp_docx_file, "rb") as f:
                files = {"file": ("test.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
                data = {"source_language": "en", "target_language": "ru"}
                response = await client.post("http://localhost:8000/translate", files=files, data=data)
        result, status_code = response.json(), response.status_code
        assert status_code == 200, f"Expected 200, got {status_code}: {result}"
        assert result["success"] is not None
