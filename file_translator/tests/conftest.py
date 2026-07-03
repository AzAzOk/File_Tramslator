"""Pytest configuration and fixtures."""

import asyncio
import sys
from pathlib import Path

import pytest


# Add project root to Python path
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_docx_file(tmp_path):
    """Create a minimal DOCX file for testing."""
    import zipfile
    
    docx_path = tmp_path / "test.docx"
    
    # Create minimal valid DOCX structure
    with zipfile.ZipFile(docx_path, 'w') as zf:
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
    <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
    <Default Extension="xml" ContentType="application/xml"/>
    <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        
        zf.writestr("word/_rels/document.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
</Relationships>""")
        
        zf.writestr("word/document.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
    <w:body>
        <w:p>
            <w:r>
                <w:t>Hello World</w:t>
            </w:r>
        </w:p>
    </w:body>
</w:document>""")
    
    return docx_path


@pytest.fixture
def mock_llm_provider():
    """Create a mock LLM provider for testing."""
    from unittest.mock import AsyncMock, MagicMock
    
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.translate_batch = AsyncMock(return_value=[
        {"id": "test-1", "text": "Translated text"},
    ])
    
    return provider


@pytest.fixture
def mock_qwen3_provider():
    """Mock provider: model_name='qwen3:8b-gpu-99', supports_tag_preservation=True."""
    from unittest.mock import AsyncMock, MagicMock
    from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider
    
    provider = MagicMock(spec=OpenAITranslationProvider)
    provider.model_name = "qwen3:8b-gpu-99"
    provider.supports_tag_preservation = True
    provider.is_available.return_value = True
    provider.translate_batch = AsyncMock(return_value=[
        {"id": "p_0", "text": "Предупреждение — <s1>не превышайте</s1> <s2>лимиты давления</s2>"},
    ])
    return provider


@pytest.fixture
def mock_translategemma_provider():
    """Mock provider: model_name without qwen3, supports_tag_preservation=False."""
    from unittest.mock import AsyncMock, MagicMock
    from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider
    
    provider = MagicMock(spec=OpenAITranslationProvider)
    provider.model_name = "translategemma:12b"
    provider.supports_tag_preservation = False
    provider.is_available.return_value = True
    provider.translate_batch = AsyncMock(return_value=[
        {"id": "p_0", "text": "Предупреждение — не превышайте лимиты давления"},
    ])
    return provider


@pytest.fixture
def qwen3_tagged_response():
    """Realistic qwen3 response with <think> block + valid tags."""
    return (
        "<think>I need to translate this carefully.</think>\n"
        "Предупреждение — <s1>не превышайте</s1> <s2>лимиты давления</s2>"
    )


@pytest.fixture
def qwen3_invalid_tag_response():
    """qwen3 response where a tag was dropped."""
    return "Предупреждение — не превышайте лимиты давления"


@pytest.fixture
def sample_paragraph_multi_style():
    """Build a <w:p> with 3 runs: bold 'Warning', normal ' — do not ', bold 'exceed limits'."""
    from xml.etree import ElementTree as ET
    
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    W = f"{{{ns}}}"
    
    p = ET.Element(f"{W}p")
    
    # Run 1: bold "Warning"
    r1 = ET.SubElement(p, f"{W}r")
    rpr1 = ET.SubElement(r1, f"{W}rPr")
    b1 = ET.SubElement(rpr1, f"{W}b")
    t1 = ET.SubElement(r1, f"{W}t")
    t1.text = "Warning"
    
    # Run 2: normal " — do not "
    r2 = ET.SubElement(p, f"{W}r")
    t2 = ET.SubElement(r2, f"{W}t")
    t2.text = " — do not "
    
    # Run 3: bold "exceed limits"
    r3 = ET.SubElement(p, f"{W}r")
    rpr3 = ET.SubElement(r3, f"{W}rPr")
    b3 = ET.SubElement(rpr3, f"{W}b")
    t3 = ET.SubElement(r3, f"{W}t")
    t3.text = "exceed limits"
    
    return p


@pytest.fixture
def sample_paragraph_single_style():
    """<w:p> with 1 run: plain text."""
    from xml.etree import ElementTree as ET
    
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    W = f"{{{ns}}}"
    
    p = ET.Element(f"{W}p")
    r = ET.SubElement(p, f"{W}r")
    t = ET.SubElement(r, f"{W}t")
    t.text = "Hello World"
    
    return p
