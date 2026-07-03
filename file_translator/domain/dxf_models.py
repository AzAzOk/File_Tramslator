"""DXF domain models for CAD document translation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DxfEntityType(Enum):
    """DXF entity types that may contain translatable text."""
    
    TEXT = "TEXT"
    MTEXT = "MTEXT"
    DIMENSION = "DIMENSION"
    ATTRIB = "ATTRIB"
    ATTDEF = "ATTDEF"
    RTEXT = "RTEXT"
    LEADER = "LEADER"
    MLEADER = "MLEADER"


@dataclass
class DxfTextPosition:
    """Position of text in a DXF entity."""
    
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rotation: float = 0.0


@dataclass
class DxfTextProperties:
    """Text formatting properties."""
    
    height: float = 2.5
    style: str = "STANDARD"
    font: str = ""
    width_factor: float = 1.0
    oblique_angle: float = 0.0
    is_mirrored: bool = False
    horizontal_alignment: str = "LEFT"
    vertical_alignment: str = "BASELINE"


@dataclass
class DxfEntity:
    """Base DXF entity with common properties."""
    
    handle: str = ""
    layer: str = "0"
    entity_type: DxfEntityType | str = ""
    owner_block: str = ""
    raw_group_codes: list[tuple[int, Any]] = field(default_factory=list)


@dataclass
class DxfTextEntity(DxfEntity):
    """A DXF entity containing translatable text."""
    
    original_text: str = ""
    translated_text: str = ""
    position: DxfTextPosition = field(default_factory=DxfTextPosition)
    properties: DxfTextProperties = field(default_factory=DxfTextProperties)
    
    @property
    def id(self) -> str:
        """Unique identifier for this text entity."""
        return f"dxf_{self.handle}" if self.handle else f"dxf_{id(self)}"


@dataclass
class DxfDimension(DxfEntity):
    """A DXF dimension entity with measurement text."""
    
    measurement: float = 0.0
    prefix: str = ""
    suffix: str = ""
    original_text: str = ""
    translated_text: str = ""
    text_position: DxfTextPosition = field(default_factory=DxfTextPosition)
    
    @property
    def id(self) -> str:
        return f"dim_{self.handle}" if self.handle else f"dim_{id(self)}"


@dataclass
class DxfBlock:
    """A DXF block definition containing entities."""
    
    name: str = ""
    description: str = ""
    entities: list[DxfEntity] = field(default_factory=list)


@dataclass
class DxfLayer:
    """A DXF layer."""
    
    name: str = "0"
    color: int = 7
    is_frozen: bool = False
    is_locked: bool = False


@dataclass
class DxfDocument:
    """Complete DXF document representation for translation."""
    
    file_path: str = ""
    format_version: str = "AC1027"  # AutoCAD 2013
    layers: list[DxfLayer] = field(default_factory=list)
    blocks: list[DxfBlock] = field(default_factory=list)
    entities: list[DxfEntity] = field(default_factory=list)
    
    def get_text_entities(self) -> list[DxfTextEntity]:
        """Get all entities containing translatable text."""
        result: list[DxfTextEntity] = []
        text_types = {
            DxfEntityType.TEXT, DxfEntityType.MTEXT, DxfEntityType.ATTRIB,
            DxfEntityType.ATTDEF, DxfEntityType.RTEXT,
        }
        for entity in self.entities:
            if isinstance(entity.entity_type, DxfEntityType) and entity.entity_type in text_types:
                if isinstance(entity, DxfTextEntity) and entity.original_text.strip():
                    result.append(entity)
        return result
    
    def get_dimensions(self) -> list[DxfDimension]:
        """Get all dimension entities containing translatable text."""
        return [
            e for e in self.entities
            if isinstance(e, DxfDimension) and e.original_text.strip()
        ]
    
    def get_all_texts(self) -> list[DxfTextEntity | DxfDimension]:
        """Get all entities with text that may need translation."""
        return self.get_text_entities() + self.get_dimensions()
