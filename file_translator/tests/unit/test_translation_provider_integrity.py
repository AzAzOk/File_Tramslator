# -*- coding: utf-8 -*-
"""Provider-level integrity tests: numeric protection and mixed-unit retry."""

import json

import pytest
from unittest.mock import AsyncMock, patch

from file_translator.domain.models import LanguageCode, TranslationBatch, TextUnit
from file_translator.infrastructure.config import LLMConfig
from file_translator.infrastructure.providers.openai_provider import OpenAITranslationProvider

PH = "\u27e6NUM{}\u27e7"


def _provider():
    return OpenAITranslationProvider(
        LLMConfig(base_url="http://localhost/v1/chat/completions")
    )


def _batch(units):
    return {
        "batch": TranslationBatch(
            sequence_id=1, text_units=units,
            source_language=LanguageCode.RU, target_language=LanguageCode.EN,
        ),
        "source_language": LanguageCode.RU,
        "target_language": LanguageCode.EN,
    }


def _content(translations):
    body = json.dumps({"translations": translations}, ensure_ascii=False)
    return json.dumps({"choices": [{"message": {"content": body}}]}, ensure_ascii=False)


class _FakeResponse:
    """Minimal resp object: json() parses .text (the OpenAI envelope)."""

    status_code = 200

    def __init__(self, text):
        self.text = text

    def json(self):
        return json.loads(self.text)


async def _run(provider, batch_data, contents):
    """Patch AsyncClient so successive HTTP calls return the given contents."""
    contents = list(contents)
    counter = {"n": 0}

    async def mock_post(*args, **kwargs):
        idx = min(counter["n"], len(contents) - 1)
        counter["n"] += 1
        return _FakeResponse(contents[idx])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = mock_post

    with patch(
        "file_translator.infrastructure.providers.openai_provider.httpx.AsyncClient",
        return_value=mock_client,
    ):
        return await provider.translate_batch(batch_data)


@pytest.mark.asyncio
async def test_numeric_placeholder_restored():
    provider = _provider()
    unit = TextUnit(id="p_1", original_text="Приказ МВД РК №732")
    batch_data = _batch([unit])
    content = _content([{
        "id": "p_1",
        "text": "Order No. " + PH.format(0) + " of the Ministry",
    }])
    result = await _run(provider, batch_data, [content])
    assert result[0]["text"].count("732") == 1
    assert "№732" in result[0]["text"]


@pytest.mark.asyncio
async def test_numeric_drift_repaired():
    provider = _provider()
    unit = TextUnit(id="p_1", original_text="Приказ МВД РК №732 от 24.10.2014")
    batch_data = _batch([unit])
    content = _content([{
        "id": "p_1",
        "text": "The distance, as defined by Order No. 107 of the Ministry dated October 24, 2014",
    }])
    result = await _run(provider, batch_data, [content])
    text = result[0]["text"]
    assert "107" not in text
    assert "732" in text


@pytest.mark.asyncio
async def test_mixed_unit_retried_then_clean():
    provider = _provider()
    unit = TextUnit(
        id="p_2",
        original_text="Проект предусматривает максимально возможное использование",
    )
    batch_data = _batch([unit])
    dirty = _content([{
        "id": "p_2",
        "text": "The project предусматривает максимально возможное использование",
    }])
    clean = _content([{
        "id": "p_2",
        "text": "The project provides the maximum possible use of existing structures",
    }])
    result = await _run(provider, batch_data, [dirty, clean])
    assert any(r["id"] == "p_2" for r in result)
    assert "предусматривает" not in result[0]["text"]


@pytest.mark.asyncio
async def test_missing_units_retried_when_coverage_ok():
    provider = _provider()
    units = [
        TextUnit(id=f"u{i}", original_text=f"Достаточно длинный исходный абзац номер {i} для перевода")
        for i in range(4)
    ]
    batch_data = _batch(units)
    first = _content([
        {"id": "u0", "text": f"Sufficiently long translated paragraph number 0"},
        {"id": "u1", "text": f"Sufficiently long translated paragraph number 1"},
        {"id": "u2", "text": f"Sufficiently long translated paragraph number 2"},
        # u3 omitted by the model
    ])
    second = _content([
        {"id": "u3", "text": "Sufficiently long translated paragraph number 3"},
    ])
    result = await _run(provider, batch_data, [first, second])
    ids = {r["id"] for r in result}
    assert {"u0", "u1", "u2", "u3"} <= ids
    assert len(result) == 4


@pytest.mark.asyncio
async def test_pure_russian_retried_then_clean():
    provider = _provider()
    unit = TextUnit(
        id="p_4",
        original_text="Для обеспечения речевого оповещения используется трансляционный усилитель мощности",
    )
    batch_data = _batch([unit])
    dirty = _content([{
        "id": "p_4",
        "text": "Для обеспечения речевого оповещения используется трансляционный усилитель мощности",
    }])
    clean = _content([{
        "id": "p_4",
        "text": "A broadcast power amplifier is used for voice warning.",
    }])
    result = await _run(provider, batch_data, [dirty, clean])
    assert any(r["id"] == "p_4" for r in result)
    assert "оповещения" not in result[0]["text"]


@pytest.mark.asyncio
async def test_mixed_unit_falls_back_to_source_when_retry_dirty():
    provider = _provider()
    unit = TextUnit(
        id="p_3",
        original_text="Проект предусматривает максимально возможное использование",
    )
    batch_data = _batch([unit])
    dirty = _content([{
        "id": "p_3",
        "text": "The project предусматривает максимально возможное использование",
    }])
    result = await _run(provider, batch_data, [dirty, dirty])
    assert result[0]["id"] == "p_3"
    # Source fallback: content preserved (no loss), integrity recorded.
    assert "предусматривает" in result[0]["text"]
    assert batch_data.get("_integrity_missing", []) != []