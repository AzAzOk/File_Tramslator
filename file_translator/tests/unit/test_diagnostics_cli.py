# -*- coding: utf-8 -*-
"""CLI wiring tests for the diagnostics package.

Covers the ``scan`` command's ``--render-dir`` path: render_dir and the
docx/pdf paths must be passed as ``Path`` objects to the render helpers
(convert_to_pdf / render_pdf) — a str silently broke ``out_dir.mkdir`` /
``docx_path.stem`` when the smoke run first exercised this command.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from file_translator.diagnostics import __main__ as cli


def _run_scan(args_list: list[str], capsys) -> tuple[int, str]:
    parser = cli.build_parser()
    args = parser.parse_args(args_list)
    rc = args.func(args)
    out = capsys.readouterr().out
    return rc, out


def test_build_parser_has_all_commands(capsys):
    parser = cli.build_parser()
    for cmd in ("render", "render-pair", "scan", "leaks", "glue", "locate"):
        # argparse --help prints the subcommand help and exits 0; that exit is
        # the signal that the subcommand is registered and parseable.
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([cmd, "--help"])
        assert exc.value.code == 0
        assert cmd in capsys.readouterr().out


def test_scan_render_dir_paths_are_path_objects(tmp_path, capsys):
    """--render-dir passes Path objects to convert_to_pdf/render_pdf."""
    orig = tmp_path / "orig.docx"
    trans = tmp_path / "trans.docx"
    orig.write_bytes(b"PK")  # placeholder, profile mocked below
    trans.write_bytes(b"PK")

    seen_pdfs: list[object] = []
    with (
        mock.patch("file_translator.diagnostics.render.convert_to_pdf",
                   side_effect=lambda p, d: Path(tmp_path / f"{Path(p).stem}.pdf")) as m_conv,
        mock.patch("file_translator.diagnostics.render.render_pdf",
                   side_effect=lambda pdf, d: (["page.png"], Path("pt.jsonl"), 7)),
        mock.patch("file_translator.diagnostics.structure_scanner.profile_docx"),
        mock.patch("file_translator.diagnostics.structure_scanner.compare_structures") as m_cmp,
    ):
        class FakeReport:
            def to_dict(self):
                return {"deltas": [], "hard_failures": [], "warnings": []}
            hard_failures = []
        m_cmp.return_value = FakeReport()

        rc, out = _run_scan(["scan", str(orig), str(trans),
                             "--render-dir", str(tmp_path)], capsys)

    assert rc == 0
    assert json.loads(out)["deltas"] == []
    # render_dir was a Path when handed to convert_to_pdf
    for call in m_conv.call_args_list:
        _p, out_dir = call.args
        assert isinstance(out_dir, Path), "render_dir leaked as str into convert_to_pdf"
    assert m_conv.call_args_list[0].args[0] == orig
    assert len(m_conv.call_args_list) == 2


def test_scan_without_render_dir_skips_rendering(tmp_path, capsys):
    orig = tmp_path / "orig.docx"
    trans = tmp_path / "trans.docx"
    orig.write_bytes(b"PK")
    trans.write_bytes(b"PK")

    with (
        mock.patch("file_translator.diagnostics.render.convert_to_pdf") as m_conv,
        mock.patch("file_translator.diagnostics.render.render_pdf") as m_render,
        mock.patch("file_translator.diagnostics.structure_scanner.profile_docx"),
        mock.patch("file_translator.diagnostics.structure_scanner.compare_structures") as m_cmp,
    ):
        class FakeReport:
            def to_dict(self):
                return {"deltas": [], "hard_failures": [], "warnings": []}
            hard_failures = []
        m_cmp.return_value = FakeReport()

        rc, _out = _run_scan(["scan", str(orig), str(trans)], capsys)

    assert rc == 0
    m_conv.assert_not_called()
    m_render.assert_not_called()


def test_locate_returns_json_and_exit_code(tmp_path, capsys):
    """CLI locate: prints UnitMatch JSON; exit 0 with matches, 1 without."""
    xliff = tmp_path / "doc.xlf"
    xliff.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        "<xliff><body>"
        '<trans-unit id="p_9"><source>Привет мир</source></trans-unit>'
        '<trans-unit id="p_12"><source>Другой абзац</source>'
        "<target>Another paragraph</target></trans-unit>"
        "</body></xliff>",
        encoding="utf-8",
    )
    parser = cli.build_parser()

    args = parser.parse_args(["locate", "Привет", "--xliff", str(xliff)])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out[0]["unit_id"] == "p_9"
    assert out[0]["is_translated"] is False

    args = parser.parse_args(["locate", "NotThere", "--xliff", str(xliff)])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out) == []