"""Environment configuration for the standalone admin service.

The admin service has its own credentials (separate from the translation
users) and reuses the same MongoDB/Redis/MySQL the main API uses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Session cookie name and lifetime (hours).
SESSION_COOKIE = "admin_session"
SESSION_TTL_HOURS = int(os.environ.get("ADMIN_SESSION_TTL_HOURS", "12"))


def default_guide_path() -> Path:
    """The admin guide inside the repository, resolved from this package.

    ``admin_service/`` sits at the repository root, so the parent of the package
    is the root both in a checkout and at ``/app`` in the image - which is where
    the Dockerfile copies the file.
    """
    return Path(__file__).resolve().parents[1] / "docs" / "admin-guide.md"


@dataclass(frozen=True)
class AdminConfig:
    """Admin-service settings resolved from the environment."""

    username: str
    password: str
    mongo_uri: str
    mongo_db: str
    redis_host: str
    redis_port: int
    redis_password: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_db: str
    host: str
    port: int
    log_level: str
    guide_path: Path = field(default_factory=default_guide_path)

    @classmethod
    def from_env(cls) -> "AdminConfig":
        return cls(
            username=os.environ.get("ADMIN_UI_USERNAME", "admin"),
            password=os.environ.get("ADMIN_UI_PASSWORD", "admin123"),
            mongo_uri=os.environ.get("MONGO_URI", "mongodb://mongo:27017"),
            mongo_db=os.environ.get("MONGO_DB_NAME", "file_translator_auth"),
            redis_host=os.environ.get("REDIS_HOST", "redis"),
            redis_port=int(os.environ.get("REDIS_PORT", "6379")),
            redis_password=os.environ.get("REDIS_PASSWORD", ""),
            mysql_host=os.environ.get("GLOSSARY_DB_HOST", "dbserver"),
            mysql_port=int(os.environ.get("GLOSSARY_DB_PORT", "3306")),
            mysql_user=os.environ.get("GLOSSARY_DB_USER", "glossary"),
            mysql_password=os.environ.get("GLOSSARY_DB_PASSWORD", ""),
            mysql_db=os.environ.get("GLOSSARY_DB_NAME", "glossary"),
            host=os.environ.get("ADMIN_HOST", "0.0.0.0"),
            port=int(os.environ.get("ADMIN_PORT", "8011")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            guide_path=Path(
                os.environ.get("ADMIN_GUIDE_PATH") or default_guide_path()
            ),
        )
