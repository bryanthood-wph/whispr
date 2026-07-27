"""Shared rolling logger. All modules call get_logger(name)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Attach a rotating file handler + console handler to the root 'whispr' logger."""
    global _configured
    if _configured:
        return
    logger = logging.getLogger("whispr")
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "whispr.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'whispr' namespace."""
    return logging.getLogger(f"whispr.{name}")
