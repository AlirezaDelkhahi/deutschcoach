from __future__ import annotations

from pathlib import Path

import pytest

from deutschcoach_phase0.config import Settings


@pytest.fixture
def sample_settings(tmp_path: Path) -> Settings:
    """Return isolated Settings pointing to temporary directories."""
    data_dir = tmp_path / "data"
    tmp_audio_dir = tmp_path / "tmp_audio"
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp_audio_dir.mkdir(parents=True, exist_ok=True)
    fake_tg_token = ":".join(["123456789", "AAH_test_bot_token_abcdefghijklmnopqrstuvwxyz"])
    fake_gemini_key = "AIza" + "SyTestGeminiApiKey1234567890abcdefghij"
    return Settings(
        telegram_bot_token=fake_tg_token,
        gemini_api_key=fake_gemini_key,
        gemini_model="gemini-2.5-flash",
        gemini_thinking_budget=512,
        gemini_timeout_sec=60,
        admin_user_ids=frozenset({999001}),
        admin_notify_chat_id=999001,
        data_dir=data_dir,
        tmp_audio_dir=tmp_audio_dir,
        zarinlink_url="https://zarinp.al/deutschcoach-beta",
        card_to_card_number="6037-9975-1234-5678",
        card_to_card_holder="علی رضایی",
        founder_support_handle="@deutschcoach_support",
        usd_to_toman_rate=273_000,
        beta_pack_price_toman=490_000,
        regular_pack_price_toman=790_000,
        reservation_ttl_sec=180,
        log_level="INFO",
    )
