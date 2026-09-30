# -*- coding: utf-8 -*-
"""Tests for support-message image attachments (validation + schema)."""

import pytest

from file_translator.application.schemas import (
    FeedbackAttachmentSchema,
    FeedbackEntrySchema,
)
from file_translator.application.support_attachments import (
    MAX_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    detect_image_type,
)


def _png():
    return b"\x89PNG\r\n\x1a\n" + b"..." + b"PNG"


def _jpeg():
    return b"\xff\xd8\xff\xe0" + b"rest"


def _webp():
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"data"


def _gif():
    return b"GIF89a" + b"data"


class TestDetectImageType:
    @pytest.mark.parametrize("data,expected", [
        (_png(), "image/png"),
        (_jpeg(), "image/jpeg"),
        (_webp(), "image/webp"),
        (_gif(), "image/gif"),
    ])
    def test_valid_raster(self, data, expected):
        assert detect_image_type(data) == expected

    def test_rejects_text(self):
        assert detect_image_type(b"hello world") is None

    def test_rejects_svg(self):
        assert detect_image_type(b"<svg xmlns=...></svg>") is None

    def test_rejects_empty(self):
        assert detect_image_type(b"") is None


class TestLimits:
    def test_constants(self):
        assert MAX_ATTACHMENTS == 10
        assert MAX_IMAGE_BYTES == 1024 * 1024


class TestSchemas:
    def test_attachment_schema(self):
        a = FeedbackAttachmentSchema(
            id=1, feedback_id=5, position=1,
            filename="shot.png", content_type="image/png", size=123,
        )
        assert a.filename == "shot.png"

    def test_entry_default_attachments_empty(self):
        e = FeedbackEntrySchema(
            id=1, user_id="u", username="usr", message="m", created_at="2026-01-01",
        )
        assert e.attachments == []