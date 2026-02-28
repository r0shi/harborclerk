"""Centralized logging setup for Harbor Clerk services.

When running inside the macOS native app (native_config_file is set),
adds a RotatingFileHandler writing to the logs/ directory alongside
the config file. In Docker / dev mode, logs only go to stdout.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(service_name: str, level: str = "INFO") -> None:
    """Configure root logger with console + optional file handler.

    Args:
        service_name: Used for the log filename (e.g. "api", "worker-io").
        level: Log level string (e.g. "INFO", "DEBUG").
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    formatter = logging.Formatter(fmt)

    # Console handler (always)
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)

    # File handler (only when running as macOS native app)
    config_file = os.environ.get("NATIVE_CONFIG_FILE", "")
    if config_file:
        logs_dir = Path(config_file).parent / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                logs_dir / f"{service_name}.log",
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=3,
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            logging.warning("Could not create log file in %s", logs_dir)
