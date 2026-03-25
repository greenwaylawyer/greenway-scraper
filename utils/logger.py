"""Structured logging configuration for Greenway Scraper."""

import structlog
import logging
import sys

_configured = False


def configure_structlog():
    """Configure structured logging for the scraper."""
    global _configured
    if _configured:
        return
    _configured = True

    # Reset any previous config and use native structlog (no stdlib integration)
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )

    # Also configure standard logging for Playwright
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )


def get_logger(name: str):
    """Get a structured logger instance."""
    configure_structlog()
    return structlog.get_logger(name)
