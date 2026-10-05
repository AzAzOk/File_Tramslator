"""The admin guide, rendered on the server from the repository's Markdown.

One endpoint answers with both the rendered article and the outline of its
headings. The page builds its navigation tree from the outline instead of
scraping the HTML, which is what keeps the two from disagreeing - and it is why
the outline has to come out of the same parse that produced the HTML.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import markdown
from fastapi import APIRouter, Depends, HTTPException, status

from admin_service.deps import AdminContainer, get_container, get_current_admin
from admin_service.schemas import AdminGuideOut, AdminGuideSection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/docs", tags=["docs"])

# `extra` carries the tables and fenced code the guide uses; `toc` gives every
# heading an id and builds the outline; `sane_lists` stops a wrapped line from
# silently becoming a new list item.
_EXTENSIONS = ["extra", "toc", "sane_lists"]


def _slugify(value: str, separator: str) -> str:
    """Heading ids that survive a Russian guide.

    Python-Markdown's default transliterates to ASCII and *drops* what it cannot
    transliterate, so a Cyrillic heading slugifies to an empty string and
    survives only as ``_3`` - ugly in a shared link and positional besides, so
    every id after it shifts when the guide gains a section. Keeping the letters
    gives stable, readable fragments.
    """
    cleaned = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(rf"\s+", separator, cleaned).strip(separator)


_TOC_CONFIG = {"permalink": True, "slugify": _slugify}


@dataclass(frozen=True)
class _Rendered:
    """A rendered guide plus the file identity it was produced from."""

    path: Path
    mtime_ns: int
    size: int
    result: AdminGuideOut


_cache: _Rendered | None = None


def _flatten(tokens: list[Any], out: list[AdminGuideSection]) -> list[AdminGuideSection]:
    """Depth-first walk of Python-Markdown's nested TOC tokens, in document order.

    Tokens are a mix of heading dicts, raw-HTML strings and grouping dicts that
    only carry children, so anything without an ``id`` is recursed into, not
    emitted.
    """
    for token in tokens:
        if not isinstance(token, dict):
            continue
        if "id" in token and "level" in token:
            out.append(
                AdminGuideSection(
                    level=int(token["level"]),
                    id=str(token["id"]),
                    text=str(token["name"]),
                )
            )
        _flatten(token.get("children", []), out)
    return out


def render_guide(path: Path) -> AdminGuideOut:
    """Render the guide, reusing the previous result while the file is unchanged.

    Keyed on modification time and size, so editing the Markdown is picked up on
    the next page load without a rebuild, while repeat loads do not re-parse it.
    """
    global _cache

    stat = path.stat()
    if (
        _cache is not None
        and _cache.path == path
        and _cache.mtime_ns == stat.st_mtime_ns
        and _cache.size == stat.st_size
    ):
        return _cache.result

    source = path.read_text(encoding="utf-8")
    converter = markdown.Markdown(
        extensions=_EXTENSIONS, extension_configs={"toc": _TOC_CONFIG}
    )
    html = converter.convert(source)
    sections = _flatten(converter.toc_tokens, [])
    title = next((s.text for s in sections if s.level == 1), "")

    result = AdminGuideOut(title=title, html=html, sections=sections)
    _cache = _Rendered(path=path, mtime_ns=stat.st_mtime_ns, size=stat.st_size, result=result)
    logger.info(f"Rendered admin guide: {path} ({len(sections)} sections, {len(html)} bytes of HTML)")
    return result


@router.get("/admin-guide", response_model=AdminGuideOut)
async def get_admin_guide(
    container: AdminContainer = Depends(get_container),
    _: str = Depends(get_current_admin),
) -> AdminGuideOut:
    """The administrator's guide as article markup plus its heading outline."""
    path = Path(container.config.guide_path)
    try:
        # Reading and parsing the file are blocking, and the parse is not free.
        return await asyncio.to_thread(render_guide, path)
    except FileNotFoundError:
        logger.warning(f"Admin guide not found at {path}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Admin guide is missing: {path}",
        ) from None
    except OSError as exc:
        logger.warning(f"Admin guide at {path} is unreadable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Admin guide is unreadable: {exc}",
        ) from exc