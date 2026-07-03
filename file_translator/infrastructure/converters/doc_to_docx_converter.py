"""Conversion between legacy Office formats using LibreOffice CLI."""

import logging
import subprocess
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class LibreOfficeConversionError(Exception):
    """Raised when LibreOffice conversion fails."""


class LibreOfficeConverter:
    """Converts .doc/.xls files to .docx/.xlsx via LibreOffice --headless.

    Uses a unique LibreOffice user profile per conversion to allow
    concurrent invocations without profile lock conflicts.
    """

    _FORMAT_MAP = {
        ".doc": "docx",
        ".xls": "xlsx",
    }

    CONVERSION_TIMEOUT = 120

    @staticmethod
    def convert(input_path: Path, output_dir: Path) -> Path:
        suffix = input_path.suffix.lower()
        target_ext = LibreOfficeConverter._FORMAT_MAP.get(suffix)
        if target_ext is None:
            raise LibreOfficeConversionError(
                f"Unsupported input extension: {suffix}. Supported: {list(LibreOfficeConverter._FORMAT_MAP.keys())}"
            )

        if not input_path.exists():
            raise LibreOfficeConversionError(f"Input file not found: {input_path}")

        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / f"{input_path.stem}.{target_ext}"

        profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)

            cmd = [
                "soffice",
                "--headless",
                "--norestore",
                f"-env:UserInstallation=file://{profile_dir.as_posix()}",
                "--convert-to",
                target_ext,
                "--outdir",
                output_dir.as_posix(),
                input_path.as_posix(),
            ]

            logger.info(f"Converting {suffix} to .{target_ext}: {input_path.name}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=LibreOfficeConverter.CONVERSION_TIMEOUT,
            )

            if result.returncode != 0:
                raise LibreOfficeConversionError(
                    f"LibreOffice conversion failed (exit={result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )

            if not output_path.exists():
                raise LibreOfficeConversionError(
                    f"LibreOffice reported success but output file not found: {output_path}"
                )

            logger.info(f"Conversion completed: {output_path}")
            return output_path

        except subprocess.TimeoutExpired:
            raise LibreOfficeConversionError(
                f"LibreOffice conversion timed out after {LibreOfficeConverter.CONVERSION_TIMEOUT}s"
            )
        finally:
            if profile_dir.exists():
                import shutil
                try:
                    shutil.rmtree(profile_dir)
                except Exception as e:
                    logger.warning(f"Failed to cleanup LibreOffice profile dir {profile_dir}: {e}")
