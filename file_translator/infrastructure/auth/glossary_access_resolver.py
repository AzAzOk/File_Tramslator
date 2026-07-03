"""Resolves AD group membership to glossary collection access."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class GlossaryAccessResolver:
    """Maps AD groups to glossary collection IDs.
    
    Reads GLOSSARY_COLLECTION_MAP env var — a JSON dict mapping
    AD group CNs to lists of collection IDs:
    
    GLOSSARY_COLLECTION_MAP='{"DTD": ["dtd_glossary"], "VPNASTANA": ["vpn_glossary"]}'
    
    Users without matching groups see only the "default" collection.
    """

    def __init__(self, collection_map: dict[str, list[str]] | None = None):
        self._collection_map = collection_map or self._load_from_env()

    @staticmethod
    def _load_from_env() -> dict[str, list[str]]:
        raw = os.getenv("GLOSSARY_COLLECTION_MAP", "")
        if not raw:
            logger.info("GLOSSARY_COLLECTION_MAP not set — only 'default' collection available")
            return {}
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning("GLOSSARY_COLLECTION_MAP is not a JSON object — ignoring")
                return {}
            return {str(k): list(v) if isinstance(v, list) else [str(v)] for k, v in parsed.items()}
        except json.JSONDecodeError as e:
            logger.warning(f"GLOSSARY_COLLECTION_MAP parse error: {e}")
            return {}

    def resolve(self, groups: list[str] | None) -> list[str]:
        """Resolve AD group names to allowed collection IDs.
        
        Args:
            groups: List of AD group CNs from memberOf attribute.
            
        Returns:
            List of collection IDs the user can access.
            Always includes "default" as a fallback.
        """
        allowed: set[str] = set()
        if groups:
            for group_name in groups:
                if group_name in self._collection_map:
                    allowed.update(self._collection_map[group_name])
        if not allowed:
            allowed.add("default")
        return list(allowed)

    def is_collection_allowed(self, collection_id: str, groups: list[str] | None) -> bool:
        """Check if a user (by their AD groups) can access a specific collection."""
        return collection_id in self.resolve(groups)
