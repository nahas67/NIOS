"""Logging helpers for the MiroFish services."""

from __future__ import annotations

import logging

__all__ = ["get_logger"]


def get_logger(name: str = "mirofish") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
