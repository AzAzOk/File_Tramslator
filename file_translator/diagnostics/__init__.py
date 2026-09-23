"""Diagnostics package — host-side triage tooling for structure fidelity.

Scanners, render loop, and the page→unit dumper live here. This package is
developer tooling: it is NOT imported by the running translation service
(except for the small ``source_origin`` metadata key set in the pipeline, see
``file_translator.application.service``).
"""

from file_translator.diagnostics.structure_scanner import (
    StructureProfile,
    compare_structures,
    profile_docx,
    TOPOLOGICAL_INVARIANTS,
    COUNT_INVARIANTS,
)
from file_translator.diagnostics.normative_whitelist import (
    DEFAULT_DESIGNATION_PREFIXES,
    is_normative_reference,
    matches_normative_designation,
)
from file_translator.diagnostics.unit_locator import (
    UnitMatch,
    locate_fragment,
    locate_fragment_in_xliff_dir,
    visible_text,
)

__all__ = [
    "StructureProfile",
    "compare_structures",
    "profile_docx",
    "TOPOLOGICAL_INVARIANTS",
    "COUNT_INVARIANTS",
    "DEFAULT_DESIGNATION_PREFIXES",
    "is_normative_reference",
    "matches_normative_designation",
    "UnitMatch",
    "locate_fragment",
    "locate_fragment_in_xliff_dir",
    "visible_text",
]