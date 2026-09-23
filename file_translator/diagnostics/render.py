"""Page render loop — DOCX → PDF → per-page PNG + per-page text with geometry.

Pipeline (host-side tooling only, not part of the running service):

1. LibreOffice headless converts the DOCX to PDF
   (``soffice --headless --norestore --convert-to pdf``).
2. PyMuPDF (``fitz``) opens the PDF, renders each page to a PNG
   (``render/<stem>__p%03d.png``), and extracts per-page text with word
   coordinates into a JSONL file (``page_text.jsonl``).

The engine is configurable; MS Word COM automation is documented as a possible
fallback but is NOT required for this change (LibreOffice is available on the
host and used by default).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# LibreOffice headless binary, discovered via PATH or standard install paths.
SOFFICE_CANDIDATES = (
    "soffice",
    "C:/Program Files/LibreOffice/program/soffice.exe",
    "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
)

# MS Word COM fallback (documented, not implemented in this change).
WORD_EXE_CANDIDATES = (
    "C:/Program Files/Microsoft Office/root/Office16/WINWORD.EXE",
)


@dataclass
class RenderResult:
    """Output of rendering one DOCX file."""

    source: str
    pdf_path: str
    png_paths: list[str]
    page_text_path: str
    page_count: int
    engine: str = "libreoffice"


def find_soffice() -> str | None:
    """Locate a usable LibreOffice ``soffice`` binary."""
    for cand in SOFFICE_CANDIDATES:
        found = shutil.which(cand) or (Path(cand).exists() and cand)
        if found:
            return found
    return None


def convert_to_pdf(docx_path: Path, out_dir: Path,
                   soffice: str | None = None,
                   timeout_seconds: int = 600) -> Path:
    """Convert a DOCX file to PDF using LibreOffice headless.

    Returns the path of the produced PDF (in ``out_dir``). Raises
    ``RuntimeError`` if conversion fails or no PDF appears.
    """
    soffice = soffice or find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice 'soffice' not found. Install LibreOffice or set "
            "PATH so `soffice` resolves (MS Word COM fallback is documented "
            "but not implemented in this change)."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    # LibreOffice needs a separate profile dir to avoid lock conflicts between
    # concurrent headless runs; use a unique per-conversion profile.
    prof = Path(tempfile.mkdtemp(prefix="lo_profile_"))
    cmd = [
        soffice,
        "--headless", "--norestore",
        f"-env:UserInstallation=file:///{prof.as_posix()}",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(docx_path),
    ]
    logger.info("LibreOffice convert: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"LibreOffice conversion timed out after {timeout_seconds}s: {docx_path}"
        ) from e
    finally:
        try:
            shutil.rmtree(prof, ignore_errors=True)
        except Exception:
            pass

    if proc.returncode != 0:
        logger.warning("soffice stderr: %s", proc.stderr[-2000:])
        raise RuntimeError(
            f"LibreOffice conversion failed (rc={proc.returncode}): {docx_path}"
        )

    pdf_path = out_dir / f"{docx_path.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError(
            f"LibreOffice reported success but no PDF at expected path: {pdf_path}"
        )
    return pdf_path


def render_pdf(pdf_path: Path, out_dir: Path, dpi: int = 110) -> tuple[list[str], Path, int]:
    """Render every PDF page to PNG and extract page text with coordinates.

    Returns ``(png_paths, page_text_jsonl, page_count)``.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PyMuPDF (fitz) is required for the render loop") from e

    out_dir.mkdir(parents=True, exist_ok=True)
    png_paths: list[str] = []
    page_text_path = out_dir / "page_text.jsonl"
    doc = fitz.open(str(pdf_path))
    try:
        with page_text_path.open("w", encoding="utf-8") as fh:
            for page_num in range(doc.page_count):
                page = doc.load_page(page_num)
                page_no = page_num + 1
                png = out_dir / f"{pdf_path.stem}__p{page_no:03d}.png"
                pix = page.get_pixmap(dpi=dpi)
                pix.save(str(png))
                png_paths.append(str(png))

                text_blocks: list[dict[str, Any]] = []
                for word in page.get_text("words"):
                    x0, y0, x1, y1, word_text, block_no, line_no, word_no = (
                        float(word[0]), float(word[1]), float(word[2]),
                        float(word[3]), str(word[4]), int(word[5]),
                        int(word[6]), int(word[7]),
                    )
                    text_blocks.append({
                        "text": word_text,
                        "x0": round(x0, 2), "y0": round(y0, 2),
                        "x1": round(x1, 2), "y1": round(y1, 2),
                        "block": block_no, "line": line_no, "word": word_no,
                    })
                rec = {
                    "page": page_no,
                    "pdf": str(pdf_path),
                    "text": page.get_text("text"),
                    "words": text_blocks,
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        doc.close()

    return png_paths, page_text_path, len(png_paths)


def render_docx(docx_path: Path | str, out_dir: Path | str,
                dpi: int = 110, timeout_seconds: int = 600) -> RenderResult:
    """Render one DOCX into per-page PNGs + page text with coordinates."""
    docx_path = Path(docx_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = convert_to_pdf(docx_path, out_dir, timeout_seconds=timeout_seconds)
    png_paths, page_text_path, page_count = render_pdf(pdf_path, out_dir, dpi=dpi)

    return RenderResult(
        source=str(docx_path),
        pdf_path=str(pdf_path),
        png_paths=png_paths,
        page_text_path=str(page_text_path),
        page_count=page_count,
    )


def render_pair(original: Path | str, translated: Path | str, out_dir: Path | str,
                dpi: int = 110, timeout_seconds: int = 600) -> dict[str, RenderResult]:
    """Render an (original, translated) pair into a shared output directory.

    The per-document outputs land in ``out_dir/<stem>/``; the pair-level page
    counts are available for the structure scanner's page-count comparison.
    """
    out_dir = Path(out_dir)
    results: dict[str, RenderResult] = {}
    for label, path in (("original", original), ("translated", translated)):
        sub_out = out_dir / Path(path).stem
        results[label] = render_docx(
            path, sub_out, dpi=dpi, timeout_seconds=timeout_seconds,
        )
    return results