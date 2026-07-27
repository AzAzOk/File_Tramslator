"""ODAFileConverter service — converts DWG ↔ DXF.

Two modes:
  1. HTTP mode: calls oda-converter HTTP API (Docker). Set ODA_CONVERTER_URL.
  2. Local mode: subprocess call to ODAFileConverter CLI (dev/Windows).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_ODA_CONVERTER_URL = os.environ.get("ODA_CONVERTER_URL")

# Default install paths per platform
_DEFAULT_PATHS: dict[str, str] = {
    # "win32": r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    "linux": "/usr/bin/ODAFileConverter",
}

_ODA_VERSION = "ACAD2013"
_SHARED_DATA = Path("/data")


def _find_converter() -> str | None:
    """Locate the ODAFileConverter executable (local mode)."""
    default = _DEFAULT_PATHS.get(sys.platform)
    if default and Path(default).exists():
        return default

    alternatives = [
        # r"C:\Program Files\ODA\ODAFileConverter 26.9.0\ODAFileConverter.exe",
        "/usr/local/bin/ODAFileConverter",
        "/usr/bin/odafc",
    ]
    for alt in alternatives:
        if Path(alt).exists():
            return alt

    try:
        cmd = "where" if sys.platform == "win32" else "which"
        result = subprocess.run(
            [cmd, "ODAFileConverter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass

    return None


def is_available() -> bool:
    """Check if ODAFileConverter is reachable (HTTP or local).

    In HTTP mode (ODA_CONVERTER_URL set), skips the health check —
    the caller already handles conversion failures gracefully via
    ``dwg_to_dxf()`` / ``dxf_to_dwg()`` which return ``None`` / ``False``.
    The pre-check was causing false negatives (service restarting,
    temporary blip) that blocked all DWG processing unnecessarily.
    """
    if _ODA_CONVERTER_URL:
        return True
    return _find_converter() is not None


def _convert_via_http(
    input_path: Path,
    output_path: Path,
    output_format: Literal["DXF", "DWG"],
    timeout: int = 120,
) -> bool:
    """Convert via oda-converter HTTP API (requires shared /data volume)."""
    pid = os.getpid()
    input_name = f"input_{pid}_{input_path.stem}{input_path.suffix}"
    output_name = f"output_{pid}_{output_path.stem}{output_path.suffix}"
    input_data = _SHARED_DATA / input_name
    output_data = _SHARED_DATA / output_name

    try:
        _SHARED_DATA.mkdir(parents=True, exist_ok=True)
        for p in (input_data, output_data):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        shutil.copy2(input_path, input_data)

        body = json.dumps({
            "input_path": input_name,
            "output_path": output_name,
            "output_format": output_format,
            "version": _ODA_VERSION,
        }).encode()

        req = urllib.request.Request(
            f"{_ODA_CONVERTER_URL}/convert",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        logger.info(
            "ODA HTTP convert: %s -> %s (%s)", input_path.name, output_path.name, output_format
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                if output_data.exists():
                    shutil.copy2(output_data, output_path)
                    logger.info("ODA HTTP conversion successful: %s", output_path.name)
                    return True
                logger.error("ODA HTTP: output file not found after conversion: %s", output_data)
            else:
                logger.error("ODA HTTP conversion failed: %s", result.get("error"))
            return False
    except urllib.error.HTTPError as e:
        logger.error("ODA HTTP error (%d): %s", e.code, e.read().decode())
        return False
    except Exception as e:
        logger.error("ODA HTTP exception: %s", e)
        return False
    finally:
        for p in (input_data, output_data):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _run_converter(
    input_dir: Path,
    output_dir: Path,
    output_format: Literal["DXF", "DWG"],
    version: str,
    input_name: str,
    timeout: int = 120,
) -> bool:
    """Execute ODAFileConverter subprocess (local mode)."""
    converter = _find_converter()
    if converter is None:
        raise RuntimeError(
            "ODAFileConverter not found. "
            "Install from https://www.opendesign.com/guestfiles/oda_file_converter"
        )

    cmd = [
        converter,
        str(input_dir),
        str(output_dir),
        version,
        output_format,
        "0",
        "1",
        input_name,
    ]

    logger.info("ODA local convert: %s -> %s (%s)", input_name, output_format, version)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            logger.info("ODA local conversion successful (rc=0)")
            return True
        logger.error(
            "ODA local conversion failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ODA local conversion timed out after %ds", timeout)
        return False
    except FileNotFoundError:
        logger.error("ODAFileConverter executable not found: %s", converter)
        return False


def dwg_to_dxf(dwg_path: Path, timeout: int = 0) -> Path | None:
    """Convert .dwg to temp .dxf. Returns temp path or None.

    *timeout*: seconds. 0 (default) = auto-scale: 120 s base + 1 s per 4 MB of input.
    """
    if timeout <= 0:
        size_mb = dwg_path.stat().st_size / (1024 * 1024) if dwg_path.exists() else 0
        timeout = max(300, int(120 + size_mb / 4))
        logger.info(
            "dwg_to_dxf auto-timeout: %.0f MB -> %ds", size_mb, timeout,
        )
    if _ODA_CONVERTER_URL:
        tmp_dir = Path(tempfile.mkdtemp(prefix="oda_dwg_to_dxf_"))
        dxf_path = tmp_dir / f"{dwg_path.stem}.dxf"
        success = _convert_via_http(dwg_path, dxf_path, "DXF", timeout)
        if success and dxf_path.exists():
            return dxf_path
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="oda_dwg_to_dxf_"))
    dxf_name = dwg_path.stem + ".dxf"
    output_path = tmp_dir / dxf_name

    success = _run_converter(
        input_dir=dwg_path.parent.resolve(),
        output_dir=tmp_dir.resolve(),
        output_format="DXF",
        version=_ODA_VERSION,
        input_name=dwg_path.name,
        timeout=timeout,
    )

    if success and output_path.exists():
        logger.info("DWG -> DXF: %s -> %s", dwg_path.name, output_path)
        return output_path
    if output_path.exists():
        logger.warning("DWG -> DXF returned non-zero but output exists: %s", output_path)
        return output_path

    logger.error("DWG -> DXF failed for %s", dwg_path)
    os.rmdir(tmp_dir)
    return None


def dxf_to_dwg(dxf_path: Path, output_dwg: Path, timeout: int = 0) -> bool:
    """Convert .dxf to .dwg. Returns True on success.

    Uses separate temp directories for input and output so that ODAFileConverter
    only processes the single DXF file (not every file in the original directory).

    *timeout*: seconds. 0 (default) = auto-scale: 120 s base + 1 s per 4 MB of input.
    """
    if timeout <= 0:
        size_mb = dxf_path.stat().st_size / (1024 * 1024) if dxf_path.exists() else 0
        timeout = max(300, int(120 + size_mb / 4))
        logger.info(
            "dxf_to_dwg auto-timeout: %.0f MB -> %ds", size_mb, timeout,
        )
    if _ODA_CONVERTER_URL:
        return _convert_via_http(dxf_path, output_dwg, "DWG", timeout)

    tmp_input = Path(tempfile.mkdtemp(prefix="oda_dxf_to_dwg_in_"))
    tmp_output = Path(tempfile.mkdtemp(prefix="oda_dxf_to_dwg_out_"))
    try:
        staged = tmp_input / dxf_path.name
        shutil.copy2(dxf_path, staged)

        success = _run_converter(
            input_dir=tmp_input.resolve(),
            output_dir=tmp_output.resolve(),
            output_format="DWG",
            version=_ODA_VERSION,
            input_name=dxf_path.name,
            timeout=timeout,
        )

        expected = tmp_output / (dxf_path.stem + ".dwg")
        if success and expected.exists():
            output_dwg.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(expected), str(output_dwg))
            logger.info("DXF -> DWG: %s -> %s", dxf_path.name, output_dwg)
            return True
        if expected.exists():
            logger.warning("DXF -> DWG returned non-zero but output exists: %s", expected)
            output_dwg.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(expected), str(output_dwg))
            return True

        logger.error("DXF -> DWG failed for %s", dxf_path)
        return False
    finally:
        shutil.rmtree(tmp_input, ignore_errors=True)
        shutil.rmtree(tmp_output, ignore_errors=True)
