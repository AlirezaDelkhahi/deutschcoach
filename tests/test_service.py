from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from deutschcoach_phase0.domain import (
    MSG_AUDIO_TOO_LONG,
    MSG_AUDIO_TOO_SHORT,
    MSG_VPN_OFF_WARNING,
    ApprovePaymentOutcome,
    ApprovePaymentResult,
    AudioRejectionCode,
    AudioValidationResult,
    ConsumeEvalResult,
    CostBreakdown,
    EvaluationReport,
    EvaluatorOutput,
    GateStats,
    IncomingAudioMeta,
    Level,
    QuotaStatus,
    ReservationOutcome,
    ReservationResult,
    TelemetryEvent,
    Timeline,
    TransientEvaluatorError,
    UserRecord,
    UserStep,
)
from deutschcoach_phase0.service import DiagnosticService


class FakeClock:
    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current


class FakeRepo:
    def __init__(self) -> None:
        self.users: dict[int, UserRecord] = {}
        self.success_evals: list[dict[str, object]] = []
        self.failed_evals: list[dict[str, object]] = []
        self.followup_marked: list[int] = []
        self.last_eval_at: dict[int, str] = {}

    def init_schema(self) -> None:
        return None

    def upsert_user_start(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        source_tag: str,
        is_qa: bool,
        now_iso: str,
    ) -> UserRecord:
        existing = self.users.get(telegram_user_id)
        if existing is None:
            user = UserRecord(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                source_tag=source_tag,
                level=None,
                timeline=None,
                step=UserStep.AWAITING_LEVEL,
                quota_status=QuotaStatus.AVAILABLE,
                reserved_at=None,
                cta_clicked=False,
                payment_submitted=False,
                payment_verified=False,
                payment_ref=None,
                payment_amount_toman=None,
                is_qa=is_qa,
                last_score=None,
                last_band=None,
                followup_36h_sent_at=None,
                created_at=now_iso,
                updated_at=now_iso,
            )
        else:
            step = (
                UserStep.AWAITING_LEVEL
                if (existing.quota_status == QuotaStatus.AVAILABLE or is_qa)
                else existing.step
            )
            kept_source = (
                source_tag
                if existing.source_tag == "direct" and source_tag != "direct"
                else existing.source_tag
            )
            user = UserRecord(
                **{
                    **existing.__dict__,
                    "username": username,
                    "first_name": first_name,
                    "source_tag": kept_source,
                    "is_qa": existing.is_qa or is_qa,
                    "step": step,
                    "updated_at": now_iso,
                }
            )
        self.users[telegram_user_id] = user
        return user

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return self.users.get(telegram_user_id)

    def set_user_level(
        self, telegram_user_id: int, level: Level, now_iso: str
    ) -> UserRecord | None:
        user = self.users.get(telegram_user_id)
        if user is None:
            return None
        updated = UserRecord(
            **{
                **user.__dict__,
                "level": level,
                "step": UserStep.AWAITING_TIMELINE,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return updated

    def set_user_timeline(
        self, telegram_user_id: int, timeline: Timeline, now_iso: str
    ) -> UserRecord | None:
        user = self.users.get(telegram_user_id)
        if user is None:
            return None
        updated = UserRecord(
            **{
                **user.__dict__,
                "timeline": timeline,
                "step": UserStep.AWAITING_VOICE,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return updated

    def try_reserve_quota(
        self,
        telegram_user_id: int,
        allow_repeat: bool,
        now_iso: str,
        stale_before_iso: str,
    ) -> ReservationResult:
        user = self.users.get(telegram_user_id)
        if user is None:
            return ReservationResult(False, ReservationOutcome.USER_NOT_FOUND, None)

        is_stale = user.quota_status == QuotaStatus.RESERVED and (
            user.reserved_at is None or user.reserved_at < stale_before_iso
        )
        can_reserve = (
            user.quota_status == QuotaStatus.AVAILABLE
            or is_stale
            or (allow_repeat and user.quota_status == QuotaStatus.CONSUMED)
        )
        if not can_reserve:
            outcome = (
                ReservationOutcome.ALREADY_CONSUMED
                if user.quota_status == QuotaStatus.CONSUMED
                else ReservationOutcome.ALREADY_IN_FLIGHT
            )
            return ReservationResult(False, outcome, user)

        reserved = UserRecord(
            **{
                **user.__dict__,
                "quota_status": QuotaStatus.RESERVED,
                "reserved_at": now_iso,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = reserved
        return ReservationResult(True, ReservationOutcome.RESERVED, reserved)

    def release_quota(
        self,
        telegram_user_id: int,
        now_iso: str,
        expected_reserved_at: str | None = None,
        *,
        billed_failure: bool = False,
        max_billed_failures: int = 2,
        allow_repeat: bool = False,
    ) -> QuotaStatus:
        user = self.users.get(telegram_user_id)
        if user is None:
            return QuotaStatus.AVAILABLE
        if user.quota_status != QuotaStatus.RESERVED:
            return user.quota_status
        if expected_reserved_at is not None and user.reserved_at != expected_reserved_at:
            return user.quota_status

        attempts = user.failed_eval_attempts
        if billed_failure:
            attempts += 1
            if not allow_repeat and attempts >= max_billed_failures:
                updated = UserRecord(
                    **{
                        **user.__dict__,
                        "quota_status": QuotaStatus.CONSUMED,
                        "reserved_at": None,
                        "step": UserStep.AWAITING_CTA,
                        "last_band": "ناموفق",
                        "failed_eval_attempts": attempts,
                        "updated_at": now_iso,
                    }
                )
                self.users[telegram_user_id] = updated
                return QuotaStatus.CONSUMED

        updated = UserRecord(
            **{
                **user.__dict__,
                "quota_status": QuotaStatus.AVAILABLE,
                "reserved_at": None,
                "failed_eval_attempts": attempts,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return QuotaStatus.AVAILABLE

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
    ) -> ConsumeEvalResult:
        user = self.users[telegram_user_id]
        if expected_reserved_at is not None and (
            user.quota_status != QuotaStatus.RESERVED or user.reserved_at != expected_reserved_at
        ):
            return ConsumeEvalResult(user=user, consumed=False)
        self.success_evals.append(
            {
                "telegram_user_id": telegram_user_id,
                "score": report.total_score,
                "cost": cost,
                "is_qa": is_qa,
                "is_valid_attempt": report.is_valid_german_attempt,
                "created_at": now_iso,
            }
        )
        self.last_eval_at[telegram_user_id] = now_iso
        updated = UserRecord(
            **{
                **user.__dict__,
                "quota_status": QuotaStatus.CONSUMED,
                "reserved_at": None,
                "step": UserStep.AWAITING_CTA,
                "last_score": report.total_score,
                "last_band": report.band,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return ConsumeEvalResult(user=updated, consumed=True)

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
    ) -> None:
        self.failed_evals.append(
            {
                "telegram_user_id": telegram_user_id,
                "reason": reason,
                "cost": cost,
                "status": status,
            }
        )

    def mark_cta_clicked(self, telegram_user_id: int, now_iso: str) -> UserRecord | None:
        user = self.users.get(telegram_user_id)
        if user is None:
            return None
        updated = UserRecord(**{**user.__dict__, "cta_clicked": True, "updated_at": now_iso})
        self.users[telegram_user_id] = updated
        return updated

    def begin_payment_proof(self, telegram_user_id: int, now_iso: str) -> UserRecord | None:
        user = self.users.get(telegram_user_id)
        if user is None:
            return None
        updated = UserRecord(
            **{**user.__dict__, "step": UserStep.AWAITING_PAYMENT_PROOF, "updated_at": now_iso}
        )
        self.users[telegram_user_id] = updated
        return updated

    def mark_payment_submitted(
        self, telegram_user_id: int, payment_ref: str, now_iso: str
    ) -> UserRecord | None:
        user = self.users.get(telegram_user_id)
        if user is None:
            return None
        updated = UserRecord(
            **{
                **user.__dict__,
                "payment_submitted": True,
                "payment_ref": payment_ref,
                "step": UserStep.PAYMENT_SUBMITTED,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return updated

    def approve_payment(
        self, telegram_user_id: int, tx_ref: str, amount_toman: int, now_iso: str
    ) -> ApprovePaymentResult:
        user = self.users.get(telegram_user_id)
        if user is None:
            return ApprovePaymentResult(False, ApprovePaymentOutcome.USER_NOT_FOUND)
        if user.payment_verified:
            return ApprovePaymentResult(
                False,
                ApprovePaymentOutcome.ALREADY_VERIFIED,
                user,
                user.payment_amount_toman or 0,
            )
        cleaned_ref = tx_ref.strip()
        if not cleaned_ref:
            return ApprovePaymentResult(False, ApprovePaymentOutcome.INVALID_TX_REF)
        if any(
            u.payment_verified
            and u.payment_ref == cleaned_ref
            and u.telegram_user_id != telegram_user_id
            for u in self.users.values()
        ):
            return ApprovePaymentResult(
                False,
                ApprovePaymentOutcome.DUPLICATE_TX_REF,
                user,
                0,
            )
        updated = UserRecord(
            **{
                **user.__dict__,
                "payment_submitted": True,
                "payment_verified": True,
                "payment_ref": tx_ref,
                "payment_amount_toman": amount_toman,
                "step": UserStep.PAYMENT_VERIFIED,
                "updated_at": now_iso,
            }
        )
        self.users[telegram_user_id] = updated
        return ApprovePaymentResult(True, ApprovePaymentOutcome.APPROVED, updated, amount_toman)

    def get_gate_stats(self) -> GateStats:
        real_users = [u for u in self.users.values() if not u.is_qa]
        return GateStats(
            total_users=len(real_users),
            completed_diagnostics=sum(1 for e in self.success_evals if not e["is_qa"]),
            cta_clicks=sum(1 for u in real_users if u.cta_clicked),
            payment_submissions=sum(1 for u in real_users if u.payment_submitted),
            paid_pre_orders=sum(1 for u in real_users if u.payment_verified),
            total_collected_toman=sum(
                u.payment_amount_toman or 0 for u in real_users if u.payment_verified
            ),
            avg_cost_usd=Decimal("0.005000"),
            avg_cost_toman=1365,
            qa_diagnostics=sum(1 for e in self.success_evals if e["is_qa"]),
            by_source={"direct": len(real_users)},
            by_level={"B1": 1},
            by_band={"قبول": 1},
        )

    def list_hour_36_followup_candidates(self, eligible_before_iso: str) -> list[UserRecord]:
        return [
            u
            for u in self.users.values()
            if not u.is_qa
            and u.quota_status == QuotaStatus.CONSUMED
            and not u.payment_verified
            and u.followup_36h_sent_at is None
            and u.last_score is not None
            and u.last_band is not None
            and (last_at := self.last_eval_at.get(u.telegram_user_id)) is not None
            and last_at <= eligible_before_iso
        ]

    def mark_followup_36h_sent(self, telegram_user_id: int, now_iso: str) -> None:
        self.followup_marked.append(telegram_user_id)
        user = self.users.get(telegram_user_id)
        if user is not None:
            self.users[telegram_user_id] = UserRecord(
                **{**user.__dict__, "followup_36h_sent_at": now_iso}
            )


class FakeTelemetry:
    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)


class FakeInspector:
    def __init__(
        self,
        pre_result: AudioValidationResult | None = None,
        post_result: AudioValidationResult | None = None,
        create_normalized: bool = False,
    ) -> None:
        self.pre_result = pre_result
        self.post_result = post_result or AudioValidationResult(ok=True, duration_sec=60.0)
        self.create_normalized = create_normalized

    def validate_pre_download(self, meta: IncomingAudioMeta) -> AudioValidationResult:
        if self.pre_result is not None:
            return self.pre_result
        if meta.reported_duration_sec is not None and meta.reported_duration_sec < 15:
            return AudioValidationResult(
                ok=False,
                rejection_code=AudioRejectionCode.TOO_SHORT,
                user_message_fa=MSG_AUDIO_TOO_SHORT,
            )
        if meta.reported_duration_sec is not None and meta.reported_duration_sec > 125:
            return AudioValidationResult(
                ok=False,
                rejection_code=AudioRejectionCode.TOO_LONG,
                user_message_fa=MSG_AUDIO_TOO_LONG,
            )
        return AudioValidationResult(ok=True, duration_sec=meta.reported_duration_sec)

    async def inspect_file(self, path: Path, claimed_mime: str | None) -> AudioValidationResult:
        if self.create_normalized and self.post_result.ok:
            norm = path.with_name(f"{path.stem}.normalized.ogg")
            norm.write_bytes(b"normalized-ogg-bytes")
            return AudioValidationResult(
                ok=True,
                duration_sec=self.post_result.duration_sec,
                size_bytes=len(b"normalized-ogg-bytes"),
                detected_format="ogg",
                normalized_path=norm,
            )
        return self.post_result


class FakeFetcher:
    def __init__(self, payload: bytes = b"fake-ogg-bytes") -> None:
        self.payload = payload
        self.calls: int = 0

    async def download_to_path(self, file_id: str, destination: Path) -> int:
        self.calls += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return len(self.payload)


class FakeCleaner:
    def __init__(self) -> None:
        self.deleted_paths: list[Path] = []

    def delete_now(self, path: Path) -> None:
        self.deleted_paths.append(path)
        path.unlink(missing_ok=True)

    def purge_older_than(self, directory: Path, max_age_hours: int = 24) -> int:
        return 0


class FakeEvaluator:
    def __init__(
        self,
        raw_text: str = (
            '{"is_valid_german_attempt":true,"transcript_summary_de":"Ich nutze ein Handy.",'
            '"total_score":66,"band":"قبول","subscores":{"task_fulfillment":17,'
            '"coherence_interaction":16,"vocab_grammar":16,"pronunciation_fluency":17},'
            '"top_3_errors":[],"strength_fa":"خوب","timeline_action_plan_fa":"تمرین کنید"}'
        ),
        delay_sec: float = 0.0,
        raise_transient: bool = False,
        potentially_billed_transient: bool = False,
        audio_input_tokens: int = 1920,
    ) -> None:
        self.raw_text = raw_text
        self.delay_sec = delay_sec
        self.raise_transient = raise_transient
        self.potentially_billed_transient = potentially_billed_transient
        self.audio_input_tokens = audio_input_tokens
        self.calls: int = 0
        self.seen_bytes: list[bytes] = []
        self.seen_mimes: list[str] = []

    async def evaluate_audio(
        self,
        audio_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        timeout_sec: int,
    ) -> EvaluatorOutput:
        self.calls += 1
        self.seen_bytes.append(audio_bytes)
        self.seen_mimes.append(mime_type)
        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
        if self.raise_transient:
            raise TransientEvaluatorError(
                "Gemini timeout",
                potentially_billed=self.potentially_billed_transient,
            )
        return EvaluatorOutput(
            raw_text=self.raw_text,
            prompt_tokens=self.audio_input_tokens + 400,
            candidates_tokens=500,
            thoughts_tokens=100,
            audio_input_tokens=self.audio_input_tokens,
            text_input_tokens=400,
            model_name="gemini-2.5-flash",
        )


def _build_service(
    sample_settings,
    repo=None,
    telemetry=None,
    inspector=None,
    fetcher=None,
    cleaner=None,
    evaluator=None,
    clock=None,
):
    repo = repo or FakeRepo()
    telemetry = telemetry or FakeTelemetry()
    inspector = inspector or FakeInspector()
    fetcher = fetcher or FakeFetcher()
    cleaner = cleaner or FakeCleaner()
    evaluator = evaluator or FakeEvaluator()
    clock = clock or FakeClock()
    svc = DiagnosticService(
        config=sample_settings.to_service_config(),
        repo=repo,
        telemetry=telemetry,
        audio_inspector=inspector,
        audio_fetcher=fetcher,
        audio_cleaner=cleaner,
        evaluator=evaluator,
        clock=clock,
    )
    return svc, repo, telemetry, fetcher, cleaner, evaluator


@pytest.mark.asyncio
async def test_service_happy_path_and_cleanup(sample_settings) -> None:
    svc, repo, telemetry, fetcher, cleaner, evaluator = _build_service(sample_settings)

    start_reply = await svc.handle_start(101, "ali", "Ali", "tg_group_1")
    assert "B1" in str(start_reply.messages[0].inline_keyboard)

    lvl_reply = await svc.handle_level_selected(101, "B1")
    assert "کمتر از ۱ ماه دیگر" in str(lvl_reply.messages[0].inline_keyboard)

    tl_reply = await svc.handle_timeline_selected(101, "<1 month")
    assert "Brauchen Jugendliche heute ein eigenes Smartphone?" in tl_reply.messages[0].text

    voice_reply = await svc.handle_voice(
        101, IncomingAudioMeta("file-1", "uniq-1", "audio/ogg", 150_000, 60.0)
    )
    assert evaluator.calls == 1
    assert fetcher.calls == 1
    assert len(cleaner.deleted_paths) == 1
    assert repo.get_user(101).quota_status == QuotaStatus.CONSUMED
    assert len(voice_reply.messages) == 2
    assert "66/100" in voice_reply.messages[0].text
    assert "490,000" in voice_reply.messages[1].text
    assert any(ev.event_type == "eval_completed" for ev in telemetry.events)


@pytest.mark.asyncio
async def test_service_pre_and_post_download_guards_preserve_quota_and_skip_gemini(
    sample_settings,
) -> None:
    # Pre-download < 15s rejection
    svc1, repo1, _, fetcher1, _, eval1 = _build_service(sample_settings)
    await svc1.handle_start(102, None, "Reza", None)
    await svc1.handle_level_selected(102, "B1")
    await svc1.handle_timeline_selected(102, "<1 month")

    short_reply = await svc1.handle_voice(
        102, IncomingAudioMeta("f-short", "u-short", "audio/ogg", 10_000, 10.0)
    )
    assert short_reply.messages[0].text == MSG_AUDIO_TOO_SHORT
    assert fetcher1.calls == 0
    assert eval1.calls == 0
    assert repo1.get_user(102).quota_status == QuotaStatus.AVAILABLE

    # Post-download spoofed metadata caught by ffprobe (> 125s)
    spoof_inspector = FakeInspector(
        post_result=AudioValidationResult(
            ok=False,
            duration_sec=130.0,
            rejection_code=AudioRejectionCode.TOO_LONG,
            user_message_fa=MSG_AUDIO_TOO_LONG,
        )
    )
    svc2, repo2, _, fetcher2, cleaner2, eval2 = _build_service(
        sample_settings, inspector=spoof_inspector
    )
    await svc2.handle_start(103, None, "Mina", None)
    await svc2.handle_level_selected(103, "B2")
    await svc2.handle_timeline_selected(103, "1-3 months")

    spoof_reply = await svc2.handle_voice(
        103, IncomingAudioMeta("f-spoof", "u-spoof", "audio/ogg", 100_000, 60.0)
    )
    assert spoof_reply.messages[0].text == MSG_AUDIO_TOO_LONG
    assert fetcher2.calls == 1
    assert eval2.calls == 0
    assert len(cleaner2.deleted_paths) == 1
    assert repo2.get_user(103).quota_status == QuotaStatus.AVAILABLE


@pytest.mark.asyncio
async def test_service_concurrent_submissions_trigger_at_most_one_eval(sample_settings) -> None:
    slow_eval = FakeEvaluator(delay_sec=0.05)
    svc, repo, _, _, _, _ = _build_service(sample_settings, evaluator=slow_eval)
    await svc.handle_start(104, None, "Sara", None)
    await svc.handle_level_selected(104, "B1")
    await svc.handle_timeline_selected(104, "Just starting")

    meta = IncomingAudioMeta("f-conc", "u-conc", "audio/ogg", 100_000, 60.0)
    r1, r2 = await asyncio.gather(svc.handle_voice(104, meta), svc.handle_voice(104, meta))

    assert slow_eval.calls == 1
    texts = [r.messages[0].text for r in (r1, r2)]
    assert any("66/100" in t for t in texts)
    assert any("در حال پردازش" in t or "قبلاً" in t for t in texts)
    assert repo.get_user(104).quota_status == QuotaStatus.CONSUMED


@pytest.mark.asyncio
async def test_service_transient_error_and_malformed_json_preserve_quota(sample_settings) -> None:
    # Transient error
    transient_eval = FakeEvaluator(raise_transient=True)
    svc1, repo1, tel1, _, cleaner1, _ = _build_service(sample_settings, evaluator=transient_eval)
    await svc1.handle_start(105, None, "Omid", None)
    await svc1.handle_level_selected(105, "B1")
    await svc1.handle_timeline_selected(105, "<1 month")
    r_err = await svc1.handle_voice(105, IncomingAudioMeta("f1", "u1", "audio/ogg", 100_000, 50.0))
    assert repo1.get_user(105).quota_status == QuotaStatus.AVAILABLE
    assert len(cleaner1.deleted_paths) == 1
    assert "Traceback" not in r_err.messages[0].text
    assert any(e.event_type == "eval_transient_error" for e in tel1.events)

    # Unparseable JSON still logs cost & releases quota
    bad_json_eval = FakeEvaluator(raw_text='{"total_score": 70')
    svc2, repo2, tel2, _, cleaner2, _ = _build_service(sample_settings, evaluator=bad_json_eval)
    await svc2.handle_start(106, None, "Neda", None)
    await svc2.handle_level_selected(106, "B2")
    await svc2.handle_timeline_selected(106, "<1 month")
    r_bad = await svc2.handle_voice(106, IncomingAudioMeta("f2", "u2", "audio/ogg", 100_000, 50.0))
    assert repo2.get_user(106).quota_status == QuotaStatus.AVAILABLE
    assert len(cleaner2.deleted_paths) == 1
    assert "Traceback" not in r_bad.messages[0].text
    failed_events = [e for e in tel2.events if e.event_type == "eval_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].estimated_cost_usd is not None
    assert failed_events[0].estimated_cost_toman is not None


@pytest.mark.asyncio
async def test_service_cta_payment_admin_auth_and_followup(sample_settings) -> None:
    clock = FakeClock()
    svc, repo, telemetry, _, _, _ = _build_service(sample_settings, clock=clock)
    await svc.handle_start(201, "buyer", "Kian", "tutor_x")
    await svc.handle_level_selected(201, "B1")
    await svc.handle_timeline_selected(201, "<1 month")
    await svc.handle_voice(201, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 60.0))

    cta_reply = await svc.handle_cta_click(201)
    assert MSG_VPN_OFF_WARNING in cta_reply.messages[0].text

    await svc.handle_start_payment_proof(201)
    proof_reply = await svc.handle_payment_proof(
        201, "Paid with card 6037-9975-1234-5678 ref ZP99", has_photo=False
    )
    assert proof_reply.admin_notification is not None
    assert proof_reply.admin_notification.photo_file_id is None
    assert "6037-9975-1234-5678" not in proof_reply.admin_notification.text
    assert "/approve 201" in proof_reply.admin_notification.text
    assert "6037-9975-1234-5678" not in (repo.get_user(201).payment_ref or "")

    # Non-admin denied
    denied = await svc.handle_admin_approve(201, 201, "ZP99")
    assert "دسترسی" in denied.messages[0].text
    assert repo.get_user(201).payment_verified is False

    # Admin approves idempotently
    approved = await svc.handle_admin_approve(999001, 201, "ZP99")
    assert repo.get_user(201).payment_verified is True
    assert len(approved.outbound_messages) == 1
    repeat_approve = await svc.handle_admin_approve(999001, 201, "ZP99")
    assert "قبلاً" in repeat_approve.messages[0].text

    # Stats check
    stats_reply = await svc.handle_admin_stats(999001)
    assert "490,000" in stats_reply.messages[0].text

    # Follow-up candidate for unpaid user: < 36h must NOT match
    await svc.handle_start(202, "unpaid", "Tara", "direct")
    await svc.handle_level_selected(202, "B1")
    await svc.handle_timeline_selected(202, "1-3 months")
    await svc.handle_voice(202, IncomingAudioMeta("f2", "u2", "audio/ogg", 100_000, 60.0))

    clock.current += timedelta(hours=35, minutes=59)
    fu_early = await svc.handle_admin_followup(999001)
    assert fu_early.outbound_messages == ()

    # After 36h+, unpaid user matches while paid user 201 remains excluded
    clock.current += timedelta(minutes=2)
    fu_reply = await svc.handle_admin_followup(999001)
    assert len(fu_reply.outbound_messages) == 1
    assert fu_reply.outbound_messages[0].chat_id == 202
    assert "66/100" in fu_reply.outbound_messages[0].message.text


@pytest.mark.asyncio
async def test_service_admin_qa_allows_repeat_while_regular_user_blocked(sample_settings) -> None:
    svc, repo, _, _, _, evaluator = _build_service(sample_settings)
    meta = IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 60.0)

    # Regular user blocked on second eval
    await svc.handle_start(301, None, "User", None)
    await svc.handle_level_selected(301, "B1")
    await svc.handle_timeline_selected(301, "<1 month")
    await svc.handle_voice(301, meta)
    blocked = await svc.handle_voice(301, meta)
    assert evaluator.calls == 1
    assert "استفاده کرده‌اید" in blocked.messages[0].text

    # Admin QA allowed repeatedly
    await svc.handle_start(999001, "admin", "Admin", None)
    await svc.handle_level_selected(999001, "B2")
    await svc.handle_timeline_selected(999001, "Just starting")
    await svc.handle_voice(999001, meta)
    await svc.handle_voice(999001, meta)
    assert evaluator.calls == 3
    assert repo.get_user(999001).is_qa is True


@pytest.mark.asyncio
async def test_service_photo_receipt_without_caption_passes_transient_file_id(
    sample_settings,
) -> None:
    svc, repo, telemetry, _, _, _ = _build_service(sample_settings)
    await svc.handle_start(401, "payer", "Nima", "tg_chan")
    await svc.handle_level_selected(401, "B1")
    await svc.handle_timeline_selected(401, "<1 month")
    await svc.handle_voice(401, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 60.0))
    await svc.handle_cta_click(401)
    await svc.handle_start_payment_proof(401)

    receipt_file_id = "AgACAgQAAxkBAAIB_photo_receipt_id"
    reply = await svc.handle_text_message(
        401,
        None,
        has_photo=True,
        photo_file_id=receipt_file_id,
    )
    assert reply.admin_notification is not None
    assert reply.admin_notification.photo_file_id == receipt_file_id
    assert "photo_receipt" in reply.admin_notification.text
    assert "/approve 401 TX_REF" in reply.admin_notification.text
    assert repo.get_user(401).payment_ref == "photo_receipt"
    assert receipt_file_id not in str(repo.get_user(401))
    assert all(receipt_file_id not in str(ev) for ev in telemetry.events)


@pytest.mark.asyncio
async def test_service_empty_json_and_nan_preserve_quota(sample_settings) -> None:
    for idx, raw in enumerate(
        ("{}", '{"unexpected": "error"}', '{"total_score": NaN, "subscores": {}}')
    ):
        uid = 402 + idx
        bad_eval = FakeEvaluator(raw_text=raw)
        svc, repo, telemetry, _, cleaner, _ = _build_service(sample_settings, evaluator=bad_eval)
        await svc.handle_start(uid, None, "Pari", None)
        await svc.handle_level_selected(uid, "B1")
        await svc.handle_timeline_selected(uid, "<1 month")

        res = await svc.handle_voice(uid, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 50.0))
        assert "مصرف نشد" in res.messages[0].text
        assert repo.get_user(uid).quota_status == QuotaStatus.AVAILABLE
        assert repo.get_user(uid).failed_eval_attempts == 1
        assert len(cleaner.deleted_paths) == 1
        assert len(repo.failed_evals) == 1
        assert repo.failed_evals[0]["reason"] == "missing_core_evaluation_fields"
        assert any(ev.event_type == "eval_failed" for ev in telemetry.events)


@pytest.mark.asyncio
async def test_service_reads_normalized_audio_and_cleans_both_temp_and_normalized(
    sample_settings,
) -> None:
    inspector = FakeInspector(create_normalized=True)
    svc, _, _, _, cleaner, evaluator = _build_service(sample_settings, inspector=inspector)
    await svc.handle_start(410, None, "Aria", None)
    await svc.handle_level_selected(410, "B1")
    await svc.handle_timeline_selected(410, "<1 month")

    res = await svc.handle_voice(
        410, IncomingAudioMeta("f-mp4", "u-mp4", "audio/mp4", 100_000, 60.0)
    )
    assert "66/100" in res.messages[0].text
    assert evaluator.seen_bytes == [b"normalized-ogg-bytes"]
    assert evaluator.seen_mimes == ["audio/ogg"]
    assert len(cleaner.deleted_paths) == 2
    assert any(p.name.endswith(".normalized.ogg") for p in cleaner.deleted_paths)
    assert all(not p.exists() for p in cleaner.deleted_paths)


@pytest.mark.asyncio
async def test_service_audio_token_anomaly_force_consumes_quota_even_on_parse_failure(
    sample_settings,
) -> None:
    bad_eval = FakeEvaluator(raw_text="{broken", audio_input_tokens=5000)
    svc, repo, _, _, _, _ = _build_service(sample_settings, evaluator=bad_eval)
    await svc.handle_start(411, None, "Abuse", None)
    await svc.handle_level_selected(411, "B1")
    await svc.handle_timeline_selected(411, "<1 month")

    res = await svc.handle_voice(411, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 60.0))
    assert repo.get_user(411).quota_status == QuotaStatus.CONSUMED
    assert len(repo.failed_evals) == 1
    assert "سهمیه" in res.messages[0].text
    assert res.messages[0].inline_keyboard


@pytest.mark.asyncio
async def test_service_caps_billed_failures_at_max_while_unbilled_retry_preserved(
    sample_settings,
) -> None:
    # Unbilled transient errors never increment failed_eval_attempts
    unbilled_eval = FakeEvaluator(raise_transient=True, potentially_billed_transient=False)
    svc1, repo1, _, _, _, _ = _build_service(sample_settings, evaluator=unbilled_eval)
    await svc1.handle_start(412, None, "RetryOK", None)
    await svc1.handle_level_selected(412, "B1")
    await svc1.handle_timeline_selected(412, "<1 month")
    for _ in range(3):
        r = await svc1.handle_voice(412, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 50.0))
        assert "مصرف نشد" in r.messages[0].text
    assert repo1.get_user(412).quota_status == QuotaStatus.AVAILABLE
    assert repo1.get_user(412).failed_eval_attempts == 0

    # 2 billed failures (1 timeout + 1 parse failure) lock regular user to CONSUMED
    billed_timeout_eval = FakeEvaluator(raise_transient=True, potentially_billed_transient=True)
    repo2 = FakeRepo()
    svc2, _, _, _, _, _ = _build_service(sample_settings, repo=repo2, evaluator=billed_timeout_eval)
    await svc2.handle_start(413, None, "Locked", None)
    await svc2.handle_level_selected(413, "B1")
    await svc2.handle_timeline_selected(413, "<1 month")

    r1 = await svc2.handle_voice(413, IncomingAudioMeta("f1", "u1", "audio/ogg", 100_000, 50.0))
    assert "مصرف نشد" in r1.messages[0].text
    assert repo2.get_user(413).quota_status == QuotaStatus.AVAILABLE
    assert repo2.get_user(413).failed_eval_attempts == 1
    assert len(repo2.failed_evals) == 1
    assert repo2.failed_evals[0]["status"] == "TIMEOUT"
    assert repo2.failed_evals[0]["cost"].estimated_cost_toman > 0

    parse_fail_eval = FakeEvaluator(raw_text="{bad")
    svc2_b, _, _, _, _, _ = _build_service(sample_settings, repo=repo2, evaluator=parse_fail_eval)
    r2 = await svc2_b.handle_voice(413, IncomingAudioMeta("f2", "u2", "audio/ogg", 100_000, 50.0))
    assert repo2.get_user(413).quota_status == QuotaStatus.CONSUMED
    assert repo2.get_user(413).failed_eval_attempts == 2
    assert r2.messages[0].inline_keyboard

    # 3rd attempt blocked before calling evaluator
    ok_eval = FakeEvaluator()
    svc2_c, _, _, _, _, _ = _build_service(sample_settings, repo=repo2, evaluator=ok_eval)
    r3 = await svc2_c.handle_voice(413, IncomingAudioMeta("f3", "u3", "audio/ogg", 100_000, 50.0))
    assert ok_eval.calls == 0
    assert "استفاده کرده‌اید" in r3.messages[0].text


@pytest.mark.asyncio
async def test_service_stale_superseded_eval_does_not_emit_completed_or_scorecard(
    sample_settings,
) -> None:
    class StealingRepo(FakeRepo):
        def consume_quota_and_record_eval(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Simulate newer reservation having overwritten reserved_at before stale worker finished
            uid = args[0] if args else kwargs["telegram_user_id"]
            u = self.users[uid]
            self.users[uid] = UserRecord(
                **{**u.__dict__, "reserved_at": "2099-01-01T00:00:00+00:00"}
            )
            return super().consume_quota_and_record_eval(*args, **kwargs)

    repo = StealingRepo()
    svc, _, telemetry, _, _, _ = _build_service(sample_settings, repo=repo)
    await svc.handle_start(414, None, "Stale", None)
    await svc.handle_level_selected(414, "B1")
    await svc.handle_timeline_selected(414, "<1 month")

    res = await svc.handle_voice(414, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 50.0))
    assert not any(ev.event_type == "eval_completed" for ev in telemetry.events)
    assert "66/100" not in res.messages[0].text
    assert len(res.messages) == 1


@pytest.mark.asyncio
async def test_service_non_fatal_telemetry_failure_still_delivers_scorecard(
    sample_settings,
) -> None:
    class BrokenTelemetry:
        def emit(self, event: TelemetryEvent) -> None:
            raise OSError("disk full")

    svc, repo, _, _, _, _ = _build_service(sample_settings, telemetry=BrokenTelemetry())
    await svc.handle_start(415, None, "Safe", None)
    await svc.handle_level_selected(415, "B1")
    await svc.handle_timeline_selected(415, "<1 month")

    res = await svc.handle_voice(415, IncomingAudioMeta("f", "u", "audio/ogg", 100_000, 50.0))
    assert repo.get_user(415).quota_status == QuotaStatus.CONSUMED
    assert "66/100" in res.messages[0].text


@pytest.mark.asyncio
async def test_service_duplicate_tx_ref_admin_approve_returns_warning(sample_settings) -> None:
    svc, repo, _, _, _, _ = _build_service(sample_settings)
    await svc.handle_start(416, "u1", "A", None)
    await svc.handle_start(417, "u2", "B", None)

    ok_res = await svc.handle_admin_approve(999001, 416, "ZP-SHARED")
    assert repo.get_user(416).payment_verified is True
    assert "ثبت شد" in ok_res.messages[0].text

    dup_res = await svc.handle_admin_approve(999001, 417, "ZP-SHARED")
    assert repo.get_user(417).payment_verified is False
    assert "دیگری" in dup_res.messages[0].text
    assert dup_res.outbound_messages == ()
