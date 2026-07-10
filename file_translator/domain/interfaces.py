"""Domain interfaces - Abstract contracts for translators and pipeline components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from file_translator.domain.document_model import Document
from file_translator.domain.models import DocumentFormat


class DocumentTranslator(ABC):
    """Abstract interface for document translators.
    
    Implements the Strategy pattern to allow different document formats
    to be processed without modifying existing code (Open-Closed Principle).
    """
    
    @classmethod
    @abstractmethod
    def supported_formats(cls) -> set[DocumentFormat]:
        """Return set of document formats this translator supports."""
        ...
    
    @abstractmethod
    def can_process(self, file_path: Path) -> bool:
        """Check if this translator can process the given file."""
        ...
    
    @abstractmethod
    def extract(self, file_path: Path,
                source_lang: str = "en",
                target_lang: str = "ru") -> dict:
        """Extract text units from document preserving structure.
        
        Args:
            file_path: Path to the source document.
            source_lang: Source language code (BCP-47).
            target_lang: Target language code (BCP-47).
        
        Returns:
            Dictionary containing extracted data and metadata.
        """
        ...
    
    @abstractmethod
    def translate(self, extracted_data: dict, translations: dict[str, str]) -> dict:
        """Apply translations to extracted data.
        
        Args:
            extracted_data: Data from extract() method.
            translations: Mapping of text unit IDs to translated text.
            
        Returns:
            Translated document data ready for saving.
        """
        ...
    
    @abstractmethod
    def save(self, translated_data: dict, output_path: Path) -> None:
        """Save translated document to file system."""
        ...


class TranslationProvider(ABC):
    """Abstract interface for LLM translation providers.
    
    Allows swapping different translation models without changing
    the application layer (Dependency Inversion Principle).
    """
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the translation provider is available."""
        ...
    
    @abstractmethod
    async def translate_batch(self, batch_data: dict) -> list[dict]:
        """Translate a batch of text units.
        
        Args:
            batch_data: Batch containing text units and language info.
            
        Returns:
            List of dictionaries with translated text for each unit.
        """
        ...


class GlossaryRepository(ABC):
    """Abstract interface for glossary data access.
    
    Decouples glossary storage (MySQL, file, etc.) from the application.
    """
    
    @abstractmethod
    async def find_all(self, table_name: str = "glossary") -> list[Any]:
        """Retrieve all glossary entries from a specific table."""
        ...
    
    @abstractmethod
    async def find_by_id(self, entry_id: str, table_name: str = "glossary") -> Any | None:
        """Find a glossary entry by its ID in a specific table."""
        ...
    
    @abstractmethod
    async def add(self, entry: Any, table_name: str = "glossary", created_by: str = "") -> Any:
        """Add a new glossary entry to a specific table."""
        ...
    
    @abstractmethod
    async def update(self, entry: Any, table_name: str = "glossary", updated_by: str = "") -> Any | None:
        """Update an existing glossary entry in a specific table."""
        ...
    
    @abstractmethod
    async def delete(self, entry_id: str, table_name: str = "glossary") -> bool:
        """Delete a glossary entry by ID from a specific table."""
        ...
    
    @abstractmethod
    async def table_exists(self, table_name: str) -> bool:
        """Check if a MySQL table exists."""
        ...
    
    @abstractmethod
    async def list_tables(self, pattern: str = "glossary_%") -> list[str]:
        """List MySQL tables matching the given pattern."""
        ...
    
    @abstractmethod
    async def import_from_file(self, file_path: Path) -> int:
        """Import glossary entries from a file. Returns count of imported entries."""
        ...
    
    @abstractmethod
    async def export_to_file(self, file_path: Path) -> Path:
        """Export all glossary entries to a file. Returns path to the exported file."""
        ...


class GlossaryCollectionRepository(ABC):
    """Abstract interface for glossary collection storage.
    
    Manages named collections that group glossary entries.
    The "default" collection backs the existing single MySQL glossary.
    """
    
    @abstractmethod
    async def find_all(self) -> list[Any]:
        """Retrieve all glossary collections."""
        ...
    
    @abstractmethod
    async def find_by_id(self, collection_id: str) -> Any | None:
        """Find a glossary collection by its ID."""
        ...
    
    @abstractmethod
    async def get_entries(self, collection_id: str) -> list[Any]:
        """Get all glossary entries belonging to a collection."""
        ...


class JournalRepository(ABC):
    """Abstract interface for processing journal storage.
    
    Manages daily journal files with monthly retention policy.
    A new journal is created on the first request of each day.
    Journals older than 30 days are automatically removed.
    """
    
    @abstractmethod
    async def get_journal(self, date: str) -> Any | None:
        """Retrieve the journal for a specific date (YYYY-MM-DD)."""
        ...
    
    @abstractmethod
    async def save_entry(self, date: str, entry: Any) -> None:
        """Append a journal entry for the given date."""
        ...
    
    @abstractmethod
    async def list_dates(self) -> list[str]:
        """List all dates that have journal entries (newest first)."""
        ...
    
    @abstractmethod
    async def delete_older_than(self, days: int) -> int:
        """Delete journal entries/files older than specified days. Returns count deleted."""
        ...


class JobRepository(ABC):
    """Abstract interface for job state storage.
    
    Manages lifecycle and progress of async translation jobs.
    Supports creation, status updates, cancellation, and querying.
    """
    
    @abstractmethod
    async def create(self, job: Any) -> Any:
        """Create a new job entry."""
        ...
    
    @abstractmethod
    async def get(self, job_id: str) -> Any | None:
        """Get a job by its ID."""
        ...
    
    @abstractmethod
    async def update(self, job: Any) -> Any | None:
        """Update an existing job (status, progress, etc.)."""
        ...
    
    @abstractmethod
    async def list_active(self) -> list[Any]:
        """List all active (pending or running) jobs."""
        ...
    
    @abstractmethod
    async def list_recent(self, limit: int = 10) -> list[Any]:
        """List most recent jobs, newest first."""
        ...

    @abstractmethod
    async def delete(self, job_id: str) -> bool:
        """Delete a job permanently."""
        ...


class DocumentValidator(ABC):
    """Abstract interface for input file validation.
    
    Implements the Chain of Responsibility pattern to allow
    multiple validation checks without modifying existing code.
    Each validator performs a single check and can stop the chain.
    """
    
    @abstractmethod
    async def validate(self, file_path: Path, context: dict[str, Any] | None = None) -> Any:
        """Validate the given file.
        
        Args:
            file_path: Path to the input file.
            context: Optional context (e.g. source_language, target_language).
            
        Returns:
            ValidationResult for this check.
        """
        ...


class AuthProvider(ABC):
    """Abstract interface for authentication providers.
    
    Supports multiple auth methods: API key, JWT token, basic auth.
    """
    
    @abstractmethod
    async def authenticate(self, token: str, method: str = "bearer") -> Any:
        """Authenticate a user by token.
        
        Args:
            token: The raw token string (JWT, API key, etc.).
            method: Authentication method (bearer, api_key, basic).
            
        Returns:
            AuthCredentials if valid, None otherwise.
        """
        ...
    
    @abstractmethod
    async def create_token(self, user_id: str, expires_in: int = 3600) -> Any:
        """Create an auth token for a user."""
        ...
    
    @abstractmethod
    async def validate_api_key(self, key: str) -> Any:
        """Validate an API key and return associated user."""
        ...


class UserRepository(ABC):
    """Abstract interface for user storage."""
    
    @abstractmethod
    async def get_by_id(self, user_id: str) -> Any | None:
        """Get a user by their ID."""
        ...
    
    @abstractmethod
    async def get_by_username(self, username: str) -> Any | None:
        """Get a user by their username."""
        ...
    
    @abstractmethod
    async def create(self, user: Any) -> Any:
        """Create a new user."""
        ...
    
    @abstractmethod
    async def update(self, user: Any) -> Any | None:
        """Update an existing user."""
        ...
    
    @abstractmethod
    async def delete(self, user_id: str) -> bool:
        """Delete a user by ID."""
        ...
    
    @abstractmethod
    async def list_all(self) -> list[Any]:
        """List all users."""
        ...


class IParser(ABC):
    """Format-agnostic parser interface.

    Every supported document format provides an implementation that
    produces a ``Document`` — the universal intermediate representation.
    """

    @abstractmethod
    def parse(self, path: Path) -> Document:
        """Read the file at *path* and return a Document."""
        ...

    @abstractmethod
    def capabilities(self) -> set[str]:
        """Declare what this parser supports (e.g. 'tables', 'blocks')."""
        ...


class IUpdater(ABC):
    """Format-agnostic updater interface.

    Applies translations to a Document and saves the result back to the
    native file format.
    """

    @abstractmethod
    def apply(self, document: Document, translations: dict[str, str]) -> None:
        """Write translated text into *document* entities.

        ``translations`` maps ``entity.id`` → ``translated_text``.
        """
        ...

    @abstractmethod
    def save(self, document: Document, output_path: Path) -> None:
        """Persist *document* (with applied translations) to *output_path*."""
        ...


class CADBackend(ABC):
    """Low-level CAD backend — abstracts ezdxf vs ODA SDK vs conversion.

    This is *not* a format-agnostic interface: it exposes DXF/DWG-specific
    concepts (handles, layers, blocks).  ``DxfParser`` / ``DxfUpdater``
    wrap it and present the generic ``IParser`` / ``IUpdater`` contract.
    """

    @abstractmethod
    def open(self, path: Path) -> Any:
        """Open a CAD file and return a backend-specific document handle."""
        ...

    @abstractmethod
    def iter_entities(self, doc: Any) -> Iterator[Any]:
        """Yield every text-bearing entity in *doc*."""
        ...

    @abstractmethod
    def get_text(self, entity: Any) -> str:
        """Return the raw text stored in *entity*."""
        ...

    @abstractmethod
    def set_text(self, entity: Any, text: str) -> None:
        """Replace the text in *entity* with *text*."""
        ...

    @abstractmethod
    def get_handle(self, entity: Any) -> str:
        """Return the native handle of *entity*."""
        ...

    @abstractmethod
    def get_layer(self, entity: Any) -> str:
        """Return the layer name of *entity*."""
        ...

    @abstractmethod
    def get_entity_count(self, doc: Any) -> int:
        """Return the total number of text-bearing entities in *doc*."""
        ...

    @abstractmethod
    def save(self, doc: Any, path: Path) -> None:
        """Write *doc* (native handle) to *path*."""
        ...

    @abstractmethod
    def close(self, doc: Any) -> None:
        """Release resources held by *doc*."""
        ...
