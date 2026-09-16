"""Windows path helpers.

First PDF 6.4 silently ignores *short* (8.3) paths in its "custom path"
textbox and writes the output next to the source instead. `%TEMP%` on this
host resolves to `C:\\Users\\KULIKO~1\\...`, so every path we hand to the GUI
must be expanded to its long form first.
"""

from __future__ import annotations

import os


def ensure_long_path(path: str) -> str:
    """Expand 8.3 short-name components to long names (no-op on failure)."""
    if not path or os.name != "nt":
        return path
    path = os.path.abspath(path)
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, len(buf))
        if n and 0 < n < len(buf):
            return buf.value
    except Exception:  # noqa: BLE001 - best effort only
        pass
    return path