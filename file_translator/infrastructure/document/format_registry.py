"""FormatRegistry — plugin registration for IParser/IUpdater pairs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from file_translator.domain.interfaces import IParser, IUpdater


class FormatRegistry:
    """Registry of (IParser, IUpdater) pairs keyed by file extension.

    Usage::

        registry = FormatRegistry()
        registry.register(".dxf", parser=DxfParser, updater=DxfUpdater)
        parser, updater = registry.get(".dxf")
    """

    def __init__(self) -> None:
        self._parsers: dict[str, type[IParser]] = {}
        self._updaters: dict[str, type[IUpdater]] = {}

    def register(
        self,
        extension: str,
        parser: type[IParser],
        updater: type[IUpdater],
    ) -> None:
        """Register an (IParser, IUpdater) pair for *extension*.

        *extension* should include the leading dot, e.g. ``".dxf"``.
        """
        ext = extension.lower()
        self._parsers[ext] = parser
        self._updaters[ext] = updater

    def get(self, extension: str) -> tuple[IParser, IUpdater]:
        """Return a fresh (parser, updater) instance for *extension*.

        Raises ``KeyError`` if the extension is not registered.
        """
        ext = extension.lower()
        parser_cls = self._parsers[ext]
        updater_cls = self._updaters[ext]
        return parser_cls(), updater_cls()

    def get_for_file(self, path: Path) -> tuple[IParser, IUpdater]:
        """Convenience: same as ``get()`` but takes a ``Path``."""
        return self.get(path.suffix.lower())

    def supported_extensions(self) -> set[str]:
        """Return the set of registered extensions (with leading dot)."""
        return set(self._parsers.keys())

    def can_process(self, path: Path) -> bool:
        """Check whether a parser/updater is registered for *path*."""
        return path.suffix.lower() in self._parsers
