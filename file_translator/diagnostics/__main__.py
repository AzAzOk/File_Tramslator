"""CLI entrypoint for the diagnostics package.

Usage::

    python -m file_translator.diagnostics render <docx> --out <dir> [--dpi N]
    python -m file_translator.diagnostics render-pair <orig> <trans> --out <dir>
    python -m file_translator.diagnostics scan <original> <translated> [--render-dir <dir>]
    python -m file_translator.diagnostics leaks <translated> [--whitelist prefix...]
    python -m file_translator.diagnostics glue <translated>
    python -m file_translator.diagnostics locate <fragment> --xliff <dir>

Command help via ``python -m file_translator.diagnostics <cmd> --help``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_render(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.render import render_docx

    result = render_docx(args.docx, args.out, dpi=args.dpi, timeout_seconds=args.timeout)
    print(json.dumps({
        "source": result.source,
        "pdf": result.pdf_path,
        "png_count": len(result.png_paths),
        "page_text": result.page_text_path,
        "page_count": result.page_count,
        "engine": result.engine,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_render_pair(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.render import render_pair

    results = render_pair(
        args.original, args.translated, args.out,
        dpi=args.dpi, timeout_seconds=args.timeout,
    )
    print(json.dumps({
        "original": {
            "page_count": results["original"].page_count,
            "png_count": len(results["original"].png_paths),
            "page_text": results["original"].page_text_path,
        },
        "translated": {
            "page_count": results["translated"].page_count,
            "png_count": len(results["translated"].png_paths),
            "page_text": results["translated"].page_text_path,
        },
        "page_delta": results["translated"].page_count - results["original"].page_count,
    }, ensure_ascii=False, indent=2))
    # Exit non-zero when page counts drift (flag, not hard gate — caller decides).
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.render import convert_to_pdf, render_pdf
    from file_translator.diagnostics.structure_scanner import compare_structures, profile_docx

    origin = args.source_origin or (
        "pdf_converted" if str(args.translated).lower().endswith(".pdf") else "native"
    )
    orig_pages = trans_pages = None

    if args.render_dir:
        # Render both files to PDF to obtain page counts (render loop handles
        # DOCX; a PDF input is used as its own PDF — no conversion needed).
        render_dir = Path(args.render_dir)
        pdf_map = {}
        for label, path in (("original", args.original), ("translated", args.translated)):
            if str(path).lower().endswith(".pdf"):
                pdf_map[label] = str(path)
            else:
                pdf_map[label] = str(convert_to_pdf(Path(path), render_dir))
        for label, pdf in pdf_map.items():
            _png, _ptxt, cnt = render_pdf(Path(pdf), render_dir)
            if label == "original":
                orig_pages = cnt
            else:
                trans_pages = cnt

    orig = profile_docx(args.original, source_origin="native", pages=orig_pages)
    trans = profile_docx(args.translated, source_origin=origin, pages=trans_pages)
    report = compare_structures(orig, trans)

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    # Hard failures → exit 1 so CI/scripts can gate on them.
    return 1 if report.hard_failures else 0


def _cmd_leaks(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.leak_scanner import scan_docx_leaks

    findings = scan_docx_leaks(args.docx, whitelist_prefixes=args.whitelist or None)
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 1 if findings else 0


def _cmd_glue(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.glue_scanner import scan_docx_glued_words

    findings = scan_docx_glued_words(args.docx)
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 1 if findings else 0


def _cmd_locate(args: argparse.Namespace) -> int:
    from file_translator.diagnostics.unit_locator import locate_fragment_in_xliff_dir

    units = locate_fragment_in_xliff_dir(args.fragment, args.xliff)
    print(json.dumps(units, ensure_ascii=False, indent=2))
    return 1 if not units else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m file_translator.diagnostics",
        description="Structure-fidelity diagnostics (host-side tooling).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render", help="Render one DOCX to PNG pages + text")
    p_render.add_argument("docx", help="Path to the DOCX file")
    p_render.add_argument("--out", required=True, help="Output directory")
    p_render.add_argument("--dpi", type=int, default=110)
    p_render.add_argument("--timeout", type=int, default=600, dest="timeout")
    p_render.set_defaults(func=_cmd_render)

    p_pair = sub.add_parser("render-pair", help="Render original + translated pair")
    p_pair.add_argument("original")
    p_pair.add_argument("translated")
    p_pair.add_argument("--out", required=True)
    p_pair.add_argument("--dpi", type=int, default=110)
    p_pair.add_argument("--timeout", type=int, default=600, dest="timeout")
    p_pair.set_defaults(func=_cmd_render_pair)

    p_scan = sub.add_parser("scan", help="Compare structural invariants of a pair")
    p_scan.add_argument("original")
    p_scan.add_argument("translated")
    p_scan.add_argument("--render-dir", default=None,
                       help="Directory to render PDFs for page-count comparison")
    p_scan.add_argument("--source-origin", default=None, dest="source_origin",
                       help="native (default) or pdf_converted")
    p_scan.set_defaults(func=_cmd_scan)

    p_leaks = sub.add_parser("leaks", help="Detect leftover source-language text")
    p_leaks.add_argument("docx")
    p_leaks.add_argument("--whitelist", action="append", default=None,
                        help="Extra normative designation prefixes")
    p_leaks.set_defaults(func=_cmd_leaks)

    p_glue = sub.add_parser("glue", help="Detect glued words (missing spaces)")
    p_glue.add_argument("docx")
    p_glue.set_defaults(func=_cmd_glue)

    p_loc = sub.add_parser("locate", help="Map fragment to XLIFF trans-units")
    p_loc.add_argument("fragment")
    p_loc.add_argument("--xliff", required=True,
                       help="Directory containing retained XLIFF files")
    p_loc.set_defaults(func=_cmd_locate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())