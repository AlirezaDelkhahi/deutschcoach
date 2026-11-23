from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from deutschcoach_phase0.config import load_settings
from deutschcoach_phase0.security import (
    SecretRedactingFormatter,
    SecretRedactingLogFilter,
    configure_logging,
    redact_sensitive_text,
    sanitize_source_tag,
)


def test_sanitize_source_tag_allows_valid_tags() -> None:
    assert sanitize_source_tag("tg_channel_1") == "tg_channel_1"
    assert sanitize_source_tag("tutor-ali_2026") == "tutor-ali_2026"
    assert sanitize_source_tag("A" * 64) == "A" * 64


def test_sanitize_source_tag_rejects_invalid_tags_to_direct() -> None:
    assert sanitize_source_tag(None) == "direct"
    assert sanitize_source_tag("") == "direct"
    assert sanitize_source_tag("   ") == "direct"
    assert sanitize_source_tag("bad tag") == "direct"
    assert sanitize_source_tag("../escape") == "direct"
    assert sanitize_source_tag("A" * 65) == "direct"
    assert sanitize_source_tag("کانال") == "direct"


def test_redact_sensitive_text_masks_tokens_keys_and_cards_only() -> None:
    fake_tg = ":".join(["123456789", "AAH_abcdefghijklmnopqrstuvwxyz12345"])
    fake_key = "AIza" + "SyD1234567890abcdefghijklmnopqrstuvwx"
    raw = (
        f"POST https://api.telegram.org/bot{fake_tg}/getFile "
        f"key={fake_key} "
        "cards: 6037-9975-1234-5678, 6037 9975 1234 5678, 6037997512345678 "
        "user_id=123456789 price=490000 last4=5678"
    )
    redacted = redact_sensitive_text(raw, extra_secrets=("custom-secret-val",))

    assert fake_tg not in redacted
    assert fake_key not in redacted
    assert "6037-9975-1234-5678" not in redacted
    assert "6037 9975 1234 5678" not in redacted
    assert "6037997512345678" not in redacted
    assert "user_id=123456789" in redacted
    assert "price=490000" in redacted
    assert "last4=5678" in redacted
    assert "[REDACTED]" in redacted


def test_redact_sensitive_text_masks_extra_secrets() -> None:
    text = "Using token my-explicit-secret and card 5022-2910-0000-1111"
    redacted = redact_sensitive_text(text, extra_secrets=("my-explicit-secret",))
    assert "my-explicit-secret" not in redacted
    assert "5022-2910-0000-1111" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_secret_redacting_log_filter_handles_args_and_exceptions() -> None:
    log_filter = SecretRedactingLogFilter(extra_secrets=("secret-xyz",))
    fake_tg = ":".join(["987654321", "BBH_abcdefghijklmnopqrstuvwxyz12345"])

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Failed calling %s with secret %s",
        args=(f"https://api.telegram.org/bot{fake_tg}/sendMessage", "secret-xyz"),
        exc_info=None,
    )
    assert log_filter.filter(record) is True
    formatted = record.getMessage()
    assert fake_tg not in formatted
    assert "secret-xyz" not in formatted
    assert "[REDACTED]" in formatted


def test_secret_redacting_log_filter_handles_dict_args_and_exc_text() -> None:
    log_filter = SecretRedactingLogFilter()
    gemini_key = "AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz123456789"
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Error %(key)s",
        args=({"key": gemini_key},),
        exc_info=None,
    )
    record.exc_text = f"Traceback: api_key={gemini_key}"
    assert log_filter.filter(record) is True
    assert gemini_key not in record.getMessage()
    assert gemini_key not in (record.exc_text or "")


def test_configure_logging_silences_httpx_and_httpcore() -> None:
    configure_logging("INFO", extra_secrets=("secret-abc",))
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("httpx2").level == logging.WARNING
    assert logging.getLogger("httpcore2").level == logging.WARNING


def test_load_settings_parses_env_and_dotenv_and_validates(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n"
        'TELEGRAM_BOT_TOKEN="111111111:AAH_dotenv_token_12345678901234567890"\n'
        "GEMINI_API_KEY=AIzaDotenvKey1234567890abcdefghijklmnop\n"
        "ADMIN_USER_IDS=101, 202 , 303\n"
        "USD_TO_TOMAN_RATE=273000\n",
        encoding="utf-8",
    )
    settings = load_settings(
        environ={"GEMINI_API_KEY": "AIzaEnvOverrideKey1234567890abcdefghij"},
        env_file=dotenv,
    )
    expected_tg = ":".join(["111111111", "AAH_dotenv_token_12345678901234567890"])
    expected_key = "AIza" + "EnvOverrideKey1234567890abcdefghij"
    assert settings.telegram_bot_token == expected_tg
    assert settings.gemini_api_key == expected_key
    assert settings.admin_user_ids == frozenset({101, 202, 303})
    assert settings.usd_to_toman_rate == 273_000

    with pytest.raises(ValueError, match="BETA_PACK_PRICE_TOMAN"):
        load_settings(
            environ={
                "TELEGRAM_BOT_TOKEN": "123:abc",
                "GEMINI_API_KEY": "AIza123",
                "BETA_PACK_PRICE_TOMAN": "0",
            },
            env_file=None,
        )


def test_logger_exception_redacts_secrets_in_traceback_with_stringio() -> None:
    fake_tg = ":".join(["123456789", "AAH_traceback_token_12345678901234567890"])
    fake_key = "AIza" + "SyTracebackGeminiKey1234567890abcdefghij"
    card = "6037-9975-1234-5678"
    extra = "super-secret-val"

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(SecretRedactingLogFilter(extra_secrets=(extra,)))

    test_logger = logging.getLogger("deutschcoach.test.exc_redaction")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False

    try:
        try:
            raise RuntimeError(f"Failed bot={fake_tg} key={fake_key} card={card} extra={extra}")
        except RuntimeError:
            test_logger.exception("Unhandled error for %s", extra)
    finally:
        test_logger.handlers.clear()

    logged = buf.getvalue()
    assert "Traceback (most recent call last):" in logged
    assert fake_tg not in logged
    assert fake_key not in logged
    assert card not in logged
    assert extra not in logged
    assert "[REDACTED]" in logged

    # Also verify SecretRedactingFormatter independently redacts formatted tracebacks
    buf_fmt = io.StringIO()
    fmt_handler = logging.StreamHandler(buf_fmt)
    fmt_handler.setFormatter(
        SecretRedactingFormatter("%(levelname)s %(message)s", extra_secrets=(extra,))
    )
    fmt_logger = logging.getLogger("deutschcoach.test.exc_formatter")
    fmt_logger.handlers = [fmt_handler]
    fmt_logger.setLevel(logging.ERROR)
    fmt_logger.propagate = False
    try:
        try:
            raise RuntimeError(f"Leak {fake_key} {extra}")
        except RuntimeError:
            fmt_logger.exception("Failure %s", fake_tg)
    finally:
        fmt_logger.handlers.clear()

    logged_fmt = buf_fmt.getvalue()
    assert "Traceback (most recent call last):" in logged_fmt
    assert fake_tg not in logged_fmt
    assert fake_key not in logged_fmt
    assert extra not in logged_fmt
    assert "[REDACTED]" in logged_fmt


def test_load_settings_rejects_reservation_ttl_not_greater_than_gemini_timeout() -> None:
    for ttl in ("60", "30", "104"):
        with pytest.raises(ValueError, match="RESERVATION_TTL_SEC"):
            load_settings(
                environ={
                    "TELEGRAM_BOT_TOKEN": "123:abc",
                    "GEMINI_API_KEY": "AIza123",
                    "GEMINI_TIMEOUT_SEC": "60",
                    "RESERVATION_TTL_SEC": ttl,
                },
                env_file=None,
            )

    valid = load_settings(
        environ={
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "GEMINI_API_KEY": "AIza123",
            "GEMINI_TIMEOUT_SEC": "60",
            "RESERVATION_TTL_SEC": "105",
        },
        env_file=None,
    )
    assert valid.reservation_ttl_sec == 105
    assert valid.max_billed_failed_attempts == 2
