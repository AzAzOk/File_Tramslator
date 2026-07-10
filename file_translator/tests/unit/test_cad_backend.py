"""Tests for EzdxfBackend, DxfDocumentParser, DxfUpdater, FormatRegistry."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import ezdxf
import pytest

from file_translator.domain.document_model import EntityType, TranslationStatus
from file_translator.infrastructure.backends.ezdxf_backend import EzdxfBackend
from file_translator.infrastructure.document.format_registry import FormatRegistry
from file_translator.infrastructure.parsers.dxf_parser import DxfDocumentParser, DxfParser
from file_translator.infrastructure.services.oda_converter_service import (
    is_available as oda_available,
)
from file_translator.infrastructure.updaters.dxf_updater import DxfUpdater

oda = pytest.mark.skipif(not oda_available(), reason="ODAFileConverter not installed")
not_oda = pytest.mark.skipif(oda_available(), reason="ODAFileConverter is installed")


# ── helpers ──

@pytest.fixture
def simple_dxf() -> Path:
    """Create a minimal DXF with one TEXT and one MTEXT entity."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_text("Hello Valve", dxfattribs={"height": 2.5, "layer": "TEXT_LAYER"})
    msp.add_mtext("Warning: do not exceed", dxfattribs={"layer": "WARN_LAYER"})
    path = Path(tempfile.mktemp(suffix=".dxf"))
    doc.saveas(str(path))
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def multi_entity_dxf() -> Path:
    """Create DXF with TEXT, MTEXT, ATTDEF."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_text("Simple text", dxfattribs={"height": 2.0})
    mtext = msp.add_mtext("Multi\\Pline\\Ptext")
    mtext.dxf.char_height = 2.0
    block = doc.blocks.new("TEST_BLOCK")
    block.add_attdef(tag="TAG2", text="Attribute Def", dxfattribs={"height": 2.0})
    msp.add_blockref("TEST_BLOCK", insert=(0, 0))
    path = Path(tempfile.mktemp(suffix=".dxf"))
    doc.saveas(str(path))
    yield path
    path.unlink(missing_ok=True)


# ── EzdxfBackend ──

class TestEzdxfBackend:
    def test_open_and_close(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        assert dxf_doc is not None
        assert dxf_doc.dxfversion is not None
        backend.close(dxf_doc)

    def test_iter_entities_counts(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        entities = list(backend.iter_entities(dxf_doc))
        assert len(entities) == 2

    def test_get_text(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        texts = []
        for entity, _source in backend.iter_entities(dxf_doc):
            texts.append(backend.get_text(entity))
        assert "Hello Valve" in texts
        assert "Warning: do not exceed" in texts

    def test_get_handle(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        for entity, _source in backend.iter_entities(dxf_doc):
            handle = backend.get_handle(entity)
            assert isinstance(handle, str)
            assert len(handle) > 0

    def test_get_layer(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        layers = set()
        for entity, _source in backend.iter_entities(dxf_doc):
            layers.add(backend.get_layer(entity))
        assert "TEXT_LAYER" in layers
        assert "WARN_LAYER" in layers

    def test_set_text(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        for entity, _source in backend.iter_entities(dxf_doc):
            backend.set_text(entity, "TRANSLATED")
        tmp = Path(tempfile.mktemp(suffix=".dxf"))
        backend.save(dxf_doc, tmp)
        doc2 = ezdxf.readfile(str(tmp))
        for e in doc2.modelspace():
            assert e.dxf.text == "TRANSLATED"
        tmp.unlink(missing_ok=True)
        backend.close(dxf_doc)

    def test_save_preserves_structure(self, simple_dxf):
        backend = EzdxfBackend()
        dxf_doc = backend.open(simple_dxf)
        tmp = Path(tempfile.mktemp(suffix=".dxf"))
        backend.save(dxf_doc, tmp)
        doc2 = ezdxf.readfile(str(tmp))
        assert len(list(doc2.modelspace())) == 2
        tmp.unlink(missing_ok=True)
        backend.close(dxf_doc)

    def test_empty_text_skipped(self):
        backend = EzdxfBackend()
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("", dxfattribs={"height": 2.0})
        mtext = msp.add_mtext("")
        mtext.dxf.char_height = 2.0
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        dxf_doc = backend.open(path)
        entities = list(backend.iter_entities(dxf_doc))
        assert len(entities) == 2
        path.unlink(missing_ok=True)

    def test_mtext_various_formatting(self):
        """MTEXT with formatting codes: get_text returns raw, set_text accepts raw."""
        backend = EzdxfBackend()
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        mtext = msp.add_mtext(r"Plain text with {\fArial|b1|i1;BoldItalic} word")
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        dxf_doc = backend.open(path)
        entities = list(backend.iter_entities(dxf_doc))
        assert len(entities) == 1
        entity = entities[0][0]
        text = backend.get_text(entity)
        assert "Plain text" in text
        backend.set_text(entity, text + " ADDED")
        tmp = Path(tempfile.mktemp(suffix=".dxf"))
        backend.save(dxf_doc, tmp)
        doc2 = ezdxf.readfile(str(tmp))
        saved_mtext = list(doc2.modelspace())[0]
        assert saved_mtext.dxf.text is not None
        path.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    def test_acad_table_skipped(self):
        """ACAD_TABLE entities are skipped by iter_entities (handled via proxy graphic)."""
        backend = EzdxfBackend()
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("Regular text", dxfattribs={"height": 2.0})
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        dxf_doc = backend.open(path)
        entities = list(backend.iter_entities(dxf_doc))
        assert all(e[0].dxftype() != "ACAD_TABLE" or e[1] == "proxy_graphic" for e in entities)
        path.unlink(missing_ok=True)

    def test_dimension_with_text(self):
        backend = EzdxfBackend()
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        dim = msp.add_aligned_dim(p1=(0, 10), p2=(100, 10), distance=20, text="100m")
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        dxf_doc = backend.open(path)
        dim_found = False
        for entity, _source in backend.iter_entities(dxf_doc):
            if entity.dxftype() == "DIMENSION":
                text = backend.get_text(entity)
                assert text == "100m"
                dim_found = True
        assert dim_found
        path.unlink(missing_ok=True)


# ── DxfDocumentParser (new IParser) ──

class TestDxfDocumentParser:
    def test_parse_counts(self, simple_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        assert len(doc.entities) == 2
        assert doc.metadata.get("format") == "DXF"

    def test_parse_entity_types(self, multi_entity_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(multi_entity_dxf)
        types = {e.type for e in doc.entities}
        assert EntityType.TEXT in types
        assert EntityType.ATTRIB in types or EntityType.ATTDEF in types

    def test_parse_handles_populated(self, simple_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        for e in doc.entities:
            assert len(e.handles) >= 1

    def test_parse_text_content(self, simple_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        texts = [e.text for e in doc.entities]
        assert "Hello Valve" in texts
        assert "Warning: do not exceed" in texts

    def test_parse_dedup_same_text(self):
        """Two entities with identical text get grouped into one TranslatableEntity."""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("Duplicate text", dxfattribs={"height": 2.0})
        msp.add_text("Duplicate text", dxfattribs={"height": 2.0})
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        parser = DxfDocumentParser()
        result = parser.parse(path)
        assert len(result.entities) == 1
        assert len(result.entities[0].handles) == 2
        path.unlink(missing_ok=True)

    def test_parse_empty_text_skipped(self):
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("", dxfattribs={"height": 2.0})
        msp.add_text("Real text", dxfattribs={"height": 2.0})
        path = Path(tempfile.mktemp(suffix=".dxf"))
        doc.saveas(str(path))
        parser = DxfDocumentParser()
        result = parser.parse(path)
        assert len(result.entities) == 1
        assert result.entities[0].text == "Real text"
        path.unlink(missing_ok=True)

    def test_capabilities(self):
        parser = DxfDocumentParser()
        caps = parser.capabilities()
        assert "attributes" in caps
        assert "blocks" in caps

    def test_parse_nonexistent_file(self):
        parser = DxfDocumentParser()
        with pytest.raises(FileNotFoundError):
            parser.parse(Path("/nonexistent/file.dxf"))


# ── DxfUpdater ──

class TestDxfUpdater:
    def test_apply_basic(self, simple_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        translations = {doc.entities[0].id: "Translated Valve"}
        updater = DxfUpdater()
        updater.apply(doc, translations)
        assert doc.entities[0].translation_status == TranslationStatus.TRANSLATED
        assert doc.entities[1].translation_status == TranslationStatus.PENDING

    def test_save_applies_updates(self, simple_dxf):
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        translations = {e.id: f"TRANS_{e.text}" for e in doc.entities}
        updater = DxfUpdater()
        updater.apply(doc, translations)
        out = Path(tempfile.mktemp(suffix=".dxf"))
        updater.save(doc, out)
        doc2 = ezdxf.readfile(str(out))
        saved_texts = []
        for e in doc2.modelspace():
            if hasattr(e.dxf, "text"):
                saved_texts.append(e.dxf.text)
        assert all(t.startswith("TRANS_") for t in saved_texts)
        out.unlink(missing_ok=True)

    def test_save_preserves_source(self, simple_dxf):
        """The source file should not be modified."""
        original_content = simple_dxf.read_bytes()
        parser = DxfDocumentParser()
        doc = parser.parse(simple_dxf)
        updater = DxfUpdater()
        updater.apply(doc, {e.id: "test" for e in doc.entities})
        out = Path(tempfile.mktemp(suffix=".dxf"))
        updater.save(doc, out)
        assert simple_dxf.read_bytes() == original_content
        out.unlink(missing_ok=True)


# ── DxfParser (backward-compatible) ──

class TestDxfParser:
    def test_parse(self, simple_dxf):
        parser = DxfParser()
        doc = parser.parse(simple_dxf)
        assert doc.file_path
        assert doc.format_version

    def test_get_all_texts(self, simple_dxf):
        parser = DxfParser()
        doc = parser.parse(simple_dxf)
        texts = doc.get_all_texts()
        assert len(texts) == 2

    def test_validate_structure(self, simple_dxf):
        parser = DxfParser()
        assert parser.validate_structure(simple_dxf) is True

    def test_validate_structure_invalid(self):
        parser = DxfParser()
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            f.write(b"not a dxf file")
            p = Path(f.name)
        assert parser.validate_structure(p) is False
        p.unlink(missing_ok=True)

    def test_parse_nonexistent(self):
        parser = DxfParser()
        with pytest.raises(Exception):
            parser.parse(Path("/nonexistent/file.dxf"))


# ── FormatRegistry ──

class TestFormatRegistry:
    def test_register_and_get(self):
        registry = FormatRegistry()
        registry.register(".dxf", parser=DxfDocumentParser, updater=DxfUpdater)
        parser, updater = registry.get(".dxf")
        assert isinstance(parser, DxfDocumentParser)
        assert isinstance(updater, DxfUpdater)

    def test_can_process(self):
        registry = FormatRegistry()
        registry.register(".dxf", parser=DxfDocumentParser, updater=DxfUpdater)
        assert registry.can_process(Path("file.dxf")) is True
        assert registry.can_process(Path("file.dwg")) is False

    def test_get_for_file(self):
        registry = FormatRegistry()
        registry.register(".dxf", parser=DxfDocumentParser, updater=DxfUpdater)
        entry = registry.get_for_file(Path("test.dxf"))
        assert entry is not None

    def test_get_for_file_unknown(self):
        registry = FormatRegistry()
        with pytest.raises(KeyError):
            registry.get_for_file(Path("test.unknown"))

    def test_get_unregistered(self):
        registry = FormatRegistry()
        with pytest.raises(KeyError):
            registry.get(".unknown")


# ── DWG via ODAFileConverter ──

class TestDwgBackend:
    @oda
    def test_open_dwg(self):
        """A .dwg file can be opened and read via ODA backend."""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("DWG hello", dxfattribs={"height": 2.5})
        path = Path(tempfile.mktemp(suffix=".dwg"))
        doc.saveas(str(path))

        backend = EzdxfBackend()
        dxf_doc = backend.open(path)
        texts = [backend.get_text(e) for e, _ in backend.iter_entities(dxf_doc)]
        assert "DWG hello" in texts
        backend.cleanup_temp(path)
        path.unlink(missing_ok=True)

    @oda
    def test_save_dwg(self):
        """Save a translated doc back to .dwg."""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("Original", dxfattribs={"height": 2.5})
        src = Path(tempfile.mktemp(suffix=".dwg"))
        doc.saveas(str(src))

        backend = EzdxfBackend()
        dxf_doc = backend.open(src)
        for e, _ in backend.iter_entities(dxf_doc):
            backend.set_text(e, "Translated")

        out = Path(tempfile.mktemp(suffix=".dwg"))
        backend.save(dxf_doc, out)
        assert out.exists()
        assert out.stat().st_size > 0

        # Re-open and verify
        dxf_doc2 = backend.open(out)
        texts = [backend.get_text(e) for e, _ in backend.iter_entities(dxf_doc2)]
        assert "Translated" in texts
        backend.cleanup_temp(src)
        src.unlink(missing_ok=True)
        out.unlink(missing_ok=True)

    @oda
    def test_dwg_via_format_registry(self):
        """FormatRegistry also accepts .dwg."""
        registry = FormatRegistry()
        registry.register(".dwg", parser=DxfDocumentParser, updater=DxfUpdater)
        assert registry.can_process(Path("test.dwg")) is True

    @oda
    def test_dwg_oda_service_available(self):
        """OdaConverterService reports available with ODA installed."""
        from file_translator.infrastructure.services.oda_converter_service import is_available
        assert is_available() is True

    @not_oda
    def test_dwg_no_oda_raises(self):
        """Without ODA, opening .dwg should raise."""
        backend = EzdxfBackend()
        doc = ezdxf.new("R2010")
        path = Path(tempfile.mktemp(suffix=".dwg"))
        doc.saveas(str(path))
        with pytest.raises(RuntimeError, match="ODAFileConverter"):
            backend.open(path)
        path.unlink(missing_ok=True)
