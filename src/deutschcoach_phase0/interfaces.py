from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from deutschcoach_phase0.domain import (
    ApprovePaymentResult,
    AudioValidationResult,
    ConsumeEvalResult,
    CostBreakdown,
    EvaluationReport,
    EvaluatorOutput,
    GateStats,
    IncomingAudioMeta,
    Level,
    QuotaStatus,
    ReservationResult,
    TelemetryEvent,
    Timeline,
    TransientEvaluatorError,
    UserRecord,
)

__all__ = [
    "AudioCleaner",
    "AudioFetcher",
    "AudioInspector",
    "Clock",
    "SpeechEvaluator",
    "StateRepository",
    "TelemetrySink",
    "TransientEvaluatorError",
]


class Clock(Protocol):
    def now(self) -> datetime: ...


class AudioInspector(Protocol):
    def validate_pre_download(self, meta: IncomingAudioMeta) -> AudioValidationResult: ...

    async def inspect_file(self, path: Path, claimed_mime: str | None) -> AudioValidationResult: ...


class AudioFetcher(Protocol):
    async def download_to_path(self, file_id: str, destination: Path) -> int: ...


class AudioCleaner(Protocol):
    def delete_now(self, path: Path) -> None: ...

    def purge_older_than(self, directory: Path, max_age_hours: int = 24) -> int: ...


class SpeechEvaluator(Protocol):
    async def evaluate_audio(
        self,
        audio_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        timeout_sec: int,
    ) -> EvaluatorOutput: ...


class StateRepository(Protocol):
    def init_schema(self) -> None: ...

    def upsert_user_start(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        source_tag: str,
        is_qa: bool,
        now_iso: str,
    ) -> UserRecord: ...

    def get_user(self, telegram_user_id: int) -> UserRecord | None: ...

    def set_user_level(
        self, telegram_user_id: int, level: Level, now_iso: str
    ) -> UserRecord | None: ...

    def set_user_timeline(
        self, telegram_user_id: int, timeline: Timeline, now_iso: str
    ) -> UserRecord | None: ...

    def try_reserve_quota(
        self,
        telegram_user_id: int,
        allow_repeat: bool,
        now_iso: str,
        stale_before_iso: str,
    ) -> ReservationResult: ...

    def release_quota(
        self,
        telegram_user_id: int,
        now_iso: str,
        expected_reserved_at: str | None = None,
        *,
        billed_failure: bool = False,
        max_billed_failures: int = 2,
        allow_repeat: bool = False,
    ) -> QuotaStatus: ...

    def consume_quota_and_record_eval(
        self,
        telegram_user_id: int,
        level: Level,
        timeline: Timeline,
        audio_duration_sec: float,
        evaluator_output: EvaluatorOutput,
        report: EvaluationReport,
        cost: CostBreakdown,
        is_qa: bool,
        now_iso: str,
        expected_reserved_at: str | None = None,
    ) -> ConsumeEvalResult: ...

    def record_failed_eval(
        self,
        telegram_user_id: int,
        level: Level,
        timeline: Timeline,
        audio_duration_sec: float,
        evaluator_output: EvaluatorOutput,
        cost: CostBreakdown,
        reason: str,
        is_qa: bool,
        now_iso: str,
        status: str = "FAILED_PARSE",
    ) -> None: ...

    def mark_cta_clicked(self, telegram_user_id: int, now_iso: str) -> UserRecord | None: ...

    def begin_payment_proof(self, telegram_user_id: int, now_iso: str) -> UserRecord | None: ...

    def mark_payment_submitted(
        self, telegram_user_id: int, payment_ref: str, now_iso: str
    ) -> UserRecord | None: ...

    def approve_payment(
        self,
        telegram_user_id: int,
        tx_ref: str,
        amount_toman: int,
        now_iso: str,
    ) -> ApprovePaymentResult: ...

    def get_gate_stats(self) -> GateStats: ...

    def list_hour_36_followup_candidates(self, eligible_before_iso: str) -> list[UserRecord]: ...

    def mark_followup_36h_sent(self, telegram_user_id: int, now_iso: str) -> None: ...


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...
