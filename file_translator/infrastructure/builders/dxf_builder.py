"""DXF builder - writes domain models back to DXF files.

Architecture:
- Takes translated DxfDocument and writes back to DXF format.
- Preserves non-text structure exactly (positions, layers, blocks, dimensions).
- Replaces original text with translated text in ENTITIES and BLOCK sections.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from file_translator.domain.dxf_models import DxfDocument, DxfEntity, DxfTextEntity

logger = logging.getLogger(__name__)


class DxfBuildError(Exception):
    """Raised when DXF building fails."""
    pass


class DxfBuilder:
    """Builder for DXF files.
    
    Takes a DxfDocument with translated text and writes it back to a .dxf file.
    Currently a stub — real implementation will perform group code substitution.
    """
    
    def build(self, document: DxfDocument, output_path: str | Path) -> str:
        """Write the translated DXF document.
        
        Args:
            document: DxfDocument with translated text.
            output_path: Where to save the translated .dxf file.
            
        Returns:
            Path to the saved file.
            
        Raises:
            DxfBuildError: If the file cannot be written.
        """
        path = Path(output_path)
        
        if not document.file_path:
            raise DxfBuildError("No source file path in document")
        
        source_path = Path(document.file_path)
        
        logger.info(f"Building translated DXF: {path.name}")
        return self._build_document(document, source_path, path)
    
    def _build_document(self, document: DxfDocument, source: Path, output: Path) -> str:
        """Build the translated DXF file.
        
        TODO: Implement actual DXF writing:
            1. Copy source DXF as base
            2. For each text entity with translated_text:
               - Find its group code 1 (text value) in the file
               - Replace with translated text
            3. Handle MTEXT (group codes 1, 3, 101)
            4. Handle DIMENSION (group code 1 for user text, or compute from measurement)
            5. Save to output path
        """
        # Stub: copy source file (no actual replacement yet)
        shutil.copy2(str(source), str(output))
        logger.warning("DXF builder stub: file copied without text replacement")
        
        return str(output)
