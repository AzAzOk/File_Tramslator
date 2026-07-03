"""Logging configuration for File Translator."""

import logging
from loguru import logger as loguru_logger


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging for the application using Loguru."""
    
    # Remove default handlers
    loguru_logger.remove()
    
    # Add console handler with colored output
    loguru_logger.add(
        "stderr",
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )
    
    # Add file handler for detailed logging
    loguru_logger.add(
        "logs/file_translator_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )
    
    # Also configure standard logging to use loguru
    class LoguruHandler(logging.Handler):
        def emit(self, record):
            try:
                level = loguru_logger.level(record.levelname).name if record.levelno else "INFO"
                message = self.format(record)
                loguru_logger.log(level, message)
            except Exception:
                pass
    
    logging.getLogger().addHandler(LoguruHandler())


# Export logger for use throughout the application
__all__ = ["logger", "setup_logging"]
