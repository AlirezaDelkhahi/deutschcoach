from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_SOURCE_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
_GOOGLE_API_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}(?![A-Za-z0-9_-])")
_CARD_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15}\d(?!\d)")

REDACTED_PLACEHOLDER = "[REDACTED]"


def sanitize_source_tag(raw: str | None) -> str:
    """Allow safe attribution tags [A-Za-z0-9_-]{1,64}; normalize anything else to 'direct'."""
    if raw is None:
        return "direct"
    candidate = raw.strip()
    if _SOURCE_TAG_RE.fullmatch(candidate):
        return candidate
    return "direct"


def redact_sensitive_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Redact bot tokens, API keys, 16-digit card numbers, and explicit secrets."""
    if not text:
        return text
    redacted = text
    for secret in sorted({s for s in extra_secrets if s}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED_PLACEHOLDER)
    redacted = _TELEGRAM_TOKEN_RE.sub(REDACTED_PLACEHOLDER, redacted)
    redacted = _GOOGLE_API_KEY_RE.sub(REDACTED_PLACEHOLDER, redacted)
    redacted = _CARD_NUMBER_RE.sub(REDACTED_PLACEHOLDER, redacted)
    return redacted


class SecretRedactingLogFilter(logging.Filter):
    """Logging filter that scrubs credentials and 16-digit card numbers from log records."""

    _EXC_FORMATTER = logging.Formatter()

    def __init__(self, extra_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._extra_secrets = tuple(s for s in extra_secrets if s)

    def _redact(self, value: object) -> object:
        if isinstance(value, str):
            return redact_sensitive_text(value, self._extra_secrets)
        if isinstance(value, BaseException):
            return redact_sensitive_text(str(value), self._extra_secrets)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_text(record.msg, self._extra_secrets)
        elif not record.args:
            record.msg = redact_sensitive_text(str(record.msg), self._extra_secrets)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact(arg) for arg in record.args)
            else:
                record.args = self._redact(record.args)
        if record.exc_info and record.exc_info != (None, None, None):
            if not record.exc_text:
                record.exc_text = self._EXC_FORMATTER.formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text, self._extra_secrets)
        if record.stack_info:
            record.stack_info = redact_sensitive_text(record.stack_info, self._extra_secrets)
        return True


class SecretRedactingFormatter(logging.Formatter):
    """Formatter that redacts tokens, keys, and card numbers across the entire formatted record."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._extra_secrets = tuple(s for s in extra_secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record), self._extra_secrets)


def configure_logging(level: str = "INFO", extra_secrets: Iterable[str] = ()) -> None:
    """Configure root logging with secret redaction and silence HTTP client info logs."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            SecretRedactingFormatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                extra_secrets=extra_secrets,
            )
        )
        root_logger.addHandler(handler)

    redacting_filter = SecretRedactingLogFilter(extra_secrets=extra_secrets)
    for handler in root_logger.handlers:
        handler.filters = [
            existing
            for existing in handler.filters
            if not isinstance(existing, SecretRedactingLogFilter)
        ]
        handler.addFilter(redacting_filter)

    for noisy_logger in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
