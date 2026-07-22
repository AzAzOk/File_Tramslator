"""Unit tests for OkapiService — XLIFF parsing, saving, inline code distribution."""

import copy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from file_translator.infrastructure.translators.okapi_service import (
    NS_XLIFF,
    OkapiService,
    OkapiServiceError,
    XliffUnit,
    _temp_work_dir,
)


# ---------------------------------------------------------------------------
# Sample XLIFF data
# ---------------------------------------------------------------------------

_SAMPLE_XLF = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="en" target-language="ru" datatype="x-ooxml">
    <body>
      <trans-unit id="p_0" translate="yes">
        <source>Hello World</source>
        <target/>
      </trans-unit>
      <trans-unit id="p_1" translate="yes">
        <source><g id="1">Warning</g> — do not exceed limits</source>
        <target/>
      </trans-unit>
      <trans-unit id="p_2" translate="no">
        <source>Do not translate</source>
        <target/>
      </trans-unit>
      <trans-unit id="p_3" translate="yes">
        <source>Concrete <g id="2">strength</g> test <g id="3">report</g></source>
        <target/>
      </trans-unit>
    </body>
  </file>
</xliff>"""

_XLF_WITH_TARGETS = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="en" target-language="ru" datatype="x-ooxml">
    <body>
      <trans-unit id="p_0" translate="yes">
        <source>Hello</source>
        <target>Привет</target>
      </trans-unit>
      <trans-unit id="p_1" translate="yes">
        <source>World</source>
        <target>Мир</target>
      </trans-unit>
    </body>
  </file>
</xliff>"""


# ===================================================================
# OkapiService — load_xliff
# ===================================================================

class TestLoadXliff:
    def test_loads_all_trans_units(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        units = OkapiService().load_xliff(xlf)

        assert len(units) == 4

    def test_unit_fields(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        units = OkapiService().load_xliff(xlf)
        u0 = units[0]

        assert u0.id == "p_0"
        assert u0.source_text == "Hello World"
        assert u0.translate is True

    def test_translate_no_translation(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        units = OkapiService().load_xliff(xlf)
        u2 = units[2]

        assert u2.id == "p_2"
        assert u2.translate is False
        assert u2.needs_translation is False

    def test_inline_codes_stripped_from_source_text(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        units = OkapiService().load_xliff(xlf)
        u1 = units[1]

        # <g id="1">Warning</g> — do not exceed limits
        # After stripping tags: Warning — do not exceed limits
        assert "Warning" in u1.source_text
        assert "<g" not in u1.source_text

    def test_target_text_loaded_when_present(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_XLF_WITH_TARGETS, encoding="utf-8")

        units = OkapiService().load_xliff(xlf)

        assert units[0].target_text == "Привет"
        assert not units[0].needs_translation

    def test_missing_file_raises_error(self):
        with pytest.raises(OkapiServiceError):
            OkapiService().load_xliff(Path("/nonexistent.xlf"))


# ===================================================================
# OkapiService — save_xliff
# ===================================================================

class TestSaveXliff:
    def test_updates_target_elements(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        translations = {"p_0": "Привет мир", "p_1": "Предупреждение"}
        OkapiService().save_xliff(xlf, translations)

        tree = ET.parse(str(xlf))
        root = tree.getroot()
        targets = root.findall(f".//{{{NS_XLIFF}}}target")

        # Verify targets got text
        target_texts = []
        for t in targets:
            text = "".join(t.itertext()) if t.text is None else (t.text or "")
            target_texts.append(text)

        assert "Привет мир" in target_texts
        assert any("Предупреждение" in t for t in target_texts)

    def test_creates_target_if_missing(self, tmp_path):
        """trans-unit without <target> should get one created."""
        xlf_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file><body>
    <trans-unit id="u1" translate="yes">
      <source>Hello</source>
    </trans-unit>
  </body></file>
</xliff>"""
        xlf = tmp_path / "test.xlf"
        xlf.write_text(xlf_content, encoding="utf-8")

        OkapiService().save_xliff(xlf, {"u1": "Привет"})

        tree = ET.parse(str(xlf))
        root = tree.getroot()
        target = root.find(f".//{{{NS_XLIFF}}}target")
        assert target is not None
        assert target.text == "Привет"

    def test_skips_unknown_ids(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        OkapiService().save_xliff(xlf, {"nonexistent": "text"})

        tree = ET.parse(str(xlf))
        root = tree.getroot()
        targets = root.findall(f".//{{{NS_XLIFF}}}target")
        all_empty = all(
            "".join(t.itertext()).strip() == ""
            for t in targets
        )
        assert all_empty

    def test_skips_empty_translations(self, tmp_path):
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        OkapiService().save_xliff(xlf, {"p_0": "   "})

        tree = ET.parse(str(xlf))
        root = tree.getroot()
        target = root.find(f".//{{{NS_XLIFF}}}target")
        assert target is not None
        assert (target.text or "").strip() == ""

    def test_preserves_inline_codes_in_target(self, tmp_path):
        """Target should have same inline code structure as source."""
        xlf = tmp_path / "test.xlf"
        xlf.write_text(_SAMPLE_XLF, encoding="utf-8")

        translations = {
            "p_1": "Предупреждение — не превышайте лимиты",
            "p_3": "Бетон <g id=\"2\">прочность</g> тест <g id=\"3\">отчет</g>",
        }
        OkapiService().save_xliff(xlf, translations)

        tree = ET.parse(str(xlf))
        root = tree.getroot()

        # p_1 has <g> inline codes
        tu = root.findall(f".//{{{NS_XLIFF}}}trans-unit[@id='p_1']")[0]
        source = tu.find(f"{{{NS_XLIFF}}}source")
        target = tu.find(f"{{{NS_XLIFF}}}target")

        source_children = list(source)
        target_children = list(target)

        assert len(target_children) == len(source_children)
        for sc, tc in zip(source_children, target_children):
            assert sc.tag == tc.tag
            assert sc.attrib == tc.attrib


# ===================================================================
# _simple_plain_text / _get_plain_text
# ===================================================================

class TestPlainText:
    def test_simple_text(self):
        elem = ET.fromstring("<source>Hello World</source>")
        assert OkapiService._simple_plain_text(elem) == "Hello World"

    def test_inline_codes_stripped(self):
        elem = ET.fromstring('<source><g id="1">Warning</g> — text</source>')
        result = OkapiService._simple_plain_text(elem)
        assert "Warning" in result
        assert "<g" not in result

    def test_run_artifacts_stripped(self):
        """<run1/>, </run1>, <run2>, </run2> artifacts removed but content kept."""
        elem = ET.fromstring("<source>Text <run1/> more <run2>nested</run2></source>")
        result = OkapiService._simple_plain_text(elem)
        assert "run1" not in result
        assert "run2" not in result
        assert "nested" in result  # content inside run2 is kept, only tags removed

    def test_html_entities_unescaped(self):
        """HTML entities in source should be unescaped."""
        elem = ET.fromstring("<source>AT&amp;T &lt;test&gt;</source>")
        result = OkapiService._simple_plain_text(elem)
        assert "AT&T" in result
        assert "&amp;" not in result

    def test_empty_element(self):
        elem = ET.fromstring("<source></source>")
        assert OkapiService._simple_plain_text(elem) == ""

    def test_multiple_spaces_collapsed(self):
        elem = ET.fromstring("<source>Hello    World</source>")
        assert OkapiService._simple_plain_text(elem) == "Hello World"

    def test_xml_with_only_inline_codes(self):
        """Source with only inline codes and no text."""
        elem = ET.fromstring('<source><g id="1"/></source>')
        result = OkapiService._simple_plain_text(elem)
        assert result == ""


# ===================================================================
# _set_target_with_inline_codes (inline code distribution)
# ===================================================================

def _make_source(xml_str: str) -> ET.Element:
    """Wrap in <source> tag and parse."""
    return ET.fromstring(f"<source xmlns='{NS_XLIFF}'>{xml_str}</source>")


class TestSetTargetWithInlineCodes:
    def test_plain_text_no_codes(self):
        source = _make_source("Hello World")
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(source, target, "Привет мир")

        assert target.text == "Привет мир"
        assert len(list(target)) == 0

    def test_single_g_tag(self):
        source = _make_source('<g id="1">Warning</g> text')
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(source, target, "Предупреждение текст")

        children = list(target)
        assert len(children) == 1
        assert children[0].tag.endswith("g")
        assert children[0].get("id") == "1"

    def test_g_tag_preserves_attributes(self):
        source = _make_source('<g id="5" ctype="bold">Important</g>')
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(source, target, "Важно")

        child = list(target)[0]
        assert child.get("id") == "5"
        assert child.get("ctype") == "bold"

    def test_text_distribution_across_g_tags(self):
        """Text distributed proportionally among g tags."""
        source = _make_source('Prefix <g id="1">AAA</g> middle <g id="2">BBB</g> suffix')
        target = copy.deepcopy(source)

        # Original lengths: "Prefix "=7, "AAA"=3, " middle "=8, "BBB"=3, " suffix"=7
        # Total = 28
        OkapiService._set_target_with_inline_codes(source, target,
                                                    "Префикс ЦЦЦ середина ДДД суффикс")

        # 5 tokens / 28 total = ~17.8% each way
        children = list(target)
        assert len(children) == 2
        assert "ЦЦЦ" in (children[0].text or "")
        assert "ДДД" in (children[1].text or "")

    def test_text_with_remaining_words_appended(self):
        """When there are more words than positions, last gets remainder."""
        source = _make_source("Short")
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(source, target,
                                                    "Very long translated text here")
        assert len((target.text or "").split()) > 1

    def test_no_source_children_copies_text_directly(self):
        source = _make_source("")
        target = copy.deepcopy(source)
        target.text = None
        OkapiService._set_target_with_inline_codes(source, target, "Direct text")
        assert target.text == "Direct text"

    def test_x_and_bx_tags_preserved(self):
        source = _make_source('Some <x id="1"/> text <bx id="2"/> here')
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(source, target,
                                                    "Некоторый текст здесь")

        children = list(target)
        assert any(c.get("id") == "1" for c in children)
        assert any(c.get("id") == "2" for c in children)

    def test_bpt_ept_do_not_consume_translation_words(self):
        """bpt/ept/ph .text are inline code markers, not visible text.
        They must NOT consume words in the water-fill distribution,
        otherwise visible text in .tail gets 0 words when there are
        many inline code markers."""
        source = _make_source(
            '<bpt id="1">&lt;run1&gt;</bpt>'
            'Visible'
            '<ept id="1">&lt;/run1&gt;</ept>'
            ' text'
            '<ph id="2">&lt;tab/&gt;</ph>'
            ' here'
        )
        target = copy.deepcopy(source)
        # With no bpt/ept/ph fix: 3 words ÷ 6 positions ≈ 0 words each
        # → markers consume all words, visible tails get "" (bug).
        # With fix: 3 words ÷ 3 tail positions = 1 word each; markers skipped.
        OkapiService._set_target_with_inline_codes(
            source, target, "Visible text here"
        )
        children = list(target)
        # Each tail gets its word + original leading whitespace preserved
        assert children[0].tail == "Visible"   # "Visible" → leading="" + word="Visible"
        assert children[1].tail == " text"     # " text" → leading=" " + word="text"
        assert children[2].tail == " here"     # " here" → leading=" " + word="here"
        # Inline markers preserved (XML-decoded by parser)
        assert children[0].text == "<run1>"
        assert children[1].text == "</run1>"
        assert children[2].text == "<tab/>"
        # All words present in combined text
        full = "".join(target.itertext())
        for word in ("Visible", "text", "here"):
            assert word in full

    def test_no_double_space_between_adjacent_positions(self):
        """Trailing whitespace in source.text + leading whitespace in
        next child's tail must not double the separator between them
        (common when DOCX runs are fragmented at whitespace boundaries)."""
        source = _make_source('First <x id="1"/> last')
        target = copy.deepcopy(source)
        source.text = "First "
        target.text = "First "
        # source.text = "First " (trailing space), x.tail = " last" (leading space)
        # Without fix: "First " + chunk1 + " " + chunk2 = double space
        # With fix: prev_had_trailing_ws strips leading from tail
        OkapiService._set_target_with_inline_codes(source, target, "Первый последний")

        text = target.text or ""
        child = list(target)[0]
        tail = child.tail or ""

        assert "  " not in text + tail, f"Double whitespace found: text={text!r} tail={tail!r}"
        assert "Первый" in text
        assert "последний" in tail.strip()

    def test_it_tag_does_not_consume_translation_words(self):
        """<it> tags (inline text markers) must NOT consume words in the
        water-fill distribution — same as bpt/ept/ph. Their .text contains
        inline code markup, not visible text."""
        source = _make_source(
            '<it id="1">open</it>'
            'Start'
            '<it id="2">close</it>'
            ' end'
        )
        target = copy.deepcopy(source)
        OkapiService._set_target_with_inline_codes(
            source, target, "Начало конец"
        )
        children = list(target)
        # <it> .text preserved but does NOT consume words
        assert children[0].text == "open"
        assert children[1].text == "close"
        # Visible text goes to tails
        assert "Начало" in (children[0].tail or "")
        assert "конец" in (children[1].tail or "")
        # All translated words present
        full = "".join(target.itertext())
        for word in ("Начало", "конец"):
            assert word in full


# ===================================================================
# XliffUnit dataclass
# ===================================================================

class TestXliffUnit:
    def test_needs_translation_true(self):
        u = XliffUnit(id="u1", source_text="Hello")
        assert u.needs_translation is True

    def test_needs_translation_false_with_target(self):
        u = XliffUnit(id="u1", source_text="Hello", target_text="Привет")
        assert u.needs_translation is False

    def test_needs_translation_false_without_translate(self):
        u = XliffUnit(id="u1", source_text="Hello", translate=False)
        assert u.needs_translation is False

    def test_needs_translation_false_blank_source(self):
        u = XliffUnit(id="u1", source_text="   ")
        assert u.needs_translation is False

    def test_plain_text_property(self):
        u = XliffUnit(id="u1", source_text="Hello World")
        assert u.plain_text == "Hello World"


# ===================================================================
# _temp_work_dir context manager
# ===================================================================

class TestTempWorkDir:
    def test_creates_and_cleans_up(self):
        with _temp_work_dir(prefix="test_cleanup_") as d:
            assert d.exists()
            assert d.is_dir()
            assert d.name.startswith("test_cleanup_")
        assert not d.exists()

    def test_cleans_up_on_exception(self):
        dir_path: Path | None = None
        with pytest.raises(ValueError):
            with _temp_work_dir(prefix="test_exc_") as d:
                dir_path = d
                raise ValueError("test error")
        assert dir_path is not None
        assert not dir_path.exists()

    def test_yields_writable_directory(self):
        with _temp_work_dir(prefix="test_write_") as d:
            test_file = d / "test.txt"
            test_file.write_text("hello")
            assert test_file.read_text() == "hello"
