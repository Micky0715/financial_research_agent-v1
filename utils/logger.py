"""Centralized loguru logger configuration."""
import sys

from loguru import logger

from config import config

logger.remove()
logger.add(
    sys.stderr,
    level=config.LOG_LEVEL,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>",
)

__all__ = ["logger"]
