"""Central logging setup with credential redaction and optional JSON output."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'(application[_ -]?password[\'"]?\s*[:=]\s*)[^,\s]+', re.I),
    re.compile(r'(authorization[\'"]?\s*[:=]\s*)[^,\s]+', re.I),
    re.compile(r'(api[_ -]?key[\'"]?\s*[:=]\s*)[^,\s]+', re.I),
    re.compile(r'(token[\'"]?\s*[:=]\s*)[^,\s]+', re.I),
    re.compile(r'(secret[\'"]?\s*[:=]\s*)[^,\s]+', re.I),
)


class RedactingFilter(logging.Filter):
    """Remove obvious secrets from log messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(arg)) for arg in record.args)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter for container log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def redact(message: str, extra_values: Iterable[str] | None = None) -> str:
    """Return a copy of *message* with credentials replaced."""

    redacted = message
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    for value in extra_values or ():
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def configure_logging(
    level: str = "INFO",
    *,
    log_file: Path | str | None = None,
    json_logs: bool = False,
    enable_file_logging: bool = True,
) -> None:
    """Configure root logging for app, health endpoint, and libraries.

    Calling this multiple times is safe; existing handlers are replaced so
    Streamlit reruns do not duplicate log lines.
    """

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    formatter: logging.Formatter
    if json_logs:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(RedactingFilter())
    root.addHandler(stream_handler)

    if enable_file_logging and log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RedactingFilter())
        root.addHandler(file_handler)
