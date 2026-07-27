"""Configuration management for File Translator."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Centralized constants ──────────────────────────────────────────────
MAX_UPLOAD_SIZE_BYTES: int = int(os.getenv("MAX_UPLOAD_SIZE", str(128 * 1024 * 1024)))
JOB_TTL_SECONDS: int = int(os.getenv("JOB_TTL_SECONDS", "604800"))
JOB_MAX_TTL_SECONDS: int = int(os.getenv("JOB_MAX_TTL_SECONDS", "604800"))
TIKAL_TIMEOUT_SECONDS: int = int(os.getenv("TIKAL_TIMEOUT", "300"))
CLEANUP_INTERVAL_SECONDS: int = int(os.getenv("CLEANUP_INTERVAL", "1800"))
DELIVERY_RATIO_THRESHOLD: float = float(os.getenv("DELIVERY_RATIO_THRESHOLD", "0.75"))
MAX_SPLIT_DEPTH: int = int(os.getenv("MAX_SPLIT_DEPTH", "3"))
STALE_JOB_TTL_SECONDS: int = int(os.getenv("STALE_JOB_TTL", "300"))
# ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMConfig:
    """Configuration for LLM translation provider."""
    
    base_url: str = ""
    model_name: str = "qwen3.6-35b-a3b-claude-4.6-opus-reasoning-distilled"
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_seconds: int = 600
    
    @classmethod
    def from_env(cls) -> LLMConfig:
        """Create configuration from environment variables."""
        base_url = os.getenv("LLM_BASE_URL", cls.base_url)
        if not base_url:
            logger.warning(
                "LLM_BASE_URL not set — LLM provider will fail at runtime. "
                "Set LLM_BASE_URL environment variable."
            )
        return cls(
            base_url=base_url,
            model_name=os.getenv("LLM_MODEL_NAME", cls.model_name),
            temperature=float(os.getenv("LLM_TEMPERATURE", str(cls.temperature))),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", str(cls.max_tokens))),
            timeout_seconds=int(os.getenv("LLM_TIMEOUT", str(cls.timeout_seconds))),
        )


@dataclass(frozen=True)
class OkapiConfig:
    """Configuration for Okapi Tikal CLI."""
    
    tikal_home: str = ""
    
    @classmethod
    def from_env(cls) -> OkapiConfig:
        return cls(
            tikal_home=os.getenv("TIKAL_HOME", ""),
        )


@dataclass(frozen=True)
class AppConfig:
    """Application-wide configuration."""
    
    # API settings
    host: str = "0.0.0.0"
    port: int = 8000
    
    # Translation settings
    default_batch_size: int = 50
    min_batch_size: int = 10
    max_batch_size: int = 200
    
    # File handling
    temp_directory: str = "/tmp/file_translator"
    max_file_size_mb: int = 128
    
    # Logging
    log_level: str = "INFO"
    
    # LLM configuration (initialized in __post_init__)
    llm_config: LLMConfig | None = None
    
    def __post_init__(self):
        """Initialize LLM config if not set."""
        if self.llm_config is None:
            object.__setattr__(self, 'llm_config', LLMConfig.from_env())
    
    @classmethod
    def from_env(cls) -> AppConfig:
        """Create configuration from environment variables."""
        return cls(
            host=os.getenv("APP_HOST", cls.host),
            port=int(os.getenv("APP_PORT", str(cls.port))),
            default_batch_size=int(os.getenv("DEFAULT_BATCH_SIZE", str(cls.default_batch_size))),
            temp_directory=os.getenv("TEMP_DIRECTORY", cls.temp_directory),
            log_level=os.getenv("LOG_LEVEL", cls.log_level),
        )


# Global configuration instance (lazy initialization)
def get_config() -> AppConfig:
    """Get the application configuration singleton."""
    return _config


# Initialize with defaults
_config = AppConfig.from_env()
