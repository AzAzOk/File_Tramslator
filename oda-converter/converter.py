"""ODAFileConverter wrapper — converts DWG ↔ DXF.

Uses ``subprocess`` to call ODAFileConverter CLI.
Works both locally (Windows) and via the ODA converter service (Linux/Docker).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Default install paths per platform
_DEFAULT_PATHS: dict[str, str] = {
    "win32": r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    "linux": "/usr/bin/ODAFileConverter",
}

_ODA_VERSION = "ACAD2013"


def _find_converter() -> str | None:
    """Locate the ODAFileConverter executable."""
    # Try platform-specific default
    default = _DEFAULT_PATHS.get(sys.platform)
    if default and Path(default).exists():
        return default

    # Try common alternatives
    alternatives = [
        r"C:\Program Files\ODA\ODAFileConverter 26.9.0\ODAFileConverter.exe",
        "/usr/local/bin/ODAFileConverter",
        "/usr/bin/odafc",
    ]
    for alt in alternatives:
        if Path(alt).exists():
            return alt

    # Try PATH
    try:
        result = subprocess.run(
            ["where" if sys.platform == "win32" else "which", "ODAFileConverter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass

    return None


def is_available() -> bool:
    """Check if ODAFileConverter is installed."""
    return _find_converter() is not None


def convert(
    input_path: Path,
    output_path: Path,
    output_format: Literal["DXF", "DWG"] = "DXF",
    version: str = _ODA_VERSION,
    timeout: int = 120,
) -> bool:
    converter = _find_converter()
    if converter is None:
        raise RuntimeError(
            "ODAFileConverter not found. "
            "Install from https://www.opendesign.com/guestfiles/oda_file_converter"
        )

    input_path = Path(input_path)
    output_path = Path(output_path)

    input_dir = input_path.parent.resolve()
    output_dir = output_path.parent.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_name = input_path.name

    if output_path.is_dir():
        shutil.rmtree(output_path)

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

    # On Linux, wrap with xvfb-run to provide a virtual display for Qt/ODAFileConverter.
    # The ODA .deb bundles Qt6 with only the xcb plugin (no offscreen), so a real
    # X server (even virtual) is required.  Using xvfb-run per-call is more reliable
    # than a long-lived background Xvfb process.
    if sys.platform != "win32":
        if shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", "-s", "-ac -screen 0 1280x1024x24"] + cmd

    logger.info(
        "ODA convert: %s -> %s (%s)", input_path.name, output_path.name, output_format
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # ODAFileConverter names the output file after the INPUT stem,
        # not after our desired output_path — locate it by that convention.
        produced_ext = ".dxf" if output_format == "DXF" else ".dwg"
        produced_path = output_dir / f"{input_path.stem}{produced_ext}"

        if result.returncode == 0 and produced_path.is_file():
            if produced_path != output_path:
                produced_path.rename(output_path)
            logger.info("ODA conversion successful: %s", output_path.name)
            return True

        logger.error(
            "ODA conversion failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ODA conversion timed out after %ds", timeout)
        return False
    except FileNotFoundError:
        logger.error("ODAFileConverter executable not found: %s", converter)
        return False
