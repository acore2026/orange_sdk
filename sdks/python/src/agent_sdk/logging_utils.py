from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .errors import AgentSdkError, ErrorCode

DEFAULT_LOG_FILE_PATH = "./logs/agent-sdk.log"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "credentials",
    "did_key",
    "identity_vc",
    "ability_vc",
    "jws",
    "key",
    "masque_authorization",
    "password",
    "api_key",
    "private_key",
    "proof",
    "public_key",
    "secret",
    "signature",
    "set_cookie",
    "token",
    "ticket",
    "vc0",
    "vc1",
    "vc_list",
}


def _is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_vc_id") or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("api_key", "password", "secret", "signature", "token", "ticket")
    )


def sanitize_for_log(value: Any, *, key: str = "") -> Any:
    """Convert a value to JSON-safe data while redacting credentials and keys."""

    if key and _is_sensitive(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: sanitize_for_log(
                getattr(value, field.name),
                key=field.name,
            )
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_for_log(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_for_log(item) for item in value]
    return f"<{type(value).__name__}>"


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    payload = {
        "event": event,
        **{name: sanitize_for_log(value, key=name) for name, value in fields.items()},
    }
    logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        exc_info=exc_info,
    )


def configure_local_logger(
    *,
    name: str,
    file_path: str,
    level: str,
    max_bytes: int,
    backup_count: int,
) -> logging.Logger:
    normalized_level = level.upper() if isinstance(level, str) else ""
    allowed_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if normalized_level not in allowed_levels:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL",
            field="log_level",
        )
    numeric_level = allowed_levels[normalized_level]
    if max_bytes <= 0:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "log_max_bytes must be greater than zero",
            field="log_max_bytes",
        )
    if backup_count < 0:
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "log_backup_count must be zero or greater",
            field="log_backup_count",
        )
    if not file_path.strip():
        raise AgentSdkError(
            ErrorCode.INVALID_ARGUMENT,
            "log_file_path must not be empty",
            field="log_file_path",
        )

    path = Path(file_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        raise AgentSdkError(
            ErrorCode.LOG_SETUP_FAILED,
            f"cannot open local SDK log file {path}: {exc}",
            field="log_file_path",
        ) from exc

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)
    logger.propagate = False
    close_logger(logger)
    logger.addHandler(handler)
    log_event(
        logger,
        logging.INFO,
        "log_initialized",
        file_path=str(path),
        log_level=normalized_level,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()
