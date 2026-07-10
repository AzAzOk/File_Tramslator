"""CADBackend — abstract interface for CAD file backends.

This module re-exports the ``CADBackend`` ABC from ``domain/interfaces.py``
so that backend implementations can import it conveniently.
"""

from file_translator.domain.interfaces import CADBackend

__all__ = ["CADBackend"]
