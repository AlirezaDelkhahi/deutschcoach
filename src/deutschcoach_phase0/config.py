from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deutschcoach_phase0.domain import ServiceConfig

VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"
    gemini_thinking_budget: int = 512
    gemini_timeout_sec: int = 60
    admin_user_ids: frozenset[int] = frozenset()
    admin_notify_chat_id: int | None = None
    data_dir: Path = Path("./data")
    tmp_audio_dir: Path = Path("./tmp_audio")
    zarinlink_url: str = ""
    card_to_card_number: str = ""
    card_to_card_holder: str = ""
    founder_support_handle: str = ""
    usd_to_toman_rate: int = 273_000
    beta_pack_price_toman: int = 490_000
    regular_pack_price_toman: int = 790_000
    reservation_ttl_sec: int = 180
    max_billed_failed_attempts: int = 2
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.telegram_bot_token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN must not be empty")
        if not self.gemini_api_key.strip():
            raise ValueError("GEMINI_API_KEY must not be empty")
        if not self.gemini_model.strip():
            raise ValueError("GEMINI_MODEL must not be empty")

        _require_non_negative_int(self.gemini_thinking_budget, "GEMINI_THINKING_BUDGET")
        _require_positive_int(self.gemini_timeout_sec, "GEMINI_TIMEOUT_SEC")
        _require_positive_int(self.usd_to_toman_rate, "USD_TO_TOMAN_RATE")
        _require_positive_int(self.beta_pack_price_toman, "BETA_PACK_PRICE_TOMAN")
        _require_positive_int(self.regular_pack_price_toman, "REGULAR_PACK_PRICE_TOMAN")
        _require_positive_int(self.reservation_ttl_sec, "RESERVATION_TTL_SEC")
        _require_positive_int(self.max_billed_failed_attempts, "MAX_BILLED_FAILED_ATTEMPTS")
        if self.reservation_ttl_sec < self.gemini_timeout_sec + 45:
            raise ValueError("RESERVATION_TTL_SEC must be at least GEMINI_TIMEOUT_SEC + 45")

        normalized_level = self.log_level.strip().upper()
        if normalized_level not in VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}")
        object.__setattr__(self, "log_level", normalized_level)

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "deutschcoach_phase0.db"

    @property
    def telemetry_path(self) -> Path:
        return self.data_dir / "telemetry.jsonl"

    def sensitive_values(self) -> tuple[str, ...]:
        secrets: list[str] = [self.telegram_bot_token, self.gemini_api_key]
        card = self.card_to_card_number.strip()
        if card:
            secrets.append(card)
            digits_only = "".join(ch for ch in card if ch.isdigit())
            if digits_only and digits_only != card:
                secrets.append(digits_only)
        return tuple(s for s in secrets if s)

    def to_service_config(self) -> ServiceConfig:
        return ServiceConfig(
            gemini_model=self.gemini_model,
            gemini_timeout_sec=self.gemini_timeout_sec,
            admin_user_ids=self.admin_user_ids,
            tmp_audio_dir=self.tmp_audio_dir,
            zarinlink_url=self.zarinlink_url,
            card_to_card_number=self.card_to_card_number,
            card_to_card_holder=self.card_to_card_holder,
            founder_support_handle=self.founder_support_handle,
            usd_to_toman_rate=self.usd_to_toman_rate,
            beta_pack_price_toman=self.beta_pack_price_toman,
            regular_pack_price_toman=self.regular_pack_price_toman,
            reservation_ttl_sec=self.reservation_ttl_sec,
            max_billed_failed_attempts=self.max_billed_failed_attempts,
            sensitive_values=self.sensitive_values(),
        )


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _parse_int(raw: str, name: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_admin_user_ids(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()
    result: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        result.add(_parse_int(token, "ADMIN_USER_IDS"))
    return frozenset(result)


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file without mutating os.environ."""
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        parsed[key] = val
    return parsed


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
) -> Settings:
    """Load and validate Phase 0 Settings from environment variables and optional .env file."""
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_dotenv(env_file))
    source_env = os.environ if environ is None else environ
    for key, val in source_env.items():
        merged[key] = val

    def get_str(key: str, default: str = "") -> str:
        return merged.get(key, default).strip()

    def get_int(key: str, default: int) -> int:
        raw = merged.get(key)
        if raw is None or not raw.strip():
            return default
        return _parse_int(raw, key)

    notify_raw = get_str("ADMIN_NOTIFY_CHAT_ID")
    admin_notify_chat_id = _parse_int(notify_raw, "ADMIN_NOTIFY_CHAT_ID") if notify_raw else None

    return Settings(
        telegram_bot_token=get_str("TELEGRAM_BOT_TOKEN"),
        gemini_api_key=get_str("GEMINI_API_KEY"),
        gemini_model=get_str("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_thinking_budget=get_int("GEMINI_THINKING_BUDGET", 512),
        gemini_timeout_sec=get_int("GEMINI_TIMEOUT_SEC", 60),
        admin_user_ids=_parse_admin_user_ids(get_str("ADMIN_USER_IDS")),
        admin_notify_chat_id=admin_notify_chat_id,
        data_dir=Path(get_str("DATA_DIR", "./data")),
        tmp_audio_dir=Path(get_str("TMP_AUDIO_DIR", "./tmp_audio")),
        zarinlink_url=get_str("ZARINLINK_URL"),
        card_to_card_number=get_str("CARD_TO_CARD_NUMBER"),
        card_to_card_holder=get_str("CARD_TO_CARD_HOLDER"),
        founder_support_handle=get_str("FOUNDER_SUPPORT_HANDLE"),
        usd_to_toman_rate=get_int("USD_TO_TOMAN_RATE", 273_000),
        beta_pack_price_toman=get_int("BETA_PACK_PRICE_TOMAN", 490_000),
        regular_pack_price_toman=get_int("REGULAR_PACK_PRICE_TOMAN", 790_000),
        reservation_ttl_sec=get_int("RESERVATION_TTL_SEC", 180),
        max_billed_failed_attempts=get_int("MAX_BILLED_FAILED_ATTEMPTS", 2),
        log_level=get_str("LOG_LEVEL", "INFO"),
    )
