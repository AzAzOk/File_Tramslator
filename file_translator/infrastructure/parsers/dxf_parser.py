"""DXF parser - reads DXF files into domain models.

Architecture:
- Parses DXF text entities (TEXT, MTEXT, DIMENSION, etc.) into domain models.
- Handles both ASCII and binary DXF formats.
- Extracts text content with full context (layer, block, position, formatting).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from file_translator.domain.dxf_models import (
    DxfBlock,
    DxfDimension,
    DxfDocument,
    DxfEntity,
    DxfEntityType,
    DxfLayer,
    DxfTextEntity,
    DxfTextPosition,
    DxfTextProperties,
)

logger = logging.getLogger(__name__)


class DxfParseError(Exception):
    """Raised when DXF parsing fails."""
    pass


class DxfParser:
    """Parser for DXF files.
    
    Reads DXF group code pairs and produces DxfDocument domain models.
    Currently a stub returning empty structures — real parsing to follow.
    """
    
    def parse(self, file_path: str | Path) -> DxfDocument:
        """Parse a DXF file into a DxfDocument.
        
        Args:
            file_path: Path to the .dxf file.
            
        Returns:
            DxfDocument with all text-bearing entities extracted.
            
        Raises:
            DxfParseError: If the file cannot be parsed.
        """
        path = Path(file_path)
        if not path.exists():
            raise DxfParseError(f"DXF file not found: {path}")
        
        if not path.suffix.lower() in (".dxf",):
            raise DxfParseError(f"Not a DXF file: {path.suffix}")
        
        logger.info(f"Parsing DXF: {path.name}")
        return self._parse_file(path)
    
    def _parse_file(self, path: Path) -> DxfDocument:
        """Parse the DXF file content.
        
        TODO: Implement actual DXF group code parsing:
            1. Read all lines/group codes
            2. Find ENTITIES section
            3. Extract TEXT, MTEXT, DIMENSION, ATTRIB, ATTDEF entities
            4. Extract blocks and their text entities
            5. Extract layers
            6. Return populated DxfDocument
        """
        doc = DxfDocument(file_path=str(path.absolute()))
        
        # Stub: detect format version (placeholder)
        doc.format_version = self._detect_format(path)
        
        return doc
    
    def _detect_format(self, path: Path) -> str:
        """Detect DXF format version from header.
        
        TODO: Read HEADER section $ACADVER variable.
        """
        return "AC1027"  # Default: AutoCAD 2013
    
    def validate_structure(self, file_path: str | Path) -> bool:
        """Quick validation that the file is a valid DXF.
        
        Checks: file exists, has .dxf extension, starts with group code 0.
        """
        path = Path(file_path)
        if not path.exists() or path.suffix.lower() != ".dxf":
            return False
        
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [next(f).strip() for _ in range(20)]
            # DXF files start with group code 0 followed by SECTION
            return "0" in lines and "SECTION" in lines
        except Exception:
            return False
