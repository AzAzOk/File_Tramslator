"""Conversion between legacy Office formats using LibreOffice CLI."""

import logging
import shutil
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

        has_non_ascii = any(ord(c) > 127 for c in input_path.stem)
        if has_non_ascii:
            tmp_input = output_dir / f"{uuid.uuid4().hex}{suffix}"
            shutil.copy2(input_path, tmp_input)
            actual_input = tmp_input
            logger.info(f"Copied file with ASCII name for LibreOffice: {actual_input.name}")
        else:
            actual_input = input_path

        output_path = output_dir / f"{actual_input.stem}.{target_ext}"

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
                actual_input.as_posix(),
            ]

            logger.info(f"Converting {suffix} to .{target_ext}: {actual_input.name}")
            logger.info(f"LibreOffice cmd: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=LibreOfficeConverter.CONVERSION_TIMEOUT,
            )

            logger.info(f"LibreOffice returncode: {result.returncode}")
            if result.stdout.strip():
                logger.info(f"LibreOffice stdout: {result.stdout.strip()}")
            if result.stderr.strip():
                logger.info(f"LibreOffice stderr: {result.stderr.strip()}")

            if result.returncode != 0:
                raise LibreOfficeConversionError(
                    f"LibreOffice conversion failed (exit={result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )

            if not output_path.exists():
                output_files = list(output_dir.glob(f"*.{target_ext}"))
                source_files = list(actual_input.parent.glob(f"*.{target_ext}"))
                logger.warning(
                    f"Expected output not found: {output_path}\n"
                    f"  output_dir contents (*.{target_ext}): {output_files}\n"
                    f"  source_dir contents (*.{target_ext}): {source_files}"
                )
                all_candidates = output_files + source_files
                if all_candidates:
                    output_path = all_candidates[0]
                    logger.warning(f"Using fallback output: {output_path}")
                else:
                    raise LibreOfficeConversionError(
                        f"LibreOffice reported success but no .{target_ext} file found. "
                        f"Output dir: {list(output_dir.iterdir())}"
                    )

            logger.info(f"Conversion completed: {output_path}")
            return output_path

        except subprocess.TimeoutExpired:
            raise LibreOfficeConversionError(
                f"LibreOffice conversion timed out after {LibreOfficeConverter.CONVERSION_TIMEOUT}s"
            )
        finally:
            if profile_dir.exists():
                try:
                    shutil.rmtree(profile_dir)
                except Exception as e:
                    logger.warning(f"Failed to cleanup LibreOffice profile dir {profile_dir}: {e}")
