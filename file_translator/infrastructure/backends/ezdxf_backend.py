"""EzdxfBackend — CADBackend implementation via the ezdxf library."""

from __future__ import annotations

import logging
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Iterator

import ezdxf
from ezdxf.document import Drawing
from ezdxf.proxygraphic import load_proxy_graphic

from file_translator.infrastructure.backends.cad_backend import CADBackend
from file_translator.infrastructure.services.oda_converter_service import (
    dwg_to_dxf,
    is_available as oda_available,
)

logger = logging.getLogger(__name__)

_Y_TOLERANCE = 5.0


class EzdxfBackend(CADBackend):
    """Read and write DXF files through ezdxf.

    Supports .dwg transparently via ODAFileConverter (if installed).
    """

    def __init__(self) -> None:
        self._temp_files: dict[str, Path] = {}  # original_path -> temp_dxf_path

    # ── CADBackend interface ──

    def open(self, path: Path) -> Drawing:
        path = Path(path)
        if path.suffix.lower() == ".dwg":
            dxf_path = self._convert_dwg_to_dxf(path)
            if dxf_path is None:
                raise RuntimeError(
                    f"Cannot open .dwg file — ODAFileConverter conversion failed: {path}"
                )
            logger.info("Opened %s via temp DXF: %s", path.name, dxf_path.name)
            return ezdxf.readfile(str(dxf_path))
        return ezdxf.readfile(str(path))

    def _convert_dwg_to_dxf(self, dwg_path: Path) -> Path | None:
        """Convert a .dwg file to a temp .dxf and return the temp path."""
        if not oda_available():
            raise RuntimeError(
                "ODAFileConverter is not installed. "
                "Cannot open .dwg files. "
                "Install from https://www.opendesign.com/guestfiles/oda_file_converter"
            )
        dxf_path = dwg_to_dxf(dwg_path)
        if dxf_path is not None:
            self._temp_files[str(dwg_path)] = dxf_path
        return dxf_path

    _SKIP_BLOCKS = {"*Model_Space", "*Paper_Space"}

    def iter_entities(self, doc: Drawing) -> Iterator[tuple[Any, str]]:
        """Yield ``(entity, source)`` for every text-bearing entity.

        *source* indicates origin: ``"modelspace"``, ``"paperspace"``,
        ``"block:{name}"``, or with a ``|table`` suffix.
        """
        yield from self._iter_layout(doc.modelspace(), "modelspace")
        ps = doc.paperspace()
        yield from self._iter_layout(ps, "paperspace")
        for block in doc.blocks:
            if block.name in self._SKIP_BLOCKS:
                continue
            yield from self._iter_layout(block, f"block:{block.name}")

    def _iter_layout(
        self, layout: Any, source: str
    ) -> Iterator[tuple[Any, str]]:
        for entity in layout:
            dxf_type = entity.dxftype()

            if dxf_type in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                yield (entity, source)
                continue

            if dxf_type == "DIMENSION":
                override = entity.get_dxf_attrib("text", "").strip()
                if override and override not in ("", " ", "0"):
                    yield (entity, source)
                continue

            if dxf_type == "ACAD_TABLE":
                yield from self._iter_acad_table(entity, source)
                continue

            if dxf_type == "INSERT":
                for attrib in entity.attribs:
                    yield (attrib, f"{source}|insert")

    def _iter_acad_table(
        self, entity: Any, source: str
    ) -> Iterator[tuple[Any, str]]:
        """Extract text from ACAD_TABLE via proxy graphic data."""
        try:
            data = load_proxy_graphic(entity.tags, 160, 310)
            if data is None:
                return
            from ezdxf.proxygraphic import ProxyGraphic
            pg = ProxyGraphic(data)
            for ve in pg.virtual_entities():
                if ve.dxftype() in ("TEXT", "MTEXT"):
                    text = ve.dxf.text if hasattr(ve.dxf, "text") else ""
                    if text.strip():
                        yield (ve, f"{source}|table_proxy")
        except Exception as exc:
            logger.debug("ACAD_TABLE proxy parse failed: %s", exc)

    def get_text(self, entity: Any) -> str:
        dxf_type = entity.dxftype() if hasattr(entity, "dxftype") else "PROXY"
        try:
            if dxf_type == "MTEXT":
                return entity.text or ""
            if dxf_type == "DIMENSION":
                return str(entity.get_dxf_attrib("text", ""))
            if dxf_type in ("TEXT", "ATTRIB", "ATTDEF"):
                return entity.dxf.text or ""
            if dxf_type == "PROXY":
                return getattr(entity, "text", "")
        except Exception:
            pass
        return ""

    def set_text(self, entity: Any, text: str) -> None:
        dxf_type = entity.dxftype() if hasattr(entity, "dxftype") else "PROXY"
        try:
            if dxf_type == "MTEXT":
                entity.text = text
            elif dxf_type in ("TEXT", "ATTRIB", "ATTDEF"):
                entity.dxf.text = text
            elif dxf_type == "DIMENSION":
                override = entity.get_dxf_attrib("text", "").strip()
                if override:
                    entity.dxf.dim_text_override = text
        except Exception:
            pass

    def get_handle(self, entity: Any) -> str:
        if hasattr(entity, "dxf") and hasattr(entity.dxf, "handle"):
            return entity.dxf.handle
        return str(id(entity))

    def get_layer(self, entity: Any) -> str:
        if hasattr(entity, "dxf") and hasattr(entity.dxf, "layer"):
            return entity.dxf.layer
        return "0"

    def get_entity_count(self, doc: Drawing) -> int:
        return sum(1 for _ in self.iter_entities(doc))

    def save(self, doc: Drawing, path: Path) -> Path:
        path = Path(path)
        if path.suffix.lower() == ".dwg":
            tmp_dxf = path.with_suffix(".dxf")

            # ── Diagnostic: pre-saveas snapshot ──
            try:
                _disk_before = shutil.disk_usage(str(tmp_dxf.parent))
                _disk_free_mb = _disk_before.free / (1024 * 1024)
                _disk_total_mb = _disk_before.total / (1024 * 1024)
                _disk_pct = (_disk_before.free / _disk_before.total * 100) if _disk_before.total else 0
            except Exception:
                _disk_free_mb = _disk_total_mb = _disk_pct = -1
            try:
                _entity_count = len(list(doc.entitydb))
            except Exception:
                _entity_count = -1
            _saveas_start = time.time()
            logger.info(
                "Attempting doc.saveas() for %s: entity_count=%d, "
                "disk_free=%.0f MB (%.1f%% of %.0f MB)",
                tmp_dxf.name, _entity_count,
                _disk_free_mb, _disk_pct, _disk_total_mb,
            )
            # ── end pre-saveas snapshot ──

            try:
                doc.saveas(str(tmp_dxf))
            except BaseException as exc:
                _saveas_elapsed = time.time() - _saveas_start
                logger.error(
                    "doc.saveas() failed for %s after %.1fs: %s (%s)\n%s",
                    tmp_dxf.name, _saveas_elapsed,
                    exc, type(exc).__name__, traceback.format_exc(),
                )
                raise
            _saveas_elapsed = time.time() - _saveas_start

            # ── Diagnostic: post-saveas snapshot ──
            try:
                _disk_after = shutil.disk_usage(str(tmp_dxf.parent))
                _disk_after_free_mb = _disk_after.free / (1024 * 1024)
                _disk_delta_mb = _disk_free_mb - _disk_after_free_mb if _disk_free_mb >= 0 else -1
            except Exception:
                _disk_after_free_mb = _disk_delta_mb = -1
            logger.info(
                "doc.saveas() completed for %s in %.1fs: "
                "disk_used_delta=%.0f MB, disk_free_after=%.0f MB",
                tmp_dxf.name, _saveas_elapsed,
                _disk_delta_mb, _disk_after_free_mb,
            )
            # ── end post-saveas snapshot ──

            if not tmp_dxf.exists():
                logger.error(
                    "DXF intermediate not created for %s: "
                    "entity_count=%d, saveas_time=%.1fs, "
                    "disk_free_before=%.0f MB, disk_free_after=%.0f MB",
                    path.name, _entity_count, _saveas_elapsed,
                    _disk_free_mb, _disk_after_free_mb,
                )
                raise FileNotFoundError(
                    f"doc.saveas() did not produce {tmp_dxf} "
                    f"(entities={_entity_count}, saveas_time={_saveas_elapsed:.1f}s)"
                )
            dxf_size_mb = tmp_dxf.stat().st_size / (1024 * 1024)
            logger.info(
                "Intermediate DXF saved: %s (%.1f MB) — converting to DWG",
                tmp_dxf.name, dxf_size_mb,
            )
            from file_translator.infrastructure.services.oda_converter_service import dxf_to_dwg
            if not dxf_to_dwg(tmp_dxf, path):
                logger.warning(
                    "DWG conversion failed for %s (%.1f MB DXF) — falling back to DXF output",
                    path.name, dxf_size_mb,
                )
                return tmp_dxf
            tmp_dxf.unlink(missing_ok=True)
            return path
        doc.saveas(str(path))
        return path

    def close(self, doc: Drawing) -> None:
        pass

    def cleanup_temp(self, original_path: str | Path) -> None:
        """Remove the temp DXF created for a .dwg file."""
        key = str(Path(original_path))
        tmp = self._temp_files.pop(key, None)
        if tmp:
            tmp.unlink(missing_ok=True)
            try:
                os.rmdir(tmp.parent)
            except OSError:
                pass
