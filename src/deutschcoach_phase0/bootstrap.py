from __future__ import annotations

import logging
import warnings
from datetime import UTC, datetime

from telegram.ext import Application, ContextTypes
from telegram.warnings import PTBUserWarning

from deutschcoach_phase0.audio_guard import FfprobeAudioInspector, FilesystemAudioCleaner
from deutschcoach_phase0.bot import BotHandlers, TelegramAudioFetcher, register_handlers
from deutschcoach_phase0.config import Settings, load_settings
from deutschcoach_phase0.gemini_evaluator import GeminiSpeechEvaluator
from deutschcoach_phase0.security import configure_logging
from deutschcoach_phase0.service import DiagnosticService
from deutschcoach_phase0.storage import (
    DualTelemetrySink,
    JsonlTelemetrySink,
    SQLiteStateRepository,
)

logger = logging.getLogger(__name__)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def create_application(settings: Settings) -> Application:
    """Wire all concrete Phase 0 adapters into the Telegram Application."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_audio_dir.mkdir(parents=True, exist_ok=True)

    cleaner = FilesystemAudioCleaner()
    purged = cleaner.purge_older_than(settings.tmp_audio_dir, max_age_hours=24)
    if purged:
        logger.info("Purged %d stale temporary audio files on startup", purged)

    repo = SQLiteStateRepository(settings.sqlite_path)
    repo.init_schema()

    jsonl_sink = JsonlTelemetrySink(
        settings.telemetry_path,
        extra_secrets=settings.sensitive_values(),
    )
    telemetry = DualTelemetrySink(repo, jsonl_sink)
    inspector = FfprobeAudioInspector()
    evaluator = GeminiSpeechEvaluator(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        thinking_budget=settings.gemini_thinking_budget,
        extra_secrets=settings.sensitive_values(),
    )

    async def _post_shutdown(_app: Application) -> None:
        await evaluator.aclose()

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(32)
        .post_shutdown(_post_shutdown)
        .build()
    )

    service = DiagnosticService(
        config=settings.to_service_config(),
        repo=repo,
        telemetry=telemetry,
        audio_inspector=inspector,
        audio_fetcher=TelegramAudioFetcher(app.bot),
        audio_cleaner=cleaner,
        evaluator=evaluator,
        clock=SystemClock(),
    )
    handlers = BotHandlers(
        service=service,
        admin_notify_chat_id=settings.admin_notify_chat_id,
        extra_secrets=settings.sensitive_values(),
        admin_user_ids=settings.admin_user_ids,
    )
    register_handlers(app, handlers)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)
        job_queue = app.job_queue
    if job_queue is not None:

        async def _purge_stale_audio(_context: ContextTypes.DEFAULT_TYPE) -> None:
            removed = cleaner.purge_older_than(settings.tmp_audio_dir, max_age_hours=24)
            if removed:
                logger.info("Periodic cleanup purged %d stale temporary audio files", removed)

        job_queue.run_repeating(_purge_stale_audio, interval=3600, first=3600)

    return app


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, extra_secrets=settings.sensitive_values())
    app = create_application(settings)
    logger.info("Starting DeutschCoach Phase 0 Telegram validation bot")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
