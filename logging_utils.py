from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Optional[str] = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> None:
    root = logging.getLogger()

    # Avoid duplicate handlers when modules call setup multiple times.
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

