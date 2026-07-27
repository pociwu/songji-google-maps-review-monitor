from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re


TOKEN_PATTERN = re.compile(r"(?<=/bot)\d{6,}:[A-Za-z0-9_-]{20,}|\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def redact_secrets(value: str) -> str:
    return TOKEN_PATTERN.sub("<redacted>", value)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        log_dir / "monitor.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
