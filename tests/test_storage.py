from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from deutschcoach_phase0.domain import (
    ApprovePaymentOutcome,
    CostBreakdown,
    EvaluationErrorItem,
    EvaluationReport,
    EvaluationSubscores,
    EvaluatorOutput,
    Level,
    QuotaStatus,
    ReservationOutcome,
    TelemetryEvent,
    Timeline,
    UserStep,
)
from deutschcoach_phase0.storage import (
    DualTelemetrySink,
    JsonlTelemetrySink,
    SQLiteStateRepository,
)


def _sample_report(score: int = 64, band: str = "قبول") -> EvaluationReport:
    return EvaluationReport(
        is_valid_german_attempt=True,
        transcript_summary_de="Ich finde Smartphones nützlich.",
        total_score=score,
        band=band,
        subscores=EvaluationSubscores(16, 16, 16, 16),
        top_3_errors=(
            EvaluationErrorItem(
                category="Grammatik",
                exact_spoken_excerpt="ich habe gesagt dass es ist gut",
                corrected_german="ich habe gesagt, dass es gut ist",
                explanation_fa="فعل جمله پیرو باید در انتها قرار گیرد.",
            ),
        ),
        strength_fa="بیان روان و واضح.",
        timeline_action_plan_fa="تمرین جملات پیرو.",
    )


def _sample_cost() -> CostBreakdown:
    return CostBreakdown(
        audio_input_tokens=2000,
        text_input_tokens=400,
        output_tokens=600,
        total_input_tokens=2400,
        estimated_cost_usd=Decimal("0.003620"),
        estimated_cost_toman=988,
    )


def test_sqlite_repo_wal_and_atomic_quota_reservation(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    with closing(sqlite3.connect(db_path)) as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"

    now = "2026-11-24T10:00:00+00:00"
    stale_before = "2026-11-24T09:57:00+00:00"

    missing = repo.try_reserve_quota(
        1001, allow_repeat=False, now_iso=now, stale_before_iso=stale_before
    )
    assert missing.ok is False
    assert missing.outcome == ReservationOutcome.USER_NOT_FOUND

    repo.upsert_user_start(1001, "ali", "Ali", "tg_group_1", is_qa=False, now_iso=now)
    r1 = repo.try_reserve_quota(
        1001, allow_repeat=False, now_iso=now, stale_before_iso=stale_before
    )
    assert r1.ok is True
    assert r1.outcome == ReservationOutcome.RESERVED
    assert r1.user is not None
    assert r1.user.quota_status == QuotaStatus.RESERVED

    # Second concurrent attempt while active reservation exists must fail as ALREADY_IN_FLIGHT
    r2 = repo.try_reserve_quota(
        1001, allow_repeat=False, now_iso=now, stale_before_iso=stale_before
    )
    assert r2.ok is False
    assert r2.outcome == ReservationOutcome.ALREADY_IN_FLIGHT

    # Stale reservation beyond TTL recovers automatically
    later = "2026-11-24T10:05:00+00:00"
    later_stale_cutoff = "2026-11-24T10:02:00+00:00"
    r3 = repo.try_reserve_quota(
        1001, allow_repeat=False, now_iso=later, stale_before_iso=later_stale_cutoff
    )
    assert r3.ok is True
    assert r3.outcome == ReservationOutcome.RESERVED

    # Release restores AVAILABLE
    repo.release_quota(1001, now_iso=later)
    user_after_release = repo.get_user(1001)
    assert user_after_release is not None
    assert user_after_release.quota_status == QuotaStatus.AVAILABLE


def test_sqlite_repo_consume_admin_repeat_and_idempotent_payment(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t0 = "2026-11-24T10:00:00+00:00"
    stale = "2026-11-24T09:57:00+00:00"

    # Regular user + QA admin user
    repo.upsert_user_start(2001, "sara", "Sara", "channel_a", is_qa=False, now_iso=t0)
    repo.set_user_level(2001, Level.B1, t0)
    repo.set_user_timeline(2001, Timeline.LESS_THAN_1_MONTH, t0)
    repo.try_reserve_quota(2001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)

    eval_out = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=2400,
        candidates_tokens=500,
        thoughts_tokens=100,
        audio_input_tokens=2000,
        text_input_tokens=400,
    )
    cost = _sample_cost()
    report = _sample_report(score=68, band="قبول")
    consume_res = repo.consume_quota_and_record_eval(
        telegram_user_id=2001,
        level=Level.B1,
        timeline=Timeline.LESS_THAN_1_MONTH,
        audio_duration_sec=62.5,
        evaluator_output=eval_out,
        report=report,
        cost=cost,
        is_qa=False,
        now_iso=t0,
    )
    assert consume_res.consumed is True
    consumed_user = consume_res.user
    assert consumed_user.quota_status == QuotaStatus.CONSUMED
    assert consumed_user.step == UserStep.AWAITING_CTA
    assert consumed_user.last_score == 68
    assert consumed_user.last_band == "قبول"

    # Non-admin repeat blocked
    blocked = repo.try_reserve_quota(2001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)
    assert blocked.ok is False
    assert blocked.outcome == ReservationOutcome.ALREADY_CONSUMED

    # Payment CTA + submission + idempotent approval
    repo.mark_cta_clicked(2001, t0)
    repo.mark_payment_submitted(2001, "last4=1234", t0)
    app1 = repo.approve_payment(2001, "ZP-1001", 490_000, t0)
    assert app1.ok is True
    assert app1.outcome == ApprovePaymentOutcome.APPROVED
    assert app1.amount_toman == 490_000

    app2 = repo.approve_payment(2001, "ZP-1001", 490_000, t0)
    assert app2.ok is False
    assert app2.outcome == ApprovePaymentOutcome.ALREADY_VERIFIED

    # QA admin user can repeat after CONSUMED, excluded from real gate stats
    repo.upsert_user_start(999001, "founder", "Founder", "qa", is_qa=True, now_iso=t0)
    repo.set_user_level(999001, Level.B2, t0)
    repo.set_user_timeline(999001, Timeline.JUST_STARTING, t0)
    assert (
        repo.try_reserve_quota(999001, allow_repeat=True, now_iso=t0, stale_before_iso=stale).ok
        is True
    )
    repo.consume_quota_and_record_eval(
        999001,
        Level.B2,
        Timeline.JUST_STARTING,
        50.0,
        eval_out,
        _sample_report(75, "قبول"),
        cost,
        True,
        t0,
    )
    # Still blocks concurrent in-flight admin reservation, allows after completion
    assert (
        repo.try_reserve_quota(999001, allow_repeat=True, now_iso=t0, stale_before_iso=stale).ok
        is True
    )
    assert (
        repo.try_reserve_quota(
            999001, allow_repeat=True, now_iso=t0, stale_before_iso=stale
        ).outcome
        == ReservationOutcome.ALREADY_IN_FLIGHT
    )

    stats = repo.get_gate_stats()
    assert stats.total_users == 1
    assert stats.completed_diagnostics == 1
    assert stats.qa_diagnostics == 1
    assert stats.cta_clicks == 1
    assert stats.paid_pre_orders == 1
    assert stats.total_collected_toman == 490_000
    assert stats.avg_cost_usd == Decimal("0.003620")
    assert stats.avg_cost_toman == 988
    assert stats.by_source == {"channel_a": 1}
    assert stats.by_level == {"B1": 1}
    assert stats.by_band == {"قبول": 1}


def test_sqlite_repo_hour_36_followup_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t_old = "2026-11-22T08:00:00+00:00"
    t_recent = "2026-11-24T08:00:00+00:00"
    cutoff = "2026-11-23T00:00:00+00:00"
    eval_out = EvaluatorOutput("{}", 1000, 200, 0, 800, 200)
    cost = _sample_cost()

    # Eligible user: old eval, unpaid, not yet followed up
    repo.upsert_user_start(3001, "u1", "Nima", "direct", is_qa=False, now_iso=t_old)
    repo.set_user_level(3001, Level.B1, t_old)
    repo.set_user_timeline(3001, Timeline.ONE_TO_THREE_MONTHS, t_old)
    repo.try_reserve_quota(
        3001, allow_repeat=False, now_iso=t_old, stale_before_iso="2026-11-22T07:57:00+00:00"
    )
    repo.consume_quota_and_record_eval(
        3001,
        Level.B1,
        Timeline.ONE_TO_THREE_MONTHS,
        45.0,
        eval_out,
        _sample_report(55, "لب مرز"),
        cost,
        False,
        t_old,
    )
    # Even if user clicked CTA later, eval timestamp determines eligibility
    repo.mark_cta_clicked(3001, t_recent)

    # Ineligible user: recent eval
    repo.upsert_user_start(3002, "u2", "Mina", "direct", is_qa=False, now_iso=t_recent)
    repo.set_user_level(3002, Level.B1, t_recent)
    repo.set_user_timeline(3002, Timeline.ONE_TO_THREE_MONTHS, t_recent)
    repo.try_reserve_quota(
        3002, allow_repeat=False, now_iso=t_recent, stale_before_iso="2026-11-24T07:57:00+00:00"
    )
    repo.consume_quota_and_record_eval(
        3002,
        Level.B1,
        Timeline.ONE_TO_THREE_MONTHS,
        45.0,
        eval_out,
        _sample_report(58, "لب مرز"),
        cost,
        False,
        t_recent,
    )

    candidates = repo.list_hour_36_followup_candidates(cutoff)
    assert [u.telegram_user_id for u in candidates] == [3001]

    repo.mark_followup_36h_sent(3001, t_recent)
    assert repo.list_hour_36_followup_candidates(cutoff) == []


def test_jsonl_telemetry_sink_writes_all_prd_fields_and_redacts_secrets(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "telemetry.jsonl"
    sink = JsonlTelemetrySink(jsonl_path, extra_secrets=("AIzaSecretKey123",))

    event = TelemetryEvent(
        telegram_user_id=4001,
        event_type="eval_completed",
        level="B1",
        timeline="<1 month",
        audio_duration_sec=61.2,
        gemini_input_tokens=2400,
        gemini_output_tokens=600,
        gemini_thoughts_tokens=150,
        estimated_cost_usd=Decimal("0.003620"),
        estimated_cost_toman=988,
        score=68,
        band="قبول",
        cta_clicked=False,
        payment_submitted=False,
        payment_verified=False,
        source_tag="tg_group_1",
        is_qa=False,
        created_at="2026-11-24T10:00:00+00:00",
    )
    sink.emit(event)

    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    required_fields = {
        "telegram_user_id",
        "level",
        "timeline",
        "audio_duration_sec",
        "gemini_input_tokens",
        "gemini_output_tokens",
        "estimated_cost_usd",
        "score",
        "band",
        "cta_clicked",
        "payment_submitted",
        "payment_verified",
        "source_tag",
        "created_at",
        "event_type",
        "estimated_cost_toman",
        "gemini_thoughts_tokens",
        "is_qa",
    }
    assert required_fields.issubset(payload.keys())
    assert payload["estimated_cost_usd"] == "0.003620"
    assert payload["estimated_cost_toman"] == 988


def test_dual_telemetry_sink_and_failed_eval_cost_accounting(tmp_path: Path) -> None:
    db_path = tmp_path / "dual.db"
    jsonl_path = tmp_path / "dual.jsonl"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()
    dual = DualTelemetrySink(repo, JsonlTelemetrySink(jsonl_path))

    now = "2026-11-24T10:00:00+00:00"
    repo.upsert_user_start(5001, "u5", "Ali", "direct", is_qa=False, now_iso=now)
    cost = _sample_cost()
    eval_out = EvaluatorOutput("{broken", 2400, 600, 0, 2000, 400)
    repo.record_failed_eval(
        5001, Level.B1, Timeline.LESS_THAN_1_MONTH, 45.0, eval_out, cost, "invalid_json", False, now
    )

    dual.emit(
        TelemetryEvent(
            telegram_user_id=5001,
            event_type="eval_failed",
            level="B1",
            timeline="<1 month",
            audio_duration_sec=45.0,
            gemini_input_tokens=2400,
            gemini_output_tokens=600,
            gemini_thoughts_tokens=0,
            estimated_cost_usd=cost.estimated_cost_usd,
            estimated_cost_toman=cost.estimated_cost_toman,
            score=None,
            band=None,
            cta_clicked=False,
            payment_submitted=False,
            payment_verified=False,
            source_tag="direct",
            is_qa=False,
            created_at=now,
        )
    )

    stats = repo.get_gate_stats()
    assert stats.completed_diagnostics == 0
    assert stats.avg_cost_usd == Decimal("0.003620")
    assert stats.avg_cost_toman == 988

    with closing(sqlite3.connect(db_path)) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM telemetry_events").fetchone()[0]
        assert cnt == 1
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 1


def test_sqlite_repo_stale_worker_cannot_release_or_overwrite_newer_reservation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stale.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t1 = "2026-11-24T10:00:00+00:00"
    t2 = "2026-11-24T10:04:00+00:00"
    t3 = "2026-11-24T10:04:10+00:00"
    t4 = "2026-11-24T10:04:20+00:00"
    t5 = "2026-11-24T10:04:30+00:00"

    repo.upsert_user_start(6001, "u6", "Dara", "direct", is_qa=False, now_iso=t1)
    repo.set_user_level(6001, Level.B1, t1)
    repo.set_user_timeline(6001, Timeline.LESS_THAN_1_MONTH, t1)

    r1 = repo.try_reserve_quota(
        6001, allow_repeat=False, now_iso=t1, stale_before_iso="2026-11-24T09:57:00+00:00"
    )
    assert r1.ok is True
    assert r1.user is not None
    assert r1.user.reserved_at == t1

    # TTL expires and Worker 2 legitimately claims a new reservation at t2
    r2 = repo.try_reserve_quota(
        6001, allow_repeat=False, now_iso=t2, stale_before_iso="2026-11-24T10:01:00+00:00"
    )
    assert r2.ok is True
    assert r2.user is not None
    assert r2.user.reserved_at == t2

    # Stale Worker 1 attempts release_quota with its old reserved_at token t1 -> must be a no-op
    release_status = repo.release_quota(6001, now_iso=t3, expected_reserved_at=t1)
    assert release_status == QuotaStatus.RESERVED
    after_stale_release = repo.get_user(6001)
    assert after_stale_release is not None
    assert after_stale_release.quota_status == QuotaStatus.RESERVED
    assert after_stale_release.reserved_at == t2

    # Stale Worker 1 attempts consume_quota_and_record_eval with token t1 -> must NOT overwrite
    eval_out = EvaluatorOutput("{}", 2000, 500, 0, 1600, 400)
    cost = _sample_cost()
    stale_res = repo.consume_quota_and_record_eval(
        telegram_user_id=6001,
        level=Level.B1,
        timeline=Timeline.LESS_THAN_1_MONTH,
        audio_duration_sec=50.0,
        evaluator_output=eval_out,
        report=_sample_report(score=40, band="رد"),
        cost=cost,
        is_qa=False,
        now_iso=t3,
        expected_reserved_at=t1,
    )
    assert stale_res.consumed is False
    assert stale_res.user.quota_status == QuotaStatus.RESERVED
    assert stale_res.user.reserved_at == t2
    assert stale_res.user.last_score is None
    assert repo.get_gate_stats().completed_diagnostics == 0

    # Active Worker 2 succeeds with token t2
    active_res = repo.consume_quota_and_record_eval(
        telegram_user_id=6001,
        level=Level.B1,
        timeline=Timeline.LESS_THAN_1_MONTH,
        audio_duration_sec=55.0,
        evaluator_output=eval_out,
        report=_sample_report(score=72, band="قبول"),
        cost=cost,
        is_qa=False,
        now_iso=t4,
        expected_reserved_at=t2,
    )
    assert active_res.consumed is True
    assert active_res.user.quota_status == QuotaStatus.CONSUMED
    assert active_res.user.reserved_at is None
    assert active_res.user.last_score == 72
    assert repo.get_gate_stats().completed_diagnostics == 1

    # Super-stale Worker 1 still cannot overwrite already-CONSUMED result
    super_stale = repo.consume_quota_and_record_eval(
        telegram_user_id=6001,
        level=Level.B1,
        timeline=Timeline.LESS_THAN_1_MONTH,
        audio_duration_sec=50.0,
        evaluator_output=eval_out,
        report=_sample_report(score=30, band="رد"),
        cost=cost,
        is_qa=False,
        now_iso=t5,
        expected_reserved_at=t1,
    )
    assert super_stale.consumed is False
    assert super_stale.user.last_score == 72
    assert repo.get_gate_stats().completed_diagnostics == 1


def test_sqlite_repo_billed_failures_lock_quota_while_unbilled_preserve_retry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "failures.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t0 = "2026-11-24T10:00:00+00:00"
    stale = "2026-11-24T09:56:00+00:00"

    repo.upsert_user_start(7001, "u7", "Sina", "direct", is_qa=False, now_iso=t0)
    repo.try_reserve_quota(7001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)

    # Unbilled error does not increment failed_eval_attempts
    st0 = repo.release_quota(
        7001, now_iso=t0, billed_failure=False, max_billed_failures=2, allow_repeat=False
    )
    assert st0 == QuotaStatus.AVAILABLE
    assert repo.get_user(7001).failed_eval_attempts == 0

    # 1st billed failure -> AVAILABLE, attempts == 1
    repo.try_reserve_quota(7001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)
    st1 = repo.release_quota(
        7001, now_iso=t0, billed_failure=True, max_billed_failures=2, allow_repeat=False
    )
    assert st1 == QuotaStatus.AVAILABLE
    assert repo.get_user(7001).failed_eval_attempts == 1

    # 2nd billed failure -> CONSUMED, attempts == 2, last_band == "ناموفق"
    repo.try_reserve_quota(7001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)
    st2 = repo.release_quota(
        7001, now_iso=t0, billed_failure=True, max_billed_failures=2, allow_repeat=False
    )
    assert st2 == QuotaStatus.CONSUMED
    locked = repo.get_user(7001)
    assert locked is not None
    assert locked.quota_status == QuotaStatus.CONSUMED
    assert locked.failed_eval_attempts == 2
    assert locked.last_band == "ناموفق"
    assert locked.step == UserStep.AWAITING_CTA
    assert (
        repo.try_reserve_quota(7001, allow_repeat=False, now_iso=t0, stale_before_iso=stale).outcome
        == ReservationOutcome.ALREADY_CONSUMED
    )

    # QA admin with allow_repeat=True is not locked out after 2 billed failures
    repo.upsert_user_start(999001, "qa", "QA", "direct", is_qa=True, now_iso=t0)
    for _ in range(3):
        repo.try_reserve_quota(999001, allow_repeat=True, now_iso=t0, stale_before_iso=stale)
        assert (
            repo.release_quota(
                999001, now_iso=t0, billed_failure=True, max_billed_failures=2, allow_repeat=True
            )
            == QuotaStatus.AVAILABLE
        )


def test_sqlite_repo_invalid_attempt_excluded_from_completed_but_counted_in_cost(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "valid_stats.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t0 = "2026-11-24T10:00:00+00:00"
    stale = "2026-11-24T09:56:00+00:00"
    eval_out = EvaluatorOutput("{}", 2000, 500, 0, 1600, 400)
    cost = _sample_cost()

    # User 1 submits invalid German attempt (is_valid_german_attempt=False)
    repo.upsert_user_start(8001, "u1", "A", "direct", is_qa=False, now_iso=t0)
    repo.try_reserve_quota(8001, allow_repeat=False, now_iso=t0, stale_before_iso=stale)
    invalid_report = EvaluationReport(
        is_valid_german_attempt=False,
        transcript_summary_de="Silence.",
        total_score=10,
        band="رد",
        subscores=EvaluationSubscores(2, 3, 3, 2),
        top_3_errors=(),
        strength_fa="—",
        timeline_action_plan_fa="—",
    )
    repo.consume_quota_and_record_eval(
        8001, Level.B1, Timeline.LESS_THAN_1_MONTH, 20.0, eval_out, invalid_report, cost, False, t0
    )

    stats1 = repo.get_gate_stats()
    assert stats1.completed_diagnostics == 0
    assert stats1.avg_cost_toman == 988
    assert stats1.avg_cost_usd == Decimal("0.003620")

    # User 2 has a TIMEOUT failure + User 3 has a valid SUCCESS evaluation
    repo.upsert_user_start(8002, "u2", "B", "direct", is_qa=False, now_iso=t0)
    repo.record_failed_eval(
        8002,
        Level.B1,
        Timeline.LESS_THAN_1_MONTH,
        30.0,
        eval_out,
        cost,
        "transient_timeout",
        False,
        t0,
        status="TIMEOUT",
    )
    repo.upsert_user_start(8003, "u3", "C", "direct", is_qa=False, now_iso=t0)
    repo.try_reserve_quota(8003, allow_repeat=False, now_iso=t0, stale_before_iso=stale)
    repo.consume_quota_and_record_eval(
        8003,
        Level.B1,
        Timeline.LESS_THAN_1_MONTH,
        45.0,
        eval_out,
        _sample_report(70, "قبول"),
        cost,
        False,
        t0,
    )

    stats2 = repo.get_gate_stats()
    assert stats2.completed_diagnostics == 1
    assert stats2.avg_cost_toman == 988 * 3


def test_sqlite_repo_duplicate_tx_ref_rejected_across_users(tmp_path: Path) -> None:
    db_path = tmp_path / "dup_tx.db"
    repo = SQLiteStateRepository(db_path)
    repo.init_schema()

    t0 = "2026-11-24T10:00:00+00:00"
    repo.upsert_user_start(9001, "p1", "Ali", "direct", is_qa=False, now_iso=t0)
    repo.upsert_user_start(9002, "p2", "Reza", "direct", is_qa=False, now_iso=t0)

    r1 = repo.approve_payment(9001, "ZP-UNIQUE-1", 490_000, t0)
    assert r1.ok is True
    assert r1.outcome == ApprovePaymentOutcome.APPROVED

    r2 = repo.approve_payment(9002, "ZP-UNIQUE-1", 490_000, t0)
    assert r2.ok is False
    assert r2.outcome == ApprovePaymentOutcome.DUPLICATE_TX_REF
    u2 = repo.get_user(9002)
    assert u2 is not None
    assert u2.payment_verified is False

    stats = repo.get_gate_stats()
    assert stats.paid_pre_orders == 1
    assert stats.total_collected_toman == 490_000
