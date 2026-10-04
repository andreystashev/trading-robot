"""Rotating local logs with Moscow timestamps and credential redaction."""

import logging
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
TOKEN_PATTERN = re.compile(r"\bt\.[A-Za-z0-9_-]{30,}")
BEARER_PATTERN = re.compile(r"Bearer\s+\S+", re.I)


def redact(message: str) -> str:
    return BEARER_PATTERN.sub(
        "Bearer [REDACTED]", TOKEN_PATTERN.sub("[REDACTED]", message)
    )


LOG_PATH = Path(__file__).with_name("logs") / "trading_bot.log"


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = redact(message)
        record.msg, record.args = message, ()
        return True


class MoscowFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Exceptions are appended after filters run, so redact the final text too.
        return redact(super().format(record))

    def formatTime(self, record, datefmt=None) -> str:
        return datetime.fromtimestamp(record.created, MOSCOW).strftime(
            "%Y-%m-%d %H:%M:%S MSK"
        )


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        ),
    ]
    for handler in handlers:
        handler.setFormatter(
            MoscowFormatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
        )
        handler.addFilter(RedactTokens())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("t_tech").setLevel(logging.CRITICAL)
    logging.info("Log file: %s", LOG_PATH)
