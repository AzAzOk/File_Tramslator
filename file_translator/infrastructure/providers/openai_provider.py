"""OpenAI-compatible translation provider implementation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from file_translator.domain.interfaces import TranslationProvider
from file_translator.domain.models import LanguageCode, TextUnit, TranslationBatch, TranslationMode, TranslationStyle
from file_translator.domain.errors import ModelUnavailableError, TranslationError
from file_translator.infrastructure.config import LLMConfig, AppConfig, DELIVERY_RATIO_THRESHOLD, MAX_SPLIT_DEPTH

logger = logging.getLogger(__name__)


@dataclass
class _TranslationMessage:
    """Internal representation of a translation message."""
    
    id: str
    original_text: str
    context: str = ""


@dataclass
class _TranslationResponseItem:
    """Internal representation of a response item."""
    
    id: str
    translated_text: str


# Language name mapping for prompts
_LANGUAGE_NAMES = {
    LanguageCode.RU: "Russian",
    LanguageCode.EN: "English",
    LanguageCode.SR: "Serbian",
    LanguageCode.ZH: "Chinese",
}

# Temperature mapping per translation style
_STYLE_TEMPERATURES = {
    TranslationStyle.LEGAL: 0.0,
    TranslationStyle.TECHNICAL: 0.1,
    TranslationStyle.MIXED: 0.3,
}


class OpenAITranslationProvider(TranslationProvider):
    """Implementation of TranslationProvider using OpenAI-compatible API.
    
    This provider handles communication with local LLM models like
    Ollama's qwen3-based models or any OpenAI-compatible endpoint.
    
    Tag preservation (supports_tag_preservation):
    - qwen3 models reliably preserve <s1>..<sN> formatting tags
    - translategemma, gemma2, gemma3, gpt-oss variants break or ignore tags
    - Determined by CAPABLE_MODEL_PATTERNS match on model_name
    """
    
    CAPABLE_MODEL_PATTERNS = ["qwen3"]
    
    def __init__(self, config: LLMConfig):
        self.base_url = config.base_url
        self.model_name = config.model_name
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.timeout_seconds = config.timeout_seconds
    
    @property
    def supports_tag_preservation(self) -> bool:
        """True if this provider's model reliably preserves <s1>..<sN> formatting tags."""
        return any(p in self.model_name.lower() for p in self.CAPABLE_MODEL_PATTERNS)
    
    @classmethod
    def from_config(cls, config: AppConfig) -> OpenAITranslationProvider:
        """Create provider from AppConfig."""
        return cls(config.llm_config)
    
    def _resolve_temperature(self, style: TranslationStyle) -> float:
        """Pick temperature based on translation style, falling back to instance default."""
        return _STYLE_TEMPERATURES.get(style, self.temperature)

    def is_available(self) -> bool:
        """Check if the translation provider is available."""
        import urllib.request
        import urllib.error
        
        # Try several health check endpoints
        urls_to_try = [
            self.base_url.replace("/v1/chat/completions", "/api/tags"),
            self.base_url.replace("/v1/chat/completions", "/health"),
            self.base_url,
        ]
        
        for url in urls_to_try:
            try:
                request = urllib.request.Request(url, method="GET")
                response = urllib.request.urlopen(request, timeout=5)
                if response.status == 200:
                    return True
            except (urllib.error.HTTPError, urllib.error.URLError, Exception):
                continue
        
        return False
    
    async def translate_batch(self, batch_data: dict, _split_depth: int = 0) -> list[dict]:
        """Translate a batch of text units using LLM.
        
        Args:
            batch_data: Dictionary containing 'batch', 'source_lang', 'target_lang'.
            _split_depth: Internal — recursion depth for split-retry (max 3).
            
        Returns:
            List of dictionaries with 'id' and 'text' keys.
            
        Raises:
            ModelUnavailableError: If the model endpoint is not reachable.
            TranslationError: If translation fails or response is invalid.
        """
        if _split_depth > MAX_SPLIT_DEPTH:
            batch_id = batch_data.get('batch', None)
            seq = batch_id.sequence_id if batch_id else "?"
            logger.error(f"Split depth limit exceeded for batch after {MAX_SPLIT_DEPTH} retries")
            raise TranslationError(
                batch_id=str(seq),
                reason=f"Превышен лимит глубины разделения ({MAX_SPLIT_DEPTH}) — ответ модели не укладывается в лимиты",
            )
        
        batch: TranslationBatch = batch_data['batch']
        source_lang: LanguageCode = batch_data['source_language']
        target_lang: LanguageCode = batch_data['target_language']
        translation_style: TranslationStyle = batch_data.get('translation_style', TranslationStyle.TECHNICAL)
        translation_mode: TranslationMode = batch_data.get('translation_mode', TranslationMode.FULL)
        use_glossary: bool = batch_data.get('use_glossary', False)
        glossary_entries: list = batch_data.get('glossary_entries', [])
        
        logger.info(f"Translating batch {batch.sequence_id} with style: {translation_style.value}, mode: {translation_mode.value}")
        if use_glossary:
            logger.info(f"Glossary enabled: {len(glossary_entries)} entries available for this batch")
        
        # Detect if any text units contain formatting tags
        has_tags = self.supports_tag_preservation and any(
            '<s1>' in (getattr(u, 'original_text', '') or '')
            for u in batch.text_units
        )
        
        # Build the prompt for the LLM
        prompt = self._build_prompt(batch, source_lang, target_lang, translation_style, translation_mode)
        
        # Prepare API request
        api_request = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": self._system_prompt(target_lang, translation_style, translation_mode, source_lang, has_tags=has_tags, use_glossary=use_glossary, glossary_entries=glossary_entries)},
                {"role": "user", "content": prompt},
            ],
            "temperature": self._resolve_temperature(translation_style),
            "max_tokens": self.max_tokens,
            "stream": False
        }
        
        logger.info(f"Sending translation request to {self.base_url} with model {self.model_name}")
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}",
                    json=api_request,
                    headers={"Content-Type": "application/json"},
                )
                
                if response.status_code != 200:
                    logger.error(f"Ollama API error {response.status_code}: {response.text}")
                    raise ModelUnavailableError(
                        endpoint=self.base_url,
                        error_code=response.status_code
                    )
                
                # Debug: log raw response for troubleshooting
                response_text = response.text
                logger.info(f"Ollama response status: {response.status_code}, length: {len(response_text)}")
                
                # Try to parse - handle both streaming and regular JSON formats
                content = None
                
                # First try: direct JSON parsing (regular format)
                try:
                    parsed_full = response.json()
                    choices = parsed_full.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", "") or msg.get("reasoning_content", "")
                except (json.JSONDecodeError, Exception):
                    pass
                
                # Second try: streaming format - lines like "data: {...}"
                if not content:
                    for line in response_text.split('\n'):
                        stripped = line.strip()
                        if stripped.startswith('data: '):
                            data_value = stripped[6:].strip()
                            if data_value == 'null':
                                continue
                            try:
                                parsed_line = json.loads(data_value)
                                if content is None:
                                    choices = parsed_line.get("choices", [])
                                    if choices:
                                        msg = choices[0].get("message", {})
                                        if isinstance(msg, dict):
                                            content = msg.get("content", "") or msg.get("reasoning_content", "")
                            except (json.JSONDecodeError, Exception):
                                continue
                
                # Debug: show first 500 chars of response for diagnostics
                logger.info(f"Extracted content length: {len(content) if content else 0}")
                if not content and len(response_text) > 0:
                    logger.warning(f"Could not extract content. Response preview: {response_text[:300]}")
                
                # Clean markers from model output
                if content:
                    content = self._clean_model_output(content, preserve_tags=self.supports_tag_preservation)
                
                if content:
                    logger.debug(f"Content preview (first 500 chars): {content[:500]}")
                    logger.debug(f"Content last 200 chars: ...{content[-200:]}")
                
                # Try to extract JSON — find first { and last } in the content
                # This handles markdown blocks, raw JSON, or text-surrounded JSON
                first_brace = content.find('{')
                last_brace = content.rfind('}')
                if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                    content = content[first_brace:last_brace + 1]
                
                # Parse the extracted content string as JSON
                # Retry with JSON syntax fix if first attempt fails
                # If still fails, split batch in half and retry each half
                try:
                    parsed_json = json.loads(content)
                except json.JSONDecodeError:
                    content = self._fix_json_syntax(content)
                    try:
                        parsed_json = json.loads(content)
                    except json.JSONDecodeError as e:
                        # Try to extract valid JSON prefix (handles truncation)
                        fixed = self._try_extract_valid_json(content)
                        if fixed is not None:
                            recovered = json.loads(fixed)
                            recovered_count = len(recovered.get("translations", []))
                            expected_count = len(batch.text_units)
                            if recovered_count < expected_count:
                                logger.warning(
                                    f"Recovered truncated JSON for batch {batch.sequence_id}: "
                                    f"{recovered_count}/{expected_count} units — "
                                    f"LLM response was truncated, "
                                    f"missing {expected_count - recovered_count} unit(s)"
                                )
                            else:
                                logger.warning(
                                    f"Recovered truncated JSON for batch {batch.sequence_id} "
                                    f"({recovered_count} units) by trimming trailing garbage"
                                )
                            content = fixed
                            parsed_json = json.loads(content)
                        elif len(batch.text_units) > 1:
                            logger.warning(
                                f"JSON parse failed for batch {batch.sequence_id} "
                                f"({len(batch.text_units)} units), splitting and retrying..."
                            )
                            return await self._translate_with_split(batch_data, _split_depth + 1)
                        else:
                            snippet = content[:300] if content else "<empty>"
                            raise TranslationError(
                                reason=f"Invalid JSON response (single unit): {str(e)} | snippet: {snippet}"
                            ) from e
                
                translations = parsed_json.get("translations", [])
                
                if not isinstance(translations, list):
                    raise TranslationError(reason="Response 'translations' is not a list")
                
                # Validate each translation item
                result = []
                for item in translations:
                    if not isinstance(item, dict):
                        continue
                    
                    text_unit_id = str(item.get("id", ""))
                    translated_text = str(item.get("text", ""))
                    
                    if text_unit_id and translated_text:
                        result.append({
                            "id": text_unit_id,
                            "text": translated_text,
                        })
                
                # Log if any items were skipped during validation
                skipped_count = len(translations) - len(result)
                if skipped_count > 0:
                    logger.warning(f"Some translation items skipped in batch {batch.sequence_id}: {skipped_count}/{len(translations)} items lost (possibly empty text or invalid format)")
                
                # Check for incomplete response — LLM skipped some units
                expected_count = len(batch.text_units)
                if len(result) < expected_count:
                    delivered_ratio = len(result) / expected_count if expected_count > 0 else 1.0
                    delivered_ids = {r["id"] for r in result}
                    missing_ids = [u.id for u in batch.text_units if u.id not in delivered_ids]
                    logger.warning(
                        f"Incomplete response for batch {batch.sequence_id}: "
                        f"{len(result)}/{expected_count} units delivered ({delivered_ratio:.0%}). "
                        f"Missing unit IDs: {missing_ids}"
                    )
                    if delivered_ratio < DELIVERY_RATIO_THRESHOLD and expected_count > 1:
                        logger.info(
                            f"Only {delivered_ratio:.0%} coverage, splitting batch "
                            f"{batch.sequence_id} and retrying..."
                        )
                        return await self._translate_with_split(batch_data, _split_depth + 1)
                
                logger.info(f"Translation completed for {len(result)} units")
                return result
                
        except httpx.ReadTimeout as e:
            detail_msg = (
                f"Translation request timed out after {self.timeout_seconds}s. "
                f"This is common for large documents. Try increasing LLM_TIMEOUT env var "
                "(e.g., export LLM_TIMEOUT=1800) or reduce document size."
            )
            logger.error(f"Read timeout during translation: {detail_msg}")
            raise ModelUnavailableError(
                endpoint=self.base_url,
                error_code="read_timeout",
            ) from e
        except httpx.ConnectError as e:
            raise ModelUnavailableError(endpoint=self.base_url, error_code=None) from e
        except httpx.TimeoutException as e:
            raise ModelUnavailableError(endpoint=self.base_url, error_code="timeout") from e
    
    async def _translate_with_split(self, batch_data: dict, depth: int = 0) -> list[dict]:
        """Split a failing batch in half and translate each half separately.
        
        Recursive fallback when JSON parsing fails on a large batch
        or response is incomplete (<75% coverage).
        
        Args:
            depth: Current recursion depth (max 3 enforced by translate_batch).
        """
        batch: TranslationBatch = batch_data['batch']
        units = batch.text_units
        mid = len(units) // 2
        left_units = units[:mid]
        right_units = units[mid:]
        
        logger.info(
            f"Splitting batch {batch.sequence_id}: "
            f"{len(units)} units → left={len(left_units)}, right={len(right_units)}"
        )
        
        results = []
        for half_idx, half_units in enumerate([left_units, right_units]):
            if not half_units:
                continue
            half_batch = TranslationBatch(
                sequence_id=batch.sequence_id * 10 + half_idx + 1,
                text_units=half_units,
                source_language=batch.source_language,
                target_language=batch.target_language,
                translation_style=batch.translation_style,
                translation_mode=batch.translation_mode,
                use_glossary=batch.use_glossary,
                glossary_id=batch.glossary_id,
            )
            half_data = {**batch_data, "batch": half_batch}
            half_result = await self.translate_batch(half_data, _split_depth=depth)
            results.extend(half_result)
        
        return results
    
    def _build_prompt(self, batch: TranslationBatch, source_lang: LanguageCode,
                      target_lang: LanguageCode, translation_style: TranslationStyle = TranslationStyle.TECHNICAL,
                      translation_mode: TranslationMode = TranslationMode.FULL) -> str:
        """Build the prompt for LLM translation."""
        
        source_name = _LANGUAGE_NAMES.get(source_lang, source_lang.value.upper())
        target_name = _LANGUAGE_NAMES.get(target_lang, target_lang.value.upper())
        
        # Build filter instruction for user prompt (redundant safety with system prompt)
        filter_instruction = ""
        if translation_mode == TranslationMode.FILTER_BY_SOURCE and source_lang != LanguageCode.DETECT:
            filter_instruction = (
                f"\nВАЖНО: Переведи ТОЛЬКО текст на языке {source_name} внутри поля 'text'. "
                f"Ключи JSON ('id', 'text', 'translations') не являются текстом для перевода — "
                f"оставь их без изменений. Текст на других языках тоже не переводи."
            )
        
        # Format text units for the prompt
        text_items = []
        for i, unit in enumerate(batch.text_units):
            item = f'{{"id": "{unit.id}", "text": "{self._escape_for_prompt(unit.original_text)}"}}'
            if unit.context:
                item += f', "context": "{self._escape_for_prompt(unit.context)}"'
            text_items.append(item)
        
        prompt = f"""Ты профессиональный технический переводчик.

Переведи следующие тексты с языка {source_name} на язык {target_name}.{filter_instruction}

Требования:
- Сохранить терминологию
- Не добавлять комментарии
- Не менять структуру
- Вернуть JSON в точно таком же формате
- Количество элементов должно совпадать

Вот текст для перевода (между маркерами <<<INPUT_TEXT>>> и <<<END_INPUT_TEXT>>>):

<<<INPUT_TEXT>>>
```json
{{
  "translations": [
{chr(10).join('    ' + item for item in text_items)}
  ]
}}
```
<<<END_INPUT_TEXT>>>

Верни ТОЛЬКО JSON с переведенными текстами в следующем формате:

```json
{{
  "translations": [
    {{
      "id": "unit_id",
      "text": "переведенный текст"
    }}
  ]
}}
```"""
        
        return prompt
    
    def _system_prompt(self, target_lang: LanguageCode,
                       translation_style: TranslationStyle = TranslationStyle.TECHNICAL,
                       translation_mode: TranslationMode = TranslationMode.FULL,
                       source_lang: LanguageCode | None = None,
                       has_tags: bool = False,
                       use_glossary: bool = False,
                       glossary_entries: list | None = None) -> str:
        """Return the system prompt for the LLM.
        
        Args:
            has_tags: When True, formatting tag instructions are added.
                      Only used when supports_tag_preservation is True.
            use_glossary: When True, glossary terms are passed as translation hints.
            glossary_entries: List of glossary entries to use as hints.
        """
        target_name = _LANGUAGE_NAMES.get(target_lang, target_lang.value.upper())
        source_name = _LANGUAGE_NAMES.get(source_lang, source_lang.value.upper()) if source_lang else ""
        
        # Tag preservation instructions — only added when tags are present in input
        tag_instruction = ""
        if has_tags and self.supports_tag_preservation:
            tag_instruction = (
                "\n\nFORMATTING TAGS INSTRUCTIONS:\n"
                "- The source text may contain formatting tags: <s1>...</s1>, <s2>...</s2>, etc.\n"
                "- Preserve ALL tags — never drop, duplicate, reorder, or nest them\n"
                "- Move each tag to wrap the semantically equivalent words in the target language\n"
                "- Never leave a tag empty — always put at least one word inside each tag pair\n"
                "- Tags mark text formatting boundaries (bold, italic, color) — placement matters\n"
                "- If tagged content has no direct equivalent, place the tag around the nearest\n"
                "  meaningful translated segment\n"
                "- Return the translation with tags inline, no explanations, no markdown"
            )
        
        # Style-specific instructions
        style_instruction = ""
        if translation_style == TranslationStyle.TECHNICAL:
            style_instruction = (
                "\n\nСТИЛЬ: ТЕХНИЧЕСКИЙ\n"
                "- Используй стандартизированную техническую терминологию (ГОСТ, ISO, DIN)\n"
                "- Сохраняй единицы измерения, числа, символы и обозначения без изменений\n"
                "- Аббревиатуры и акронимы оставляй на исходном языке (первый раз с расшифровкой при необходимости)\n"
                "- Сохраняй наклонение: императив (требования), индикатив (описания)\n"
                "- Избегай художественных оборотов; точность важнее стиля\n"
                "- Технические примечания и ссылки на нормативные документы сохраняй в оригинале\n"
                "- НЕ заменяй числа в хим. формулах на подстрочные/надстрочные символы Unicode (₂, ³, ² и т.д.).\n"
                "  Пиши 'SO2' а не 'SO₂', 'H2O' а не 'H₂O'. Оставляй цифры как есть — размер шрифта\n"
                "  для подстрочного/надстрочного текста будет установлен автоматически на основе оригинала"
            )
        elif translation_style == TranslationStyle.LEGAL:
            style_instruction = (
                "\n\nСТИЛЬ: ЮРИДИЧЕСКИЙ\n"
                "- Используй официально-деловую лексику и юридическую терминологию\n"
                "- Сохраняй модальность: «должен» / «обязан» / «вправе» / «имеет право» точно\n"
                "- Ссылки на законы, статьи, пункты, приложения оставляй в исходном формате\n"
                "- Не перефразируй — сохраняй буквальное значение каждого предложения\n"
                "- Пассивные конструкции сохраняй где это возможно\n"
                "- Даты, номера договоров, суммы прописью — строго как в оригинале\n"
                "- Избегай сокращений, пиши полные формулировки"
            )
        elif translation_style == TranslationStyle.MIXED:
            style_instruction = (
                "\n\nСТИЛЬ: СМЕШАННЫЙ (ТЕХНИЧЕСКИЙ + ЮРИДИЧЕСКИЙ)\n"
                "- Определяй тип контекста автоматически: технические разделы переводи в техническом стиле, юридические — в юридическом\n"
                "- В технических частях: стандартизированная терминология, точность, единицы измерения\n"
                "- В юридических частях: формальная лексика, модальность, точные формулировки\n"
                "- При неопределённом контексте отдавай приоритет юридически точной формулировке\n"
                "- Единообразие терминов по всему документу обязательно"
            )
        
        # Mode-specific instructions
        mode_instruction = ""
        if translation_mode == TranslationMode.FILTER_BY_SOURCE and source_lang:
            mode_instruction = (
                f" IMPORTANT: Translate ONLY the text inside the JSON 'text' field "
                f"that is in {source_name}. "
                f"Do NOT translate the JSON keys ('id', 'text', 'translations') — "
                f"they are markup, not translation content. "
                f"Leave text in all other languages completely unchanged — "
                f"return it verbatim as it appears between the input markers."
            )
        
        # Glossary instruction — pass terms as hints for the LLM
        glossary_instruction = ""
        if use_glossary and glossary_entries:
            terms = []
            for entry in glossary_entries:
                source_text = entry.get_text(source_lang) if source_lang else ""
                target_text = entry.get_text(target_lang)
                if source_text and target_text:
                    terms.append(f'- "{source_text}" → "{target_text}"')
            if terms:
                glossary_instruction = (
                    "\n\nGLOSSARY — Use the following required terminology:\n"
                    + "\n".join(terms)
                    + "\n\nThese terms MUST appear in the translation exactly as specified above."
                    + " When a source term is found in the input, use its corresponding target term in the output."
                )
        
        return (
            f"You are a professional {translation_style.value} translator. "
            f"Your task is to translate text from the language detected in the input "
            f"to {target_name} while preserving terminology and structure. "
            f"Always respond in valid JSON format."
            f"{style_instruction}"
            f"{tag_instruction}"
            f"{mode_instruction}"
            f"{glossary_instruction}"
            f"\n\nINPUT RULES:\n"
            f"- The text to translate is between <<<INPUT_TEXT>>> and <<<END_INPUT_TEXT>>> markers.\n"
            f"- Do NOT include the markers <<<INPUT_TEXT>>> or <<<END_INPUT_TEXT>>> in your output.\n"
            f"- Do NOT add any introductory phrases, explanations, or comments.\n"
            f"- Return ONLY the JSON object with translations.\n"
            f"- PRESERVE plain digits in chemical formulas: write 'SO2' not 'SO₂', 'H2O' not 'H₂O'.\n"
            f"  Never replace numbers with Unicode subscript/superscript characters (₂, ³, ², etc.).\n"
            f"- Translate ALL content completely — do not omit, truncate, or summarize any part.\n"
            f"  Every sentence, clause, and number in the input must appear in your output."
        )
    
    @staticmethod
    def _strip_think_block(text: str) -> str:
        """Remove Qwen3 chain-of-thought <think>...</think> block if present."""
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    
    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        """Remove markdown code fences from model output."""
        return re.sub(r'^```[^\n]*\n|```$', '', text.strip(), flags=re.MULTILINE).strip()
    
    def _clean_model_output(self, content: str, preserve_tags: bool = False) -> str:
        """Remove boundary markers, boilerplate, and model-specific artifacts.
        
        Strips:
        - <<<INPUT_TEXT>>>, <<<END_INPUT_TEXT>>> boundary markers
        - Common boilerplate phrases models prepend
        - Qwen3 <think>...</think> chain-of-thought blocks
        - Markdown code fences (```json ... ```)
        - <sN> formatting tags if model leaked them into output (unless preserve_tags=True)
        """
        # Remove boundary markers if they leaked into output
        content = content.replace("<<<INPUT_TEXT>>>", "")
        content = content.replace("<<<END_INPUT_TEXT>>>", "")
        # Remove <sN> formatting tags only if model doesn't support tag preservation
        if not preserve_tags:
            content = re.sub(r'<s\d+>|</s\d+>', '', content)
        
        # Remove common boilerplate phrases that models sometimes prepend
        boilerplate_patterns = [
            "Вот перевод:",
            "Вот перевод",
            "Перевод:",
            "Sure, here is:",
            "Sure, here is the translation:",
            "Here is the translation:",
            "Here's the translation:",
            "Certainly:",
            "Certainly!",
        ]
        for phrase in boilerplate_patterns:
            if content.startswith(phrase):
                content = content[len(phrase):].strip()
                break
        
        content = self._strip_think_block(content)
        content = self._strip_markdown_fences(content)
        
        return content.strip()
    
    def _fix_json_syntax(self, json_str: str) -> str:
        """Fix common JSON syntax issues produced by LLM output.
        
        Handles:
        - Missing commas between array/object items: `}{` -> `},{`
        - Trailing commas before `]` or `}`  (common LLM issue)
        - Trailing comma after last `}` in array
        """
        # 1. Missing comma between closing and opening braces in arrays
        #    e.g., {"id":"1"}{"id":"2"} -> {"id":"1"},{"id":"2"}
        json_str = re.sub(r'}(\s*){', r'},\1{', json_str)
        
        # 2. Trailing comma before closing bracket/brace
        #    e.g., [1,2,] -> [1,2]  or  {"a":1,} -> {"a":1}
        json_str = re.sub(r',\s*]', r']', json_str)
        json_str = re.sub(r',\s*}', r'}', json_str)
        
        return json_str
    
    def _try_extract_valid_json(self, content: str) -> str | None:
        """Find the longest valid JSON prefix in potentially truncated content.
        
        Handles truncated responses where the last object is cut off mid-string
        (common with translategemma). Progressively strips trailing content
        by trying earlier '}' positions until valid JSON is found.
        
        Returns the valid JSON string or None if none found.
        """
        first = content.find('{')
        if first == -1:
            return None
        
        seen: set[int] = set()
        end = content.rfind('}')
        while end > first:
            if end in seen:
                break
            seen.add(end)
            candidate = content[first:end + 1]
            candidate = self._fix_json_syntax(candidate)
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                end = content.rfind('}', 0, end)
        
        return None
    
    def _escape_for_prompt(self, text: str) -> str:
        """Escape special characters for prompt formatting."""
        if not text:
            return ""
        
        # Escape quotes and newlines for JSON-like format in prompt
        escaped = text.replace("\\", "\\\\")
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n")
        escaped = escaped.replace("\r", "\\r")
        
        return escaped
