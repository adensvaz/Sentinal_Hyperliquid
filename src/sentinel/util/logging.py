"""Logging setup: console (rich if available) + rotating-ish file handler."""
from __future__ import annotations

import logging
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_path: str | None = None, level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("sentinel")
    if _CONFIGURED:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    try:
        from rich.logging import RichHandler
        console: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
        console.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
    except Exception:  # pragma: no cover
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)

    _CONFIGURED = True
    return logger
