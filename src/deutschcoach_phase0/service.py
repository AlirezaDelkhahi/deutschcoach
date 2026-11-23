from __future__ import annotations

import html
import logging
import math
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from deutschcoach_phase0.cost import calculate_gemini_cost
from deutschcoach_phase0.domain import (
    MAX_AUDIO_DURATION_SEC,
    MSG_AUDIO_INVALID_FORMAT,
    MSG_UNOFFICIAL_DISCLAIMER,
    AdminNotification,
    ApprovePaymentOutcome,
    CostBreakdown,
    EvaluatorOutput,
    IncomingAudioMeta,
    InlineButton,
    Level,
    OutboundMessage,
    QuotaStatus,
    ReplyMessage,
    ReservationOutcome,
    ServiceConfig,
    ServiceReply,
    TelemetryEvent,
    Timeline,
    TransientEvaluatorError,
    UserRecord,
    UserStep,
)
from deutschcoach_phase0.interfaces import (
    AudioCleaner,
    AudioFetcher,
    AudioInspector,
    Clock,
    SpeechEvaluator,
    StateRepository,
    TelemetrySink,
)
from deutschcoach_phase0.prompt_builder import (
    SYSTEM_PROMPT,
    build_user_prompt,
    format_beta_offer_html,
    format_payment_instructions_html,
    format_scorecard_html,
    format_toman,
    get_prompt_card,
    parse_evaluation_json,
    render_hour_36_followup,
)
from deutschcoach_phase0.security import redact_sensitive_text, sanitize_source_tag

logger = logging.getLogger(__name__)
_MAX_ALLOWED_AUDIO_INPUT_TOKENS = int(MAX_AUDIO_DURATION_SEC * 32 * 1.15)


class DiagnosticService:
    """Pure application orchestration depending strictly on interfaces and domain types."""

    def __init__(
        self,
        config: ServiceConfig,
        repo: StateRepository,
        telemetry: TelemetrySink,
        audio_inspector: AudioInspector,
        audio_fetcher: AudioFetcher,
        audio_cleaner: AudioCleaner,
        evaluator: SpeechEvaluator,
        clock: Clock,
    ) -> None:
        self._config = config
        self._repo = repo
        self._telemetry = telemetry
        self._audio_inspector = audio_inspector
        self._audio_fetcher = audio_fetcher
        self._audio_cleaner = audio_cleaner
        self._evaluator = evaluator
        self._clock = clock

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    def _is_qa(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self._config.admin_user_ids

    async def handle_start(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        start_arg: str | None,
    ) -> ServiceReply:
        source_tag = sanitize_source_tag(start_arg)
        is_qa = self._is_qa(telegram_user_id)
        now_iso = self._now_iso()

        user = self._repo.upsert_user_start(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            source_tag=source_tag,
            is_qa=is_qa,
            now_iso=now_iso,
        )
        self._emit_user_event("start", user, now_iso)

        if user.quota_status == QuotaStatus.CONSUMED and not is_qa:
            return self._already_used_reply(user)

        greeting_name = (
            html.escape(first_name.strip()) if first_name and first_name.strip() else "دوست گرامی"
        )
        text = (
            f"سلام {greeting_name}! 👋\n"
            "به <b>دویچ‌کوچ (DeutschCoach)</b> خوش آمدید.\n\n"
            "اینجا می‌توانید <b>۱ ارزیابی رایگان و واقعی آزمون شفاهی گوته (Sprechen)</b> "
            "را انجام دهید، نمره از ۱۰۰ بگیرید و ۳ اشتباه دقیق گرامری و تلفظی خودتان را "
            "با اصلاحیه آلمانی و توضیح فارسی دریافت کنید.\n\n"
            f"<i>{html.escape(MSG_UNOFFICIAL_DISCLAIMER)}</i>\n\n"
            "برای شروع، سطح آزمون خودتان را انتخاب کنید:"
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=text,
                    parse_mode="HTML",
                    inline_keyboard=_level_keyboard(),
                ),
            )
        )

    async def handle_level_selected(
        self,
        telegram_user_id: int,
        raw_level: str,
        username: str | None = None,
        first_name: str | None = None,
    ) -> ServiceReply:
        try:
            level = Level(raw_level)
        except ValueError:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="سطح انتخابی معتبر نیست. لطفاً دوباره انتخاب کنید.", parse_mode=None
                    ),
                ),
                callback_answer="سطح نامعتبر",
            )

        now_iso = self._now_iso()
        is_qa = self._is_qa(telegram_user_id)
        user = self._repo.get_user(telegram_user_id)
        if user is None:
            user = self._repo.upsert_user_start(
                telegram_user_id, username, first_name, "direct", is_qa, now_iso
            )

        if user.quota_status == QuotaStatus.CONSUMED and not is_qa:
            reply = self._already_used_reply(user)
            return ServiceReply(
                messages=reply.messages,
                callback_answer="سهمیه رایگان شما قبلاً استفاده شده است.",
            )

        updated = self._repo.set_user_level(telegram_user_id, level, now_iso)
        if updated is not None:
            self._emit_user_event("level_selected", updated, now_iso)

        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=(
                        f"عالی! سطح هدف شما روی <b>{html.escape(level.value)}</b> تنظیم شد.\n"
                        "چند وقت دیگر آزمون گوته/ÖSD یا مصاحبه آوسبیلدونگ دارید؟"
                    ),
                    parse_mode="HTML",
                    inline_keyboard=_timeline_keyboard(),
                ),
            ),
            callback_answer=f"سطح {level.value} انتخاب شد",
        )

    async def handle_timeline_selected(
        self,
        telegram_user_id: int,
        raw_timeline: str,
        username: str | None = None,
        first_name: str | None = None,
    ) -> ServiceReply:
        try:
            timeline = Timeline(raw_timeline)
        except ValueError:
            return ServiceReply(
                messages=(ReplyMessage(text="بازه زمانی انتخابی معتبر نیست.", parse_mode=None),),
                callback_answer="گزینه نامعتبر",
            )

        now_iso = self._now_iso()
        is_qa = self._is_qa(telegram_user_id)
        user = self._repo.get_user(telegram_user_id)
        if user is None:
            user = self._repo.upsert_user_start(
                telegram_user_id, username, first_name, "direct", is_qa, now_iso
            )

        if user.quota_status == QuotaStatus.CONSUMED and not is_qa:
            reply = self._already_used_reply(user)
            return ServiceReply(
                messages=reply.messages,
                callback_answer="سهمیه رایگان شما قبلاً استفاده شده است.",
            )

        if user.level is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="لطفاً ابتدا سطح آزمون (B1 یا B2) را انتخاب کنید:",
                        parse_mode=None,
                        inline_keyboard=_level_keyboard(),
                    ),
                ),
                callback_answer="ابتدا سطح را انتخاب کنید",
            )

        updated = self._repo.set_user_timeline(telegram_user_id, timeline, now_iso)
        if updated is not None:
            self._emit_user_event("timeline_selected", updated, now_iso)

        card_text = get_prompt_card(user.level)
        return ServiceReply(
            messages=(ReplyMessage(text=card_text, parse_mode=None),),
            callback_answer=timeline.label_fa,
        )

    async def handle_voice(
        self,
        telegram_user_id: int,
        meta: IncomingAudioMeta,
        username: str | None = None,
        first_name: str | None = None,
    ) -> ServiceReply:
        is_qa = self._is_qa(telegram_user_id)
        user = self._repo.get_user(telegram_user_id)
        if user is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="برای شروع ارزیابی رایگان، لطفاً ابتدا دستور /start را ارسال کنید.",
                        parse_mode=None,
                    ),
                )
            )

        if user.quota_status == QuotaStatus.CONSUMED and not is_qa:
            return self._already_used_reply(user)

        if user.level is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="ابتدا سطح آزمون خودتان (B1 یا B2) را انتخاب کنید:",
                        parse_mode=None,
                        inline_keyboard=_level_keyboard(),
                    ),
                )
            )

        if user.timeline is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="ابتدا زمان باقی‌مانده تا آزمون را انتخاب کنید:",
                        parse_mode=None,
                        inline_keyboard=_timeline_keyboard(),
                    ),
                )
            )

        pre_check = self._audio_inspector.validate_pre_download(meta)
        if not pre_check.ok:
            self._emit_user_event(
                "audio_rejected_pre_download",
                user,
                self._now_iso(),
                audio_duration_sec=pre_check.duration_sec,
            )
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text=pre_check.user_message_fa or MSG_AUDIO_INVALID_FORMAT,
                        parse_mode=None,
                    ),
                )
            )

        now_dt = self._clock.now()
        now_iso = now_dt.isoformat()
        stale_before_iso = (
            now_dt - timedelta(seconds=self._config.reservation_ttl_sec)
        ).isoformat()

        reservation = self._repo.try_reserve_quota(
            telegram_user_id=telegram_user_id,
            allow_repeat=is_qa,
            now_iso=now_iso,
            stale_before_iso=stale_before_iso,
        )
        if not reservation.ok:
            if reservation.outcome == ReservationOutcome.ALREADY_CONSUMED:
                return self._already_used_reply(reservation.user or user)
            if reservation.outcome == ReservationOutcome.ALREADY_IN_FLIGHT:
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=(
                                "⏳ ارزیابی وُیس قبلی شما هنوز در حال پردازش است. "
                                "لطفاً چند ثانیه صبر کنید."
                            ),
                            parse_mode=None,
                        ),
                    )
                )
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="برای شروع لطفاً دستور /start را ارسال کنید.",
                        parse_mode=None,
                    ),
                )
            )

        reserved_at = reservation.user.reserved_at if reservation.user is not None else None
        temp_path = (
            self._config.tmp_audio_dir
            / f"voice_{telegram_user_id}_{uuid4().hex}{_ext_for_mime(meta.mime_type)}"
        )
        normalized_path: Path | None = None
        quota_finalized = False
        try:
            try:
                await self._audio_fetcher.download_to_path(meta.file_id, temp_path)
            except Exception:
                self._repo.release_quota(
                    telegram_user_id,
                    self._now_iso(),
                    expected_reserved_at=reserved_at,
                )
                quota_finalized = True
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=(
                                "در دریافت فایل صوتی خطای موقت رخ داد؛ سهمیه رایگان شما مصرف نشد. "
                                "لطفاً دوباره ارسال کنید."
                            ),
                            parse_mode=None,
                        ),
                    )
                )

            inspection = await self._audio_inspector.inspect_file(temp_path, meta.mime_type)
            normalized_path = inspection.normalized_path
            if not inspection.ok:
                self._repo.release_quota(
                    telegram_user_id,
                    self._now_iso(),
                    expected_reserved_at=reserved_at,
                )
                quota_finalized = True
                self._emit_user_event(
                    "audio_rejected_ffprobe",
                    user,
                    self._now_iso(),
                    audio_duration_sec=inspection.duration_sec,
                )
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=inspection.user_message_fa or MSG_AUDIO_INVALID_FORMAT,
                            parse_mode=None,
                        ),
                    )
                )

            duration_sec = inspection.duration_sec or 0.0
            if normalized_path is not None:
                audio_bytes = normalized_path.read_bytes()
                effective_mime = "audio/ogg"
            else:
                audio_bytes = temp_path.read_bytes()
                effective_mime = _canonical_mime(meta.mime_type, inspection.detected_format)

            try:
                eval_output = await self._evaluator.evaluate_audio(
                    audio_bytes=audio_bytes,
                    mime_type=effective_mime,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=build_user_prompt(user.level, user.timeline),
                    timeout_sec=self._config.gemini_timeout_sec,
                )
            except TransientEvaluatorError as exc:
                err_now = self._now_iso()
                billed_cost: CostBreakdown | None = None
                if exc.potentially_billed:
                    est_audio_tokens = math.ceil(max(0.0, duration_sec) * 32)
                    est_text_tokens = 400
                    est_output = EvaluatorOutput(
                        raw_text="",
                        prompt_tokens=est_audio_tokens + est_text_tokens,
                        candidates_tokens=0,
                        thoughts_tokens=0,
                        audio_input_tokens=est_audio_tokens,
                        text_input_tokens=est_text_tokens,
                        model_name=self._config.gemini_model,
                    )
                    billed_cost = calculate_gemini_cost(
                        output=est_output,
                        audio_duration_sec=duration_sec,
                        usd_to_toman_rate=self._config.usd_to_toman_rate,
                    )
                    self._repo.record_failed_eval(
                        telegram_user_id=telegram_user_id,
                        level=user.level,
                        timeline=user.timeline,
                        audio_duration_sec=duration_sec,
                        evaluator_output=est_output,
                        cost=billed_cost,
                        reason="transient_timeout_or_billed_error",
                        is_qa=is_qa,
                        now_iso=err_now,
                        status="TIMEOUT",
                    )
                release_status = self._repo.release_quota(
                    telegram_user_id,
                    err_now,
                    expected_reserved_at=reserved_at,
                    billed_failure=exc.potentially_billed,
                    max_billed_failures=self._config.max_billed_failed_attempts,
                    allow_repeat=is_qa,
                )
                quota_finalized = True
                self._emit_user_event(
                    "eval_transient_error",
                    user,
                    err_now,
                    audio_duration_sec=duration_sec,
                    gemini_input_tokens=(
                        billed_cost.total_input_tokens if billed_cost is not None else None
                    ),
                    gemini_output_tokens=0 if billed_cost is not None else None,
                    cost=billed_cost,
                )
                if release_status == QuotaStatus.CONSUMED:
                    return self._retry_limit_reached_reply()
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=(
                                "در ارتباط با سرویس ارزیابی خطای موقت رخ داد؛ "
                                "سهمیه رایگان شما مصرف نشد. لطفاً چند لحظه دیگر دوباره ارسال کنید."
                            ),
                            parse_mode=None,
                        ),
                    )
                )

            cost = calculate_gemini_cost(
                output=eval_output,
                audio_duration_sec=duration_sec,
                usd_to_toman_rate=self._config.usd_to_toman_rate,
            )
            audio_token_anomaly = cost.audio_input_tokens > _MAX_ALLOWED_AUDIO_INPUT_TOKENS
            if audio_token_anomaly:
                logger.warning(
                    "Audio input token anomaly for user %s: audio_tokens=%s",
                    telegram_user_id,
                    cost.audio_input_tokens,
                )

            parsed = parse_evaluation_json(eval_output.raw_text)
            if not parsed.ok or parsed.report is None:
                reason = parsed.fallback_reason or "unparseable_json"
                fail_now = self._now_iso()
                self._repo.record_failed_eval(
                    telegram_user_id=telegram_user_id,
                    level=user.level,
                    timeline=user.timeline,
                    audio_duration_sec=duration_sec,
                    evaluator_output=eval_output,
                    cost=cost,
                    reason=reason,
                    is_qa=is_qa,
                    now_iso=fail_now,
                )
                release_status = self._repo.release_quota(
                    telegram_user_id,
                    fail_now,
                    expected_reserved_at=reserved_at,
                    billed_failure=True,
                    max_billed_failures=(
                        1 if audio_token_anomaly else self._config.max_billed_failed_attempts
                    ),
                    allow_repeat=False if audio_token_anomaly else is_qa,
                )
                quota_finalized = True
                self._emit_user_event(
                    "eval_failed",
                    user,
                    fail_now,
                    audio_duration_sec=duration_sec,
                    gemini_input_tokens=cost.total_input_tokens,
                    gemini_output_tokens=eval_output.candidates_tokens,
                    gemini_thoughts_tokens=eval_output.thoughts_tokens,
                    cost=cost,
                )
                if release_status == QuotaStatus.CONSUMED:
                    return self._retry_limit_reached_reply()
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=(
                                "پاسخ ارزیابی به شکل کامل دریافت نشد؛ سهمیه رایگان شما مصرف نشد. "
                                "لطفاً یک بار دیگر وُیس خود را ارسال کنید."
                            ),
                            parse_mode=None,
                        ),
                    )
                )

            done_now = self._now_iso()
            consume_res = self._repo.consume_quota_and_record_eval(
                telegram_user_id=telegram_user_id,
                level=user.level,
                timeline=user.timeline,
                audio_duration_sec=duration_sec,
                evaluator_output=eval_output,
                report=parsed.report,
                cost=cost,
                is_qa=is_qa,
                now_iso=done_now,
                expected_reserved_at=reserved_at,
            )
            quota_finalized = True
            if not consume_res.consumed:
                return ServiceReply(
                    messages=(
                        ReplyMessage(
                            text=(
                                "⏳ درخواست ارزیابی جدیدتری برای شما ثبت یا پردازش شده است. "
                                "لطفاً نتیجه همان درخواست را بررسی کنید یا دوباره تلاش کنید."
                            ),
                            parse_mode=None,
                        ),
                    )
                )

            self._emit_user_event(
                "eval_completed",
                consume_res.user,
                done_now,
                audio_duration_sec=duration_sec,
                gemini_input_tokens=cost.total_input_tokens,
                gemini_output_tokens=eval_output.candidates_tokens,
                gemini_thoughts_tokens=eval_output.thoughts_tokens,
                cost=cost,
                score=parsed.report.total_score,
                band=parsed.report.band,
            )

            scorecard_html = format_scorecard_html(parsed.report, user.level)
            offer_html = format_beta_offer_html(
                beta_price_toman=self._config.beta_pack_price_toman,
                regular_price_toman=self._config.regular_pack_price_toman,
            )
            return ServiceReply(
                messages=(
                    ReplyMessage(text=scorecard_html, parse_mode="HTML"),
                    ReplyMessage(
                        text=offer_html,
                        parse_mode="HTML",
                        inline_keyboard=_cta_keyboard(self._config.beta_pack_price_toman),
                    ),
                )
            )
        finally:
            if not quota_finalized:
                self._repo.release_quota(
                    telegram_user_id,
                    self._now_iso(),
                    expected_reserved_at=reserved_at,
                )
            if normalized_path is not None:
                self._audio_cleaner.delete_now(normalized_path)
            self._audio_cleaner.delete_now(temp_path)

    async def handle_cta_click(self, telegram_user_id: int) -> ServiceReply:
        now_iso = self._now_iso()
        user = self._repo.mark_cta_clicked(telegram_user_id, now_iso)
        if user is not None:
            self._emit_user_event("cta_clicked", user, now_iso)

        instructions = format_payment_instructions_html(
            beta_price_toman=self._config.beta_pack_price_toman,
            zarinlink_url=self._config.zarinlink_url,
            card_to_card_number=self._config.card_to_card_number,
            card_to_card_holder=self._config.card_to_card_holder,
            founder_support_handle=self._config.founder_support_handle,
        )
        rows: list[tuple[InlineButton, ...]] = []
        if self._config.zarinlink_url.strip():
            rows.append(
                (
                    InlineButton(
                        text="🌐 پرداخت آنلاین زرین‌لینک (بدون VPN)",
                        url=self._config.zarinlink_url.strip(),
                    ),
                )
            )
        rows.append(
            (
                InlineButton(
                    text="🧾 ارسال رسید / ۴ رقم آخر کارت",
                    callback_data="payment:submit",
                ),
            )
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=instructions,
                    parse_mode="HTML",
                    inline_keyboard=tuple(rows),
                ),
            ),
            callback_answer="جزئیات پرداخت بتای بنیان‌گذار",
        )

    async def handle_start_payment_proof(self, telegram_user_id: int) -> ServiceReply:
        now_iso = self._now_iso()
        self._repo.begin_payment_proof(telegram_user_id, now_iso)
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=(
                        "لطفاً تصویر رسید پرداخت، کد پیگیری زرین‌لینک، "
                        "یا فقط <b>۴ رقم آخر کارت + ساعت واریز</b> را همین‌جا ارسال کنید.\n"
                        "⚠️ لطفاً شماره کارت ۱۶ رقمی کامل ارسال نکنید."
                    ),
                    parse_mode="HTML",
                ),
            ),
            callback_answer="منتظر ارسال رسید شما هستیم",
        )

    async def handle_payment_proof(
        self,
        telegram_user_id: int,
        proof_text: str | None,
        has_photo: bool = False,
        photo_file_id: str | None = None,
    ) -> ServiceReply:
        now_iso = self._now_iso()
        has_photo = has_photo or bool(photo_file_id)
        cleaned_raw = redact_sensitive_text(
            (proof_text or "").strip(),
            self._config.sensitive_values,
        )
        ref_summary = cleaned_raw[:120] or ("photo_receipt" if has_photo else "submitted")

        user = self._repo.mark_payment_submitted(telegram_user_id, ref_summary, now_iso)
        if user is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="لطفاً ابتدا دستور /start را ارسال کنید.",
                        parse_mode=None,
                    ),
                )
            )

        self._emit_user_event("payment_submitted", user, now_iso)

        safe_username = f"@{html.escape(user.username)}" if user.username else "بدون یوزرنیم"
        admin_text = (
            "💳 <b>رسید پرداخت جدید ثبت شد</b>\n"
            f"• کاربر: <code>{telegram_user_id}</code> ({safe_username})\n"
            f"• سطح: {html.escape(user.level.value if user.level else '-')}\n"
            f"• منبع: {html.escape(user.source_tag)}\n"
            f"• متن/مرجع: <code>{html.escape(ref_summary)}</code>\n\n"
            f"برای تأیید:\n<code>/approve {telegram_user_id} TX_REF</code>"
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=(
                        "✅ رسید شما ثبت شد و برای بررسی بنیان‌گذار ارسال گردید.\n"
                        "به‌محض تأیید، پیام فعال‌سازی پکیج بتای بنیان‌گذار برای شما ارسال می‌شود."
                    ),
                    parse_mode=None,
                ),
            ),
            admin_notification=AdminNotification(
                text=admin_text,
                parse_mode="HTML",
                photo_file_id=photo_file_id,
            ),
        )

    async def handle_text_message(
        self,
        telegram_user_id: int,
        text: str | None,
        has_photo: bool = False,
        photo_file_id: str | None = None,
    ) -> ServiceReply:
        user = self._repo.get_user(telegram_user_id)
        if user is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="برای شروع، لطفاً دستور /start را ارسال کنید.",
                        parse_mode=None,
                    ),
                )
            )

        if user.step == UserStep.AWAITING_PAYMENT_PROOF or (
            user.cta_clicked
            and user.step in (UserStep.AWAITING_CTA, UserStep.PAYMENT_SUBMITTED)
            and not user.payment_verified
        ):
            return await self.handle_payment_proof(
                telegram_user_id=telegram_user_id,
                proof_text=text,
                has_photo=has_photo,
                photo_file_id=photo_file_id,
            )

        if user.quota_status == QuotaStatus.CONSUMED and not self._is_qa(telegram_user_id):
            return self._already_used_reply(user)

        if user.level is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="لطفاً از دکمه‌های زیر سطح آزمون خود را انتخاب کنید:",
                        parse_mode=None,
                        inline_keyboard=_level_keyboard(),
                    ),
                )
            )
        if user.timeline is None:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="لطفاً زمان آزمون خود را از دکمه‌های زیر انتخاب کنید:",
                        parse_mode=None,
                        inline_keyboard=_timeline_keyboard(),
                    ),
                )
            )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=(
                        "🎙️ لطفاً پاسخ خودتان را به‌صورت <b>وُیس تلگرام (۳۰ تا ۱۲۰ ثانیه)</b> "
                        "ضبط و ارسال کنید."
                    ),
                    parse_mode="HTML",
                ),
            )
        )

    async def handle_admin_approve(
        self,
        admin_user_id: int,
        target_user_id: int,
        tx_ref: str,
    ) -> ServiceReply:
        if admin_user_id not in self._config.admin_user_ids:
            return ServiceReply(
                messages=(ReplyMessage(text="⛔ دسترسی مجاز نیست.", parse_mode=None),)
            )

        now_iso = self._now_iso()
        res = self._repo.approve_payment(
            telegram_user_id=target_user_id,
            tx_ref=tx_ref,
            amount_toman=self._config.beta_pack_price_toman,
            now_iso=now_iso,
        )
        if res.outcome == ApprovePaymentOutcome.USER_NOT_FOUND:
            return ServiceReply(
                messages=(ReplyMessage(text="کاربر مورد نظر یافت نشد.", parse_mode=None),)
            )
        if res.outcome == ApprovePaymentOutcome.INVALID_TX_REF:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="فرمت صحیح: /approve <telegram_user_id> <tx_ref>",
                        parse_mode=None,
                    ),
                )
            )
        if res.outcome == ApprovePaymentOutcome.ALREADY_VERIFIED:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text=f"ℹ️ این پرداخت قبلاً تأیید شده است (کاربر {target_user_id}).",
                        parse_mode=None,
                    ),
                )
            )
        if res.outcome == ApprovePaymentOutcome.DUPLICATE_TX_REF:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text=(
                            f"⚠️ کد پیگیری <code>{html.escape(tx_ref.strip())}</code> "
                            "قبلاً برای کاربر دیگری ثبت و تأیید شده است."
                        ),
                        parse_mode="HTML",
                    ),
                )
            )

        if res.user is None:
            raise RuntimeError("Approved payment result missing user record")
        self._emit_user_event("payment_verified", res.user, now_iso)

        buyer_msg = ReplyMessage(
            text=(
                "🎉 <b>پرداخت شما تأیید شد!</b>\n"
                f"رزرو <b>پکیج بتای بنیان‌گذار دویچ‌کوچ</b> "
                f"({format_toman(res.amount_toman)} تومان — "
                "۱۰ آزمون کامل + تمرین مصاحبه آوسبیلدونگ) "
                f"با کد پیگیری <code>{html.escape(tx_ref.strip())}</code> ثبت شد.\n"
                "لینک دسترسی وب‌اپ بدون فیلترشکن به‌محض آماده شدن برای شما ارسال می‌شود."
            ),
            parse_mode="HTML",
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=(
                        f"✅ پرداخت کاربر <code>{target_user_id}</code> "
                        f"به مبلغ <b>{format_toman(res.amount_toman)} تومان</b> "
                        f"(مرجع: <code>{html.escape(tx_ref.strip())}</code>) ثبت شد."
                    ),
                    parse_mode="HTML",
                ),
            ),
            outbound_messages=(OutboundMessage(chat_id=target_user_id, message=buyer_msg),),
        )

    async def handle_admin_stats(self, admin_user_id: int) -> ServiceReply:
        if admin_user_id not in self._config.admin_user_ids:
            return ServiceReply(
                messages=(ReplyMessage(text="⛔ دسترسی مجاز نیست.", parse_mode=None),)
            )

        stats = self._repo.get_gate_stats()
        source_summary = ", ".join(f"{k}:{v}" for k, v in sorted(stats.by_source.items())) or "-"
        level_summary = ", ".join(f"{k}:{v}" for k, v in sorted(stats.by_level.items())) or "-"
        band_summary = ", ".join(f"{k}:{v}" for k, v in sorted(stats.by_band.items())) or "-"

        text = (
            "📈 <b>آمار Phase 0 (بدون کاربران QA)</b>\n"
            f"• کل کاربران: <b>{stats.total_users}</b>\n"
            f"• ارزیابی‌های تکمیل‌شده: <b>{stats.completed_diagnostics}</b> / 100\n"
            f"• کلیک CTA پرداخت: <b>{stats.cta_clicks}</b>\n"
            f"• رسیدهای ارسالی: <b>{stats.payment_submissions}</b>\n"
            f"• پیش‌خریدهای تأییدشده: <b>{stats.paid_pre_orders}</b> / 10\n"
            f"• مجموع دریافتی: <b>{format_toman(stats.total_collected_toman)} تومان</b> "
            "(آستانه: 4,900,000)\n"
            f"• میانگین هزینه API هر ارزیابی: <b>{format_toman(stats.avg_cost_toman)} تومان</b> "
            f"(<code>${stats.avg_cost_usd}</code> — سقف: 3,000 تومان)\n"
            f"• ارزیابی‌های QA ادمین: {stats.qa_diagnostics}\n\n"
            f"• منابع: <code>{html.escape(source_summary)}</code>\n"
            f"• سطوح: <code>{html.escape(level_summary)}</code>\n"
            f"• نتایج: <code>{html.escape(band_summary)}</code>"
        )
        return ServiceReply(messages=(ReplyMessage(text=text, parse_mode="HTML"),))

    async def handle_admin_followup(
        self, admin_user_id: int, dry_run: bool = False
    ) -> ServiceReply:
        if admin_user_id not in self._config.admin_user_ids:
            return ServiceReply(
                messages=(ReplyMessage(text="⛔ دسترسی مجاز نیست.", parse_mode=None),)
            )

        cutoff_iso = (self._clock.now() - timedelta(hours=36)).isoformat()
        candidates = self._repo.list_hour_36_followup_candidates(cutoff_iso)
        if not candidates:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text="هیچ کاربر واجد شرایطی برای پیام پیگیری ۳۶ ساعته یافت نشد.",
                        parse_mode=None,
                    ),
                )
            )

        if dry_run:
            return ServiceReply(
                messages=(
                    ReplyMessage(
                        text=f"پیش‌نمایش: {len(candidates)} کاربر واجد شرایط پیگیری ۳۶ ساعته هستند.",
                        parse_mode=None,
                    ),
                )
            )

        outbound: list[OutboundMessage] = []
        for user in candidates:
            if user.last_score is None or user.last_band is None:
                continue
            body = render_hour_36_followup(
                score=user.last_score,
                band=user.last_band,
                first_name=user.first_name,
            )
            outbound.append(
                OutboundMessage(
                    chat_id=user.telegram_user_id,
                    message=ReplyMessage(
                        text=body,
                        parse_mode=None,
                        inline_keyboard=_cta_keyboard(self._config.beta_pack_price_toman),
                    ),
                )
            )

        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=f"در حال ارسال پیام پیگیری ۳۶ ساعته برای {len(outbound)} کاربر...",
                    parse_mode=None,
                ),
            ),
            outbound_messages=tuple(outbound),
        )

    async def mark_followup_sent(self, telegram_user_id: int) -> None:
        now_iso = self._now_iso()
        self._repo.mark_followup_36h_sent(telegram_user_id, now_iso)
        user = self._repo.get_user(telegram_user_id)
        if user is not None:
            self._emit_user_event("followup_36h_sent", user, now_iso)

    def _already_used_reply(self, user: UserRecord) -> ServiceReply:
        score_line = (
            f"\nنمره ثبت‌شده قبلی شما: "
            f"<b>{user.last_score}/100 ({html.escape(user.last_band)})</b>\n"
            if user.last_score is not None and user.last_band is not None
            else "\n"
        )
        text = (
            "شما قبلاً از <b>۱ ارزیابی رایگان</b> خود استفاده کرده‌اید."
            f"{score_line}\n"
            + format_beta_offer_html(
                beta_price_toman=self._config.beta_pack_price_toman,
                regular_price_toman=self._config.regular_pack_price_toman,
            )
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=text,
                    parse_mode="HTML",
                    inline_keyboard=_cta_keyboard(self._config.beta_pack_price_toman),
                ),
            )
        )

    def _retry_limit_reached_reply(self) -> ServiceReply:
        support_line = (
            f"\nدر صورت نیاز به راهنمایی به {html.escape(self._config.founder_support_handle)} "
            "پیام دهید.\n"
            if self._config.founder_support_handle.strip()
            else "\n"
        )
        text = (
            "⚠️ سقف تلاش مجاز برای ارزیابی رایگان شما به پایان رسید و سهمیه این مرحله بسته شد."
            f"{support_line}\n"
            + format_beta_offer_html(
                beta_price_toman=self._config.beta_pack_price_toman,
                regular_price_toman=self._config.regular_pack_price_toman,
            )
        )
        return ServiceReply(
            messages=(
                ReplyMessage(
                    text=text,
                    parse_mode="HTML",
                    inline_keyboard=_cta_keyboard(self._config.beta_pack_price_toman),
                ),
            )
        )

    def _emit_user_event(
        self,
        event_type: str,
        user: UserRecord,
        created_at: str,
        *,
        audio_duration_sec: float | None = None,
        gemini_input_tokens: int | None = None,
        gemini_output_tokens: int | None = None,
        gemini_thoughts_tokens: int | None = None,
        cost: CostBreakdown | None = None,
        score: int | None = None,
        band: str | None = None,
    ) -> None:
        try:
            self._telemetry.emit(
                TelemetryEvent(
                    telegram_user_id=user.telegram_user_id,
                    event_type=event_type,
                    level=user.level.value if user.level is not None else None,
                    timeline=user.timeline.value if user.timeline is not None else None,
                    audio_duration_sec=audio_duration_sec,
                    gemini_input_tokens=gemini_input_tokens,
                    gemini_output_tokens=gemini_output_tokens,
                    gemini_thoughts_tokens=gemini_thoughts_tokens,
                    estimated_cost_usd=cost.estimated_cost_usd if cost is not None else None,
                    estimated_cost_toman=cost.estimated_cost_toman if cost is not None else None,
                    score=score if score is not None else user.last_score,
                    band=band if band is not None else user.last_band,
                    cta_clicked=user.cta_clicked,
                    payment_submitted=user.payment_submitted,
                    payment_verified=user.payment_verified,
                    source_tag=user.source_tag,
                    is_qa=user.is_qa,
                    created_at=created_at,
                )
            )
        except Exception as exc:
            logger.warning(
                "Telemetry emit failed for event %s user %s: %s",
                event_type,
                user.telegram_user_id,
                redact_sensitive_text(str(exc), self._config.sensitive_values),
            )


def _level_keyboard() -> tuple[tuple[InlineButton, ...], ...]:
    return (
        (
            InlineButton("سطح B1 (گوته / ÖSD)", callback_data=f"level:{Level.B1.value}"),
            InlineButton("سطح B2 (گوته / ÖSD)", callback_data=f"level:{Level.B2.value}"),
        ),
    )


def _timeline_keyboard() -> tuple[tuple[InlineButton, ...], ...]:
    return (
        (
            InlineButton(
                Timeline.LESS_THAN_1_MONTH.label_fa,
                callback_data=f"timeline:{Timeline.LESS_THAN_1_MONTH.value}",
            ),
        ),
        (
            InlineButton(
                Timeline.ONE_TO_THREE_MONTHS.label_fa,
                callback_data=f"timeline:{Timeline.ONE_TO_THREE_MONTHS.value}",
            ),
        ),
        (
            InlineButton(
                Timeline.JUST_STARTING.label_fa,
                callback_data=f"timeline:{Timeline.JUST_STARTING.value}",
            ),
        ),
    )


def _cta_keyboard(beta_price_toman: int) -> tuple[tuple[InlineButton, ...], ...]:
    return (
        (
            InlineButton(
                text=f"🔥 رزرو پکیج بتا ({format_toman(beta_price_toman)} تومان)",
                callback_data="cta:beta",
            ),
        ),
    )


def _ext_for_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ".ogg"
    base = mime_type.split(";", 1)[0].strip().lower()
    mapping = {
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/webm": ".webm",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
    }
    return mapping.get(base, ".ogg")


def _canonical_mime(claimed_mime: str | None, detected_format: str | None) -> str:
    if detected_format:
        tokens = {tok.strip().lower() for tok in detected_format.split(",")}
        if "ogg" in tokens or "opus" in tokens:
            return "audio/ogg"
        if "webm" in tokens:
            return "audio/webm"
        if "mp4" in tokens or "m4a" in tokens:
            return "audio/mp4"
        if "mp3" in tokens:
            return "audio/mpeg"
    if claimed_mime:
        base = claimed_mime.split(";", 1)[0].strip().lower()
        if base == "audio/opus":
            return "audio/ogg"
        return base
    return "audio/ogg"
