"""Logging utility for greenway-scraper."""

import logging
import sys
import json
from typing import Any, Mapping, Optional


class StructuredLogger:
    """
    Thin wrapper around stdlib logging that supports structured kwargs:

        logger.info("message", source="justia", batch_size=10)

    Python's stdlib logger methods do not accept arbitrary kwargs, so we
    merge them into the message as JSON.
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _format(self, msg: str, fields: Optional[Mapping[str, Any]] = None) -> str:
        if not fields:
            return msg
        try:
            return f"{msg} | {json.dumps(fields, default=str, ensure_ascii=False)}"
        except Exception:
            # Fallback: best-effort stringification
            return f"{msg} | {fields}"

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(self._format(msg, kwargs))

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(self._format(msg, kwargs))

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(self._format(msg, kwargs))

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(self._format(msg, kwargs))

    def exception(self, msg: str, **kwargs: Any) -> None:
        self._logger.exception(self._format(msg, kwargs))

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._logger.critical(self._format(msg, kwargs))

    def __getattr__(self, item: str) -> Any:
        return getattr(self._logger, item)


def get_logger(name: str, level: int = logging.INFO) -> StructuredLogger:
    """
    Get a configured logger instance.
    
    Args:
        name: Logger name (usually __name__)
        level: Logging level
    
    Returns:
        Configured structured logger instance
    """
    logger = logging.getLogger(name)
    
    # Only configure if no handlers exist (avoid duplicate logs)
    if not logger.handlers:
        logger.setLevel(level)
        
        # Console handler with formatting
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(formatter)
        
        logger.addHandler(console_handler)
    
    return StructuredLogger(logger)
