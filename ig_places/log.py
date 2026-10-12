"""Logging a consola y a fichero diario (logs/sync-AAAA-MM-DD.log)."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

LOGGER_NAME = "ig_places"


def setup_logging(log_dir: Path, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"sync-{datetime.now(timezone.utc):%Y-%m-%d}.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return log_file


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
