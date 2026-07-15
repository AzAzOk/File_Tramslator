"""Domain errors - Custom exception hierarchy."""

from __future__ import annotations


class DocumentTranslatorError(Exception):
    """Base exception for document translation errors."""
    
    def __init__(self, message: str = "Произошла ошибка при переводе документа", 
                 context: dict | None = None):
        super().__init__(message)
        self.message = message
        self.context = context or {}


class DocumentOpenError(DocumentTranslatorError):
    """Raised when document cannot be opened."""
    
    def __init__(self, file_path: str = "", reason: str = ""):
        message = f"Не удалось открыть документ: {file_path}" if file_path else "Не удалось открыть документ"
        super().__init__(message, {"file_path": file_path, "reason": reason})


class DocumentParseError(DocumentTranslatorError):
    """Raised when document structure cannot be parsed."""
    
    def __init__(self, error_type: str = "", details: str = ""):
        message = f"Ошибка разбора документа ({error_type}): {details}" if details else f"Ошибка разбора документа: {error_type}"
        super().__init__(message, {"error_type": error_type, "details": details})


class TranslationError(DocumentTranslatorError):
    """Raised when translation process fails."""
    
    def __init__(self, batch_id: str = "", reason: str = ""):
        message = f"Ошибка перевода для пакета {batch_id}: {reason}" if batch_id else "Ошибка перевода"
        super().__init__(message, {"batch_id": batch_id, "reason": reason})


class ModelUnavailableError(DocumentTranslatorError):
    """Raised when translation model is unavailable."""
    
    def __init__(self, endpoint: str = "", error_code: int | None = None):
        message = f"Модель перевода недоступна: {endpoint}" if endpoint else "Модель перевода недоступна"
        super().__init__(message, {"endpoint": endpoint, "error_code": error_code})


class SaveDocumentError(DocumentTranslatorError):
    """Raised when document cannot be saved."""
    
    def __init__(self, output_path: str = "", reason: str = ""):
        msg = f"Не удалось сохранить документ: {output_path}" if output_path else "Не удалось сохранить документ"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg, {"output_path": output_path, "reason": reason})


class ValidationError(DocumentTranslatorError):
    """Raised when input validation fails."""
    
    def __init__(self, field: str = "", message: str = ""):
        full_message = f"Ошибка валидации '{field}': {message}" if field else message
        super().__init__(full_message, {"field": field, "details": message})
