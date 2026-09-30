"""Support-attachment validation helpers (pure, testable).

Support messages (MySQL ``glossary.feedback``) may carry up to
``MAX_ATTACHMENTS`` raster images of up to ``MAX_IMAGE_BYTES`` each. Validation
is content-based (magic bytes), never extension/Content-Type trust.
"""

from __future__ import annotations

MAX_ATTACHMENTS = 10
MAX_IMAGE_BYTES = 1 * 1024 * 1024  # 1 MB


def detect_image_type(data: bytes) -> str | None:
    """Return a raster-image MIME type if ``data`` is a real PNG/JPEG/WebP/GIF,
    otherwise ``None`` (rejects everything else, including SVG)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None