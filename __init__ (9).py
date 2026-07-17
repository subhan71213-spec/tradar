"""logger.py — production logging setup.

Deliberately NOT named logging.py: a module named logging.py anywhere
importable on sys.path would shadow Python's own standard-library
logging module for the entire process (any `import logging` from that
point on would resolve to this file instead of the stdlib), which is a
subtle, hard-to-diagnose bug. Living at
infrastructure/logging/logger.py avoids that entirely while still
being exactly what "the project's logging module" means.

Provides:
    - A console (stdout) handler on the root logger -- this is the
      PRIMARY log sink in production: Render (and most PaaS platforms)
      capture container stdout/stderr directly, and that capture
      survives container restarts even when local disk does not.
    - A daily-rotating general log file (midnight rotation, 14 days
      retained).
    - A separate error-only log file (level=ERROR and above only), so
      "show me what broke" never requires grepping through routine
      INFO noise.
    - A dedicated `titan.telegram` logger with its own rotating file,
      for every Telegram send/verify call -- delivery failures need
      their own trail separate from general application logs.

File logging is best-effort: on a platform with an ephemeral/read-only
filesystem (or no LOG_DIR permissions), file handler setup failures are
caught and logged to the console instead of crashing startup -- the
console handler alone is sufficient to run in production.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s"

TELEGRAM_LOGGER_NAME = "titan.telegram"


def _make_rotating_file_handler(
    path: Path, level: int, formatter: logging.Formatter, backup_count: int = 14
) -> logging.Handler | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(path), when="midnight", backupCount=backup_count, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        return handler
    except OSError as exc:
        # Best-effort: an ephemeral/read-only filesystem must not stop
        # the application from starting. Console logging still works.
        print(f"[logger setup] Could not create file handler for {path}: {exc}", file=sys.stderr)
        return None


def setup_logging(
    log_dir: str | os.PathLike[str] | None = None,
    level: str = "INFO",
) -> None:
    """Configures the root logger (console + daily-rotating file +
    error-only file) and the dedicated titan.telegram logger. Safe to
    call once at process startup; idempotent if called again (existing
    handlers on the root logger are cleared first, so repeated calls in
    tests don't accumulate duplicate handlers)."""
    log_directory = Path(log_dir) if log_dir else Path(os.environ.get("LOG_DIR", "logs"))
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root_logger.addHandler(console_handler)

    file_formatter = logging.Formatter(_FILE_FORMAT)

    general_handler = _make_rotating_file_handler(
        log_directory / "titan.log", numeric_level, file_formatter
    )
    if general_handler is not None:
        root_logger.addHandler(general_handler)

    error_handler = _make_rotating_file_handler(
        log_directory / "titan_errors.log", logging.ERROR, file_formatter
    )
    if error_handler is not None:
        root_logger.addHandler(error_handler)

    telegram_logger = logging.getLogger(TELEGRAM_LOGGER_NAME)
    telegram_logger.setLevel(numeric_level)
    telegram_logger.propagate = True  # also flows to the root handlers above
    telegram_file_handler = _make_rotating_file_handler(
        log_directory / "titan_telegram.log", numeric_level, file_formatter
    )
    if telegram_file_handler is not None:
        telegram_logger.addHandler(telegram_file_handler)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s log_dir=%s", level.upper(), log_directory
    )


def get_telegram_logger() -> logging.Logger:
    """The dedicated logger for Telegram send/verify activity. Use this
    (rather than a module-level logger) anywhere a Telegram API call is
    made, so delivery issues land in titan_telegram.log."""
    return logging.getLogger(TELEGRAM_LOGGER_NAME)
