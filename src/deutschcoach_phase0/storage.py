from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from deutschcoach_phase0.domain import (
    ApprovePaymentOutcome,
    ApprovePaymentResult,
    ConsumeEvalResult,
    CostBreakdown,
    EvaluationReport,
    EvaluatorOutput,
    GateStats,
    Level,
    QuotaStatus,
    ReservationOutcome,
    ReservationResult,
    TelemetryEvent,
    Timeline,
    UserRecord,
    UserStep,
)
from deutschcoach_phase0.security import redact_sensitive_text

_USD_QUANTUM = Decimal("0.000001")
_TOMAN_QUANTUM = Decimal("1")


class SQLiteStateRepository:
    """SQLite state store using WAL mode and short per-operation transactions."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _immediate_tx(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")

    def init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            with self._immediate_tx(conn):
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        telegram_user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        source_tag TEXT NOT NULL DEFAULT 'direct',
                        level TEXT,
                        timeline TEXT,
                        step TEXT NOT NULL,
                        quota_status TEXT NOT NULL,
                        reserved_at TEXT,
                        cta_clicked INTEGER NOT NULL DEFAULT 0,
                        payment_submitted INTEGER NOT NULL DEFAULT 0,
                        payment_verified INTEGER NOT NULL DEFAULT 0,
                        payment_ref TEXT,
                        payment_amount_toman INTEGER,
                        is_qa INTEGER NOT NULL DEFAULT 0,
                        last_score INTEGER,
                        last_band TEXT,
                        followup_36h_sent_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        failed_eval_attempts INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evaluations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_user_id INTEGER NOT NULL,
                        level TEXT NOT NULL,
                        timeline TEXT NOT NULL,
                        audio_duration_sec REAL NOT NULL,
                        prompt_tokens INTEGER NOT NULL,
                        candidates_tokens INTEGER NOT NULL,
                        thoughts_tokens INTEGER NOT NULL,
                        audio_input_tokens INTEGER NOT NULL,
                        text_input_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        estimated_cost_usd TEXT NOT NULL,
                        estimated_cost_toman INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        failure_reason TEXT,
                        score INTEGER,
                        band TEXT,
                        subscores_json TEXT,
                        errors_json TEXT,
                        is_qa INTEGER NOT NULL DEFAULT 0,
                        is_valid_attempt INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (telegram_user_id) REFERENCES users (telegram_user_id)
                    )
                    """
                )
                _ensure_column(
                    conn,
                    "users",
                    "failed_eval_attempts",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                _ensure_column(
                    conn,
                    "evaluations",
                    "is_valid_attempt",
                    "INTEGER NOT NULL DEFAULT 1",
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_user_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        level TEXT,
                        timeline TEXT,
                        audio_duration_sec REAL,
                        gemini_input_tokens INTEGER,
                        gemini_output_tokens INTEGER,
                        gemini_thoughts_tokens INTEGER,
                        estimated_cost_usd TEXT,
                        estimated_cost_toman INTEGER,
                        score INTEGER,
                        band TEXT,
                        cta_clicked INTEGER NOT NULL DEFAULT 0,
                        payment_submitted INTEGER NOT NULL DEFAULT 0,
                        payment_verified INTEGER NOT NULL DEFAULT 0,
                        source_tag TEXT NOT NULL,
                        is_qa INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_eval_user_status
                        ON evaluations (telegram_user_id, status, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_users_qa_followup
                        ON users (is_qa, quota_status, payment_verified, followup_36h_sent_at)
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_verified_payment_ref
                        ON users (payment_ref)
                        WHERE payment_verified = 1 AND payment_ref IS NOT NULL
                    """
                )

    def upsert_user_start(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        source_tag: str,
        is_qa: bool,
        now_iso: str,
    ) -> UserRecord:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            existing = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, username, first_name, source_tag,
                        level, timeline, step, quota_status, reserved_at,
                        cta_clicked, payment_submitted, payment_verified,
                        payment_ref, payment_amount_toman, is_qa,
                        last_score, last_band, followup_36h_sent_at,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, NULL, NULL, ?, ?, NULL,
                        0, 0, 0, NULL, NULL, ?, NULL, NULL, NULL, ?, ?
                    )
                    """,
                    (
                        telegram_user_id,
                        username,
                        first_name,
                        source_tag,
                        UserStep.AWAITING_LEVEL.value,
                        QuotaStatus.AVAILABLE.value,
                        1 if is_qa else 0,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                next_step = existing["step"]
                if existing["quota_status"] == QuotaStatus.AVAILABLE.value or is_qa:
                    next_step = UserStep.AWAITING_LEVEL.value
                # Keep original non-direct attribution tag unless it was previously 'direct'.
                kept_source = (
                    source_tag
                    if existing["source_tag"] == "direct" and source_tag != "direct"
                    else existing["source_tag"]
                )
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        first_name = ?,
                        source_tag = ?,
                        is_qa = ?,
                        step = ?,
                        updated_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (
                        username,
                        first_name,
                        kept_source,
                        1 if (bool(existing["is_qa"]) or is_qa) else 0,
                        next_step,
                        now_iso,
                        telegram_user_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(_require_row(row, "upsert_user_start"))

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def set_user_level(
        self, telegram_user_id: int, level: Level, now_iso: str
    ) -> UserRecord | None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET level = ?, step = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (level.value, UserStep.AWAITING_TIMELINE.value, now_iso, telegram_user_id),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def set_user_timeline(
        self, telegram_user_id: int, timeline: Timeline, now_iso: str
    ) -> UserRecord | None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET timeline = ?, step = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (timeline.value, UserStep.AWAITING_VOICE.value, now_iso, telegram_user_id),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def try_reserve_quota(
        self,
        telegram_user_id: int,
        allow_repeat: bool,
        now_iso: str,
        stale_before_iso: str,
    ) -> ReservationResult:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            existing = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if existing is None:
                return ReservationResult(
                    ok=False,
                    outcome=ReservationOutcome.USER_NOT_FOUND,
                    user=None,
                )

            if allow_repeat:
                cursor = conn.execute(
                    """
                    UPDATE users
                    SET quota_status = ?, reserved_at = ?, updated_at = ?
                    WHERE telegram_user_id = ?
                      AND (
                        quota_status IN (?, ?)
                        OR (quota_status = ? AND (reserved_at IS NULL OR reserved_at < ?))
                      )
                    """,
                    (
                        QuotaStatus.RESERVED.value,
                        now_iso,
                        now_iso,
                        telegram_user_id,
                        QuotaStatus.AVAILABLE.value,
                        QuotaStatus.CONSUMED.value,
                        QuotaStatus.RESERVED.value,
                        stale_before_iso,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE users
                    SET quota_status = ?, reserved_at = ?, updated_at = ?
                    WHERE telegram_user_id = ?
                      AND (
                        quota_status = ?
                        OR (quota_status = ? AND (reserved_at IS NULL OR reserved_at < ?))
                      )
                    """,
                    (
                        QuotaStatus.RESERVED.value,
                        now_iso,
                        now_iso,
                        telegram_user_id,
                        QuotaStatus.AVAILABLE.value,
                        QuotaStatus.RESERVED.value,
                        stale_before_iso,
                    ),
                )

            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            user = _row_to_user(_require_row(row, "try_reserve_quota"))

            if cursor.rowcount == 1:
                return ReservationResult(
                    ok=True,
                    outcome=ReservationOutcome.RESERVED,
                    user=user,
                )

            if user.quota_status == QuotaStatus.CONSUMED:
                return ReservationResult(
                    ok=False,
                    outcome=ReservationOutcome.ALREADY_CONSUMED,
                    user=user,
                )
            return ReservationResult(
                ok=False,
                outcome=ReservationOutcome.ALREADY_IN_FLIGHT,
                user=user,
            )

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
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            row = conn.execute(
                """
                SELECT quota_status, reserved_at, failed_eval_attempts
                FROM users
                WHERE telegram_user_id = ?
                """,
                (telegram_user_id,),
            ).fetchone()
            if row is None:
                return QuotaStatus.AVAILABLE
            current_status = QuotaStatus(row["quota_status"])
            if current_status != QuotaStatus.RESERVED:
                return current_status
            if expected_reserved_at is not None and row["reserved_at"] != expected_reserved_at:
                return current_status

            attempts = int(row["failed_eval_attempts"])
            if billed_failure:
                attempts += 1
                if not allow_repeat and attempts >= max_billed_failures:
                    conn.execute(
                        """
                        UPDATE users
                        SET quota_status = ?,
                            reserved_at = NULL,
                            step = ?,
                            last_band = ?,
                            failed_eval_attempts = ?,
                            updated_at = ?
                        WHERE telegram_user_id = ?
                        """,
                        (
                            QuotaStatus.CONSUMED.value,
                            UserStep.AWAITING_CTA.value,
                            "ناموفق",
                            attempts,
                            now_iso,
                            telegram_user_id,
                        ),
                    )
                    return QuotaStatus.CONSUMED

            conn.execute(
                """
                UPDATE users
                SET quota_status = ?,
                    reserved_at = NULL,
                    failed_eval_attempts = ?,
                    updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (
                    QuotaStatus.AVAILABLE.value,
                    attempts,
                    now_iso,
                    telegram_user_id,
                ),
            )
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
        subscores_json = json.dumps(asdict(report.subscores), ensure_ascii=False)
        errors_json = json.dumps(
            [asdict(item) for item in report.top_3_errors],
            ensure_ascii=False,
        )
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            if expected_reserved_at is not None:
                cursor = conn.execute(
                    """
                    UPDATE users
                    SET quota_status = ?,
                        reserved_at = NULL,
                        step = ?,
                        last_score = ?,
                        last_band = ?,
                        updated_at = ?
                    WHERE telegram_user_id = ?
                      AND quota_status = ?
                      AND reserved_at = ?
                    """,
                    (
                        QuotaStatus.CONSUMED.value,
                        UserStep.AWAITING_CTA.value,
                        report.total_score,
                        report.band,
                        now_iso,
                        telegram_user_id,
                        QuotaStatus.RESERVED.value,
                        expected_reserved_at,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM users WHERE telegram_user_id = ?",
                    (telegram_user_id,),
                ).fetchone()
                user = _row_to_user(_require_row(row, "consume_quota_and_record_eval"))
                if cursor.rowcount != 1:
                    conn.execute(
                        """
                        INSERT INTO evaluations (
                            telegram_user_id, level, timeline, audio_duration_sec,
                            prompt_tokens, candidates_tokens, thoughts_tokens,
                            audio_input_tokens, text_input_tokens, output_tokens,
                            estimated_cost_usd, estimated_cost_toman, status, failure_reason,
                            score, band, subscores_json, errors_json, is_qa,
                            is_valid_attempt, created_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'STALE_SUPERSEDED', 'stale_reservation_superseded',
                            NULL, NULL, NULL, NULL, ?, 0, ?
                        )
                        """,
                        (
                            telegram_user_id,
                            level.value,
                            timeline.value,
                            audio_duration_sec,
                            evaluator_output.prompt_tokens,
                            evaluator_output.candidates_tokens,
                            evaluator_output.thoughts_tokens,
                            cost.audio_input_tokens,
                            cost.text_input_tokens,
                            cost.output_tokens,
                            str(cost.estimated_cost_usd),
                            cost.estimated_cost_toman,
                            1 if is_qa else 0,
                            now_iso,
                        ),
                    )
                    return ConsumeEvalResult(user=user, consumed=False)
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET quota_status = ?,
                        reserved_at = NULL,
                        step = ?,
                        last_score = ?,
                        last_band = ?,
                        updated_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (
                        QuotaStatus.CONSUMED.value,
                        UserStep.AWAITING_CTA.value,
                        report.total_score,
                        report.band,
                        now_iso,
                        telegram_user_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM users WHERE telegram_user_id = ?",
                    (telegram_user_id,),
                ).fetchone()
                user = _row_to_user(_require_row(row, "consume_quota_and_record_eval"))

            conn.execute(
                """
                INSERT INTO evaluations (
                    telegram_user_id, level, timeline, audio_duration_sec,
                    prompt_tokens, candidates_tokens, thoughts_tokens,
                    audio_input_tokens, text_input_tokens, output_tokens,
                    estimated_cost_usd, estimated_cost_toman, status, failure_reason,
                    score, band, subscores_json, errors_json, is_qa,
                    is_valid_attempt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCESS', NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    level.value,
                    timeline.value,
                    audio_duration_sec,
                    evaluator_output.prompt_tokens,
                    evaluator_output.candidates_tokens,
                    evaluator_output.thoughts_tokens,
                    cost.audio_input_tokens,
                    cost.text_input_tokens,
                    cost.output_tokens,
                    str(cost.estimated_cost_usd),
                    cost.estimated_cost_toman,
                    report.total_score,
                    report.band,
                    subscores_json,
                    errors_json,
                    1 if is_qa else 0,
                    1 if report.is_valid_german_attempt else 0,
                    now_iso,
                ),
            )
            return ConsumeEvalResult(user=user, consumed=True)

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
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                INSERT INTO evaluations (
                    telegram_user_id, level, timeline, audio_duration_sec,
                    prompt_tokens, candidates_tokens, thoughts_tokens,
                    audio_input_tokens, text_input_tokens, output_tokens,
                    estimated_cost_usd, estimated_cost_toman, status, failure_reason,
                    score, band, subscores_json, errors_json, is_qa,
                    is_valid_attempt, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, NULL, NULL, NULL, NULL, ?, 0, ?
                )
                """,
                (
                    telegram_user_id,
                    level.value,
                    timeline.value,
                    audio_duration_sec,
                    evaluator_output.prompt_tokens,
                    evaluator_output.candidates_tokens,
                    evaluator_output.thoughts_tokens,
                    cost.audio_input_tokens,
                    cost.text_input_tokens,
                    cost.output_tokens,
                    str(cost.estimated_cost_usd),
                    cost.estimated_cost_toman,
                    status,
                    reason,
                    1 if is_qa else 0,
                    now_iso,
                ),
            )

    def mark_cta_clicked(self, telegram_user_id: int, now_iso: str) -> UserRecord | None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET cta_clicked = 1, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (now_iso, telegram_user_id),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def begin_payment_proof(self, telegram_user_id: int, now_iso: str) -> UserRecord | None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET step = CASE WHEN payment_verified = 1 THEN step ELSE ? END,
                    updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (UserStep.AWAITING_PAYMENT_PROOF.value, now_iso, telegram_user_id),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def mark_payment_submitted(
        self, telegram_user_id: int, payment_ref: str, now_iso: str
    ) -> UserRecord | None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET payment_submitted = 1,
                    payment_ref = ?,
                    step = CASE WHEN payment_verified = 1 THEN step ELSE ? END,
                    updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (
                    payment_ref,
                    UserStep.PAYMENT_SUBMITTED.value,
                    now_iso,
                    telegram_user_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            return _row_to_user(row) if row is not None else None

    def approve_payment(
        self,
        telegram_user_id: int,
        tx_ref: str,
        amount_toman: int,
        now_iso: str,
    ) -> ApprovePaymentResult:
        cleaned_ref = tx_ref.strip()
        if not cleaned_ref:
            return ApprovePaymentResult(
                ok=False,
                outcome=ApprovePaymentOutcome.INVALID_TX_REF,
                user=None,
                amount_toman=0,
            )

        with closing(self._connect()) as conn, self._immediate_tx(conn):
            existing = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if existing is None:
                return ApprovePaymentResult(
                    ok=False,
                    outcome=ApprovePaymentOutcome.USER_NOT_FOUND,
                    user=None,
                    amount_toman=0,
                )

            existing_user = _row_to_user(existing)
            if existing_user.payment_verified:
                return ApprovePaymentResult(
                    ok=False,
                    outcome=ApprovePaymentOutcome.ALREADY_VERIFIED,
                    user=existing_user,
                    amount_toman=existing_user.payment_amount_toman or 0,
                )

            duplicate_row = conn.execute(
                """
                SELECT 1
                FROM users
                WHERE payment_verified = 1
                  AND payment_ref = ?
                  AND telegram_user_id != ?
                LIMIT 1
                """,
                (cleaned_ref, telegram_user_id),
            ).fetchone()
            if duplicate_row is not None:
                return ApprovePaymentResult(
                    ok=False,
                    outcome=ApprovePaymentOutcome.DUPLICATE_TX_REF,
                    user=existing_user,
                    amount_toman=0,
                )

            try:
                cursor = conn.execute(
                    """
                    UPDATE users
                    SET payment_submitted = 1,
                        payment_verified = 1,
                        payment_ref = ?,
                        payment_amount_toman = ?,
                        step = ?,
                        updated_at = ?
                    WHERE telegram_user_id = ? AND payment_verified = 0
                    """,
                    (
                        cleaned_ref,
                        amount_toman,
                        UserStep.PAYMENT_VERIFIED.value,
                        now_iso,
                        telegram_user_id,
                    ),
                )
            except sqlite3.IntegrityError:
                return ApprovePaymentResult(
                    ok=False,
                    outcome=ApprovePaymentOutcome.DUPLICATE_TX_REF,
                    user=existing_user,
                    amount_toman=0,
                )

            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            user = _row_to_user(_require_row(row, "approve_payment"))

            if cursor.rowcount == 1:
                return ApprovePaymentResult(
                    ok=True,
                    outcome=ApprovePaymentOutcome.APPROVED,
                    user=user,
                    amount_toman=amount_toman,
                )
            return ApprovePaymentResult(
                ok=False,
                outcome=ApprovePaymentOutcome.ALREADY_VERIFIED,
                user=user,
                amount_toman=user.payment_amount_toman or 0,
            )

    def record_telemetry(self, event: TelemetryEvent) -> None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                INSERT INTO telemetry_events (
                    telegram_user_id, event_type, level, timeline, audio_duration_sec,
                    gemini_input_tokens, gemini_output_tokens, gemini_thoughts_tokens,
                    estimated_cost_usd, estimated_cost_toman, score, band,
                    cta_clicked, payment_submitted, payment_verified,
                    source_tag, is_qa, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.telegram_user_id,
                    event.event_type,
                    event.level,
                    event.timeline,
                    event.audio_duration_sec,
                    event.gemini_input_tokens,
                    event.gemini_output_tokens,
                    event.gemini_thoughts_tokens,
                    str(event.estimated_cost_usd) if event.estimated_cost_usd is not None else None,
                    event.estimated_cost_toman,
                    event.score,
                    event.band,
                    1 if event.cta_clicked else 0,
                    1 if event.payment_submitted else 0,
                    1 if event.payment_verified else 0,
                    event.source_tag,
                    1 if event.is_qa else 0,
                    event.created_at,
                ),
            )

    def get_gate_stats(self) -> GateStats:
        with closing(self._connect()) as conn:
            agg = _require_row(
                conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_users,
                        COALESCE(
                            SUM(CASE WHEN cta_clicked = 1 THEN 1 ELSE 0 END), 0
                        ) AS cta_clicks,
                        COALESCE(
                            SUM(CASE WHEN payment_submitted = 1 THEN 1 ELSE 0 END), 0
                        ) AS payment_submissions,
                        COALESCE(
                            SUM(CASE WHEN payment_verified = 1 THEN 1 ELSE 0 END), 0
                        ) AS paid_pre_orders,
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN payment_verified = 1
                                    THEN COALESCE(payment_amount_toman, 0)
                                    ELSE 0
                                END
                            ),
                            0
                        ) AS total_collected_toman
                    FROM users
                    WHERE is_qa = 0
                    """
                ).fetchone(),
                "get_gate_stats",
            )

            completed_diagnostics = int(
                conn.execute(
                    "SELECT COUNT(*) FROM evaluations "
                    "WHERE is_qa = 0 AND status = 'SUCCESS' AND is_valid_attempt = 1"
                ).fetchone()[0]
            )
            qa_diagnostics = int(
                conn.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE is_qa = 1 AND status = 'SUCCESS'"
                ).fetchone()[0]
            )

            eval_costs = conn.execute(
                """
                SELECT estimated_cost_usd, estimated_cost_toman
                FROM evaluations
                WHERE is_qa = 0
                """
            ).fetchall()
            total_usd = Decimal("0")
            total_toman = 0
            for row in eval_costs:
                total_usd += Decimal(row["estimated_cost_usd"])
                total_toman += int(row["estimated_cost_toman"])

            denom = completed_diagnostics if completed_diagnostics > 0 else len(eval_costs)
            if denom > 0:
                avg_cost_usd = (total_usd / Decimal(denom)).quantize(
                    _USD_QUANTUM, rounding=ROUND_HALF_UP
                )
                avg_cost_toman = int(
                    (Decimal(total_toman) / Decimal(denom)).quantize(
                        _TOMAN_QUANTUM, rounding=ROUND_HALF_UP
                    )
                )
            else:
                avg_cost_usd = Decimal("0.000000")
                avg_cost_toman = 0

            by_source = {
                str(r["source_tag"]): int(r["cnt"])
                for r in conn.execute(
                    "SELECT source_tag, COUNT(*) AS cnt "
                    "FROM users WHERE is_qa = 0 GROUP BY source_tag"
                ).fetchall()
            }
            by_level = {
                str(r["level"]): int(r["cnt"])
                for r in conn.execute(
                    "SELECT level, COUNT(*) AS cnt "
                    "FROM evaluations "
                    "WHERE is_qa = 0 AND status = 'SUCCESS' AND is_valid_attempt = 1 "
                    "GROUP BY level"
                ).fetchall()
            }
            by_band = {
                str(r["band"]): int(r["cnt"])
                for r in conn.execute(
                    "SELECT band, COUNT(*) AS cnt "
                    "FROM evaluations "
                    "WHERE is_qa = 0 AND status = 'SUCCESS' AND is_valid_attempt = 1 "
                    "GROUP BY band"
                ).fetchall()
            }

            return GateStats(
                total_users=int(agg["total_users"]),
                completed_diagnostics=completed_diagnostics,
                cta_clicks=int(agg["cta_clicks"]),
                payment_submissions=int(agg["payment_submissions"]),
                paid_pre_orders=int(agg["paid_pre_orders"]),
                total_collected_toman=int(agg["total_collected_toman"]),
                avg_cost_usd=avg_cost_usd,
                avg_cost_toman=avg_cost_toman,
                qa_diagnostics=qa_diagnostics,
                by_source=by_source,
                by_level=by_level,
                by_band=by_band,
            )

    def list_hour_36_followup_candidates(self, eligible_before_iso: str) -> list[UserRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT u.*
                FROM users u
                WHERE u.is_qa = 0
                  AND u.quota_status = ?
                  AND u.payment_verified = 0
                  AND u.followup_36h_sent_at IS NULL
                  AND u.last_score IS NOT NULL
                  AND u.last_band IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM evaluations e
                      WHERE e.telegram_user_id = u.telegram_user_id
                        AND e.status = 'SUCCESS'
                        AND e.created_at <= ?
                  )
                ORDER BY u.telegram_user_id ASC
                """,
                (QuotaStatus.CONSUMED.value, eligible_before_iso),
            ).fetchall()
            return [_row_to_user(row) for row in rows]

    def mark_followup_36h_sent(self, telegram_user_id: int, now_iso: str) -> None:
        with closing(self._connect()) as conn, self._immediate_tx(conn):
            conn.execute(
                """
                UPDATE users
                SET followup_36h_sent_at = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (now_iso, now_iso, telegram_user_id),
            )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _require_row(row: sqlite3.Row | None, context: str) -> sqlite3.Row:
    if row is None:
        raise RuntimeError(f"Expected SQLite row in {context}")
    return row


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        telegram_user_id=int(row["telegram_user_id"]),
        username=row["username"],
        first_name=row["first_name"],
        source_tag=str(row["source_tag"]),
        level=Level(row["level"]) if row["level"] is not None else None,
        timeline=Timeline(row["timeline"]) if row["timeline"] is not None else None,
        step=UserStep(row["step"]),
        quota_status=QuotaStatus(row["quota_status"]),
        reserved_at=row["reserved_at"],
        cta_clicked=bool(row["cta_clicked"]),
        payment_submitted=bool(row["payment_submitted"]),
        payment_verified=bool(row["payment_verified"]),
        payment_ref=row["payment_ref"],
        payment_amount_toman=(
            int(row["payment_amount_toman"]) if row["payment_amount_toman"] is not None else None
        ),
        is_qa=bool(row["is_qa"]),
        last_score=int(row["last_score"]) if row["last_score"] is not None else None,
        last_band=row["last_band"],
        followup_36h_sent_at=row["followup_36h_sent_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        failed_eval_attempts=int(row["failed_eval_attempts"]),
    )


class JsonlTelemetrySink:
    """Thread-safe append-only JSONL telemetry writer with secret redaction."""

    def __init__(self, path: Path, extra_secrets: Iterable[str] = ()) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._extra_secrets = tuple(s for s in extra_secrets if s)

    def _clean(self, value: str | None) -> str | None:
        if value is None:
            return None
        return redact_sensitive_text(value, self._extra_secrets)

    def emit(self, event: TelemetryEvent) -> None:
        payload = {
            "telegram_user_id": event.telegram_user_id,
            "event_type": self._clean(event.event_type),
            "level": self._clean(event.level),
            "timeline": self._clean(event.timeline),
            "audio_duration_sec": event.audio_duration_sec,
            "gemini_input_tokens": event.gemini_input_tokens,
            "gemini_output_tokens": event.gemini_output_tokens,
            "gemini_thoughts_tokens": event.gemini_thoughts_tokens,
            "estimated_cost_usd": (
                str(event.estimated_cost_usd) if event.estimated_cost_usd is not None else None
            ),
            "estimated_cost_toman": event.estimated_cost_toman,
            "score": event.score,
            "band": self._clean(event.band),
            "cta_clicked": event.cta_clicked,
            "payment_submitted": event.payment_submitted,
            "payment_verified": event.payment_verified,
            "source_tag": self._clean(event.source_tag),
            "is_qa": event.is_qa,
            "created_at": event.created_at,
        }
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class DualTelemetrySink:
    """Persists telemetry events to both SQLite telemetry_events and JSONL."""

    def __init__(self, repo: SQLiteStateRepository, jsonl_sink: JsonlTelemetrySink) -> None:
        self._repo = repo
        self._jsonl_sink = jsonl_sink

    def emit(self, event: TelemetryEvent) -> None:
        self._repo.record_telemetry(event)
        self._jsonl_sink.emit(event)
