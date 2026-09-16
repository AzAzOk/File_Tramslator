"""Automation layer: drives the First PDF GUI via UI Automation.

Recipe provenance: spike script `firstpdf_convert.ps1` (2026-09-08) validated
against First PDF 6.4 on this host. Selectors (AutomationIds `MainForm`,
`CustomPath`, `TB_CustomPath`, `Button_Convertion`) are pinned to that build --
if a First PDF update renames controls, this module is the only place to change.

The output-format combo (`CBConvertionDirection`) is intentionally left
untouched: its default is Word and its list items are unnamed in this build.

Reliability notes learned from testing (2026-09-08):
- Acting on the window immediately after launch races UI initialization; a
  settle delay + read-back verification of every control change is required.
- If `CustomPath` is off at click time, First PDF silently writes the DOCX next
  to the *source* file instead of the requested output dir. We therefore verify
  the checkbox state and the output-path textbox value before clicking Convert.
- First PDF exits by itself after a successful conversion, so `proc.poll()` is
  only treated as a failure if no DOCX appeared anywhere we control.
"""

from __future__ import annotations

import os
import subprocess
import time

from pywinauto import Application
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

from .paths import ensure_long_path

_SETTLE_SECONDS = 2.0


class ConvertError(Exception):
    """Base class for conversion failures."""


class ConverterStartError(ConvertError):
    """First PDF could not be started."""


class ConverterFailed(ConvertError):
    """First PDF started but the conversion produced no valid output."""


class ConvertTimeout(ConvertError):
    """Conversion did not finish within the configured timeout."""


def _find_output_docx(output_dir: str) -> str | None:
    """Return the first non-empty *.docx in output_dir, or None."""
    for name in sorted(os.listdir(output_dir)):
        if name.lower().endswith(".docx"):
            full = os.path.join(output_dir, name)
            if os.path.getsize(full) > 0:
                return full
    return None


def _toggle_state(checkbox) -> int:
    """Return the UIA ToggleState (0=off, 1=on, 2=indeterminate)."""
    return checkbox.get_toggle_state()


def _set_custom_path(main, output_dir: str, attempts: int = 3) -> None:
    """Ensure `CustomPath` is ON and `TB_CustomPath` actually holds output_dir.

    Raises ConverterFailed if the controls cannot be driven to the desired
    state. Read-back verification guards against the silent-wrong-folder bug.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            custom = main.child_window(auto_id="CustomPath")
            custom.wait("exists enabled", timeout=10)
            if _toggle_state(custom) != 1:
                custom.toggle()
            time.sleep(0.5)

            tb = main.child_window(auto_id="TB_CustomPath")
            tb.wait("exists enabled", timeout=10)
            tb.set_edit_text(output_dir)
            time.sleep(0.5)

            value = tb.get_value()
            state = _toggle_state(custom)
            if value == output_dir and state == 1:
                return
            last_error = RuntimeError(
                f"Custom path not applied (attempt {attempt}): "
                f"checkbox={state} value={value!r}"
            )
        except Exception as exc:  # noqa: BLE001 - one bad attempt must not abort the loop
            last_error = exc
        time.sleep(0.5)

    raise ConverterFailed(f"Could not configure First PDF output folder: {last_error}")


def _kill_first_pdf(exe_path: str) -> None:
    """Terminate the First PDF process tree we spawned (spike recipe used
    `Stop-Process -Name "First PDF"`; taskkill /T also kills the GUI's
    children). Never raises."""
    exe_name = os.path.basename(exe_path)
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", exe_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except OSError:
        pass


def convert_pdf(pdf_path: str, output_dir: str, exe_path: str, timeout: float) -> str:
    """Convert `pdf_path` to DOCX through First PDF and return the output path.

    Raises ConverterStartError / ConverterFailed / ConvertTimeout.
    """
    # First PDF silently ignores 8.3 short paths. Create the dir first so
    # GetLongPathNameW can resolve it, then drive the GUI with the long form.
    os.makedirs(output_dir, exist_ok=True)
    output_dir = ensure_long_path(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.isfile(exe_path):
        raise ConverterStartError(f"First PDF executable not found: {exe_path}")

    try:
        proc = subprocess.Popen([exe_path, pdf_path])
    except OSError as exc:
        raise ConverterStartError(f"Failed to launch First PDF: {exc}") from exc

    try:
        # 1) Connect to the main window and let the UI settle.
        try:
            app = Application(backend="uia").connect(path=exe_path, timeout=30)
            main = app.window(auto_id="MainForm")
            main.wait("exists visible", timeout=30)
        except (ElementNotFoundError, PywinautoTimeoutError, RuntimeError) as exc:
            if proc.poll() is not None:
                raise ConverterStartError(
                    f"First PDF exited with code {proc.returncode} before showing its window"
                ) from exc
            raise ConverterStartError(f"Could not find First PDF window: {exc}") from exc
        time.sleep(_SETTLE_SECONDS)

        # 2) Enable the custom output folder and point it at output_dir
        #    (verified by read-back, see module docstring).
        _set_custom_path(main, output_dir)

        # 3) Start the conversion (combo left on its default: Word).
        convert_btn = main.child_window(auto_id="Button_Convertion")
        convert_btn.wait("exists enabled", timeout=10)
        try:
            convert_btn.invoke()
        except Exception:  # noqa: BLE001 - fall back to a mouse click
            convert_btn.click()

        # 4) Poll for the output document.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _find_output_docx(output_dir):
                break
            if proc.poll() is not None:
                raise ConverterFailed(
                    f"First PDF exited with code {proc.returncode} without producing a DOCX"
                )
            time.sleep(0.5)
        else:
            raise ConvertTimeout(
                f"No DOCX appeared in {output_dir} within {timeout:g} s"
            )

        out_path = _find_output_docx(output_dir)
        if out_path is None:
            raise ConverterFailed("Conversion finished but no DOCX file was found")
        return out_path
    finally:
        _kill_first_pdf(exe_path)