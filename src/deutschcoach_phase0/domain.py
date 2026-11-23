from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

MIN_AUDIO_DURATION_SEC: float = 15.0
MAX_AUDIO_DURATION_SEC: float = 125.0
MAX_AUDIO_SIZE_BYTES: int = 5 * 1024 * 1024

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "audio/ogg",
        "audio/opus",
        "audio/webm",
        "audio/mp4",
        "audio/mpeg",
    }
)

VALID_BANDS: frozenset[str] = frozenset({"قبول", "لب مرز", "رد"})
VALID_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {"Grammatik", "Aussprache", "Wortschatz", "Struktur"}
)


class Level(StrEnum):
    B1 = "B1"
    B2 = "B2"


class Timeline(StrEnum):
    LESS_THAN_1_MONTH = "<1 month"
    ONE_TO_THREE_MONTHS = "1-3 months"
    JUST_STARTING = "Just starting"

    @property
    def label_fa(self) -> str:
        return TIMELINE_LABELS_FA[self]


TIMELINE_LABELS_FA: dict[Timeline, str] = {
    Timeline.LESS_THAN_1_MONTH: "کمتر از ۱ ماه دیگر",
    Timeline.ONE_TO_THREE_MONTHS: "۱ تا ۳ ماه دیگر",
    Timeline.JUST_STARTING: "تازه شروع کرده‌ام",
}


class QuotaStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"


class UserStep(StrEnum):
    NEW = "NEW"
    AWAITING_LEVEL = "AWAITING_LEVEL"
    AWAITING_TIMELINE = "AWAITING_TIMELINE"
    AWAITING_VOICE = "AWAITING_VOICE"
    AWAITING_CTA = "AWAITING_CTA"
    AWAITING_PAYMENT_PROOF = "AWAITING_PAYMENT_PROOF"
    PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"


class AudioRejectionCode(StrEnum):
    TOO_SHORT = "TOO_SHORT"
    TOO_LONG = "TOO_LONG"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_MIME = "INVALID_MIME"
    CORRUPT_OR_NO_AUDIO = "CORRUPT_OR_NO_AUDIO"
    EMPTY_FILE = "EMPTY_FILE"


class ReservationOutcome(StrEnum):
    RESERVED = "RESERVED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    ALREADY_CONSUMED = "ALREADY_CONSUMED"
    ALREADY_IN_FLIGHT = "ALREADY_IN_FLIGHT"


class ApprovePaymentOutcome(StrEnum):
    APPROVED = "APPROVED"
    ALREADY_VERIFIED = "ALREADY_VERIFIED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_TX_REF = "INVALID_TX_REF"
    DUPLICATE_TX_REF = "DUPLICATE_TX_REF"


class TransientEvaluatorError(Exception):
    """Raised when the external speech evaluator fails transiently and quota may be released."""

    def __init__(self, message: str, *, potentially_billed: bool = False) -> None:
        super().__init__(message)
        self.potentially_billed = potentially_billed


MSG_AUDIO_TOO_SHORT = (
    "وُیس شما خیلی کوتاه است (زیر ۱۵ ثانیه). "
    "لطفاً حداقل ۳۰ تا ۹۰ ثانیه صحبت کنید؛ سهمیه رایگان شما مصرف نشد."
)

MSG_AUDIO_TOO_LONG = (
    "حداکثر زمان مجاز ۱۲۰ ثانیه است. لطفاً فشرده‌تر ضبط و ارسال کنید؛ سهمیه رایگان شما مصرف نشد."
)

MSG_AUDIO_TOO_LARGE = (
    "حجم فایل صوتی بیش از حد مجاز (۵ مگابایت) است. "
    "لطفاً یک وُیس کوتاه‌تر با تلگرام ضبط و ارسال کنید؛ سهمیه رایگان شما مصرف نشد."
)

MSG_AUDIO_INVALID_FORMAT = (
    "فایل ارسالی صوتی معتبر نیست یا قابل خواندن نبود. "
    "لطفاً مستقیم با میکروفون تلگرام یک وُیس ضبط و ارسال کنید؛ سهمیه رایگان شما مصرف نشد."
)

MSG_VPN_OFF_WARNING = (
    "⚠️ نکته بسیار مهم: قبل از زدن لینک پرداخت، حتماً فیلترشکن (VPN) را خاموش کنید "
    "تا درگاه شاپرک خطا ندهد."
)

MSG_UNOFFICIAL_DISCLAIMER = (
    "⚠️ توجه: دویچ‌کوچ یک شبیه‌ساز تمرینی غیررسمی مبتنی بر هوش مصنوعی است، "
    "هیچ وابستگی یا تاییدیه‌ای از مؤسسه گوته (Goethe-Institut) یا ÖSD ندارد "
    "و مدرک رسمی صادر نمی‌کند."
)

PROMPT_CARD_B1 = (
    "🇩🇪 کارت آزمون شفاهی گوته B1 — بخش دوم (ارائه موضوع)\n\n"
    "Thema: „Brauchen Jugendliche heute ein eigenes Smartphone?“\n\n"
    "در یک فایل صوتی بین ۶۰ تا ۹۰ ثانیه به زبان آلمانی، این ۵ بخش را به ترتیب پوشش دهید:\n"
    "1. Einleitung: Mein Thema ist ... (معرفی موضوع)\n"
    "2. Persönliche Erfahrungen: Meine eigenen Erfahrungen damit (تجربه شخصی شما)\n"
    "3. Situation im Heimatland: Wie ist die Situation im Iran? (وضعیت در ایران)\n"
    "4. Vor- und Nachteile: Was spricht dafür, was dagegen? (مزایا و معایب)\n"
    "5. Abschluss und Dank: Meine Meinung + Vielen Dank für Ihre Aufmerksamkeit "
    "(جمع‌بندی و تشکر)\n\n"
    "🎙️ همین الان وُیس خودتان را ضبط و ارسال کنید (۳۰ تا ۱۲۰ ثانیه)."
)

PROMPT_CARD_B2 = (
    "🇩🇪 کارت آزمون شفاهی گوته B2 — بخش اول (سخنرانی و اعلام موضع)\n\n"
    "Thema: „Gesund leben am Arbeitsplatz — wie können Berufstätige trotz Stress "
    "und Schichtarbeit gesund bleiben?“\n\n"
    "در یک فایل صوتی بین ۹۰ تا ۱۲۰ ثانیه به زبان آلمانی، ارائه ساختارمند بدهید "
    "و این موارد را پوشش دهید:\n"
    "1. Einstieg & Relevanz: Warum ist dieses Thema heute besonders wichtig?\n"
    "2. Verschiedene Möglichkeiten nennen: Welche Maßnahmen gibt es "
    "(z. B. Bewegung, Ernährung, Pausen, Arbeitszeitmodelle)?\n"
    "3. Eine Möglichkeit genauer beschreiben & Vor-/Nachteile abwägen.\n"
    "4. Eigene Stellungnahme & Fazit: Welche Lösung halten Sie für am sinnvollsten und warum?\n\n"
    "🎙️ همین الان وُیس خودتان را ضبط و ارسال کنید (۳۰ تا ۱۲۰ ثانیه)."
)

HOUR_36_FOLLOWUP_TEMPLATE = (
    "سلام [نام/دوست گرامی] 👋\n"
    "دیروز نمره بخش Sprechen شما توی دویچ‌کوچ {{SCORE}}/100 ({{BAND}}) شد. "
    "اگر تا ۳ ماه آینده آزمون گوته/ÖSD یا مصاحبه آوسبیلدونگ دارید، "
    "حیف است قبل از آزمون فقط با چند تا تمرین پراکنده ریسک کنید.\n"
    "ظرفیت تخفیف بتای بنیان‌گذار (۴۹۰ هزار تومان به‌جای ۷۹۰ هزار تومان) "
    "برای پکیج ۱۰ آزمون کامل نمره‌دهی‌شده + تمرین مصاحبه آوسبیلدونگ روی وب‌اپ بدون فیلترشکن، "
    "تا فردا شب باز است (با اعتبار ۹۰ روزه و گارانتی ۱۰۰٪ بازگشت وجه).\n"
    "برای رزرو پکیج با قیمت بتا از دکمه زیر استفاده کنید 👇"
)


@dataclass(frozen=True)
class UserRecord:
    telegram_user_id: int
    username: str | None
    first_name: str | None
    source_tag: str
    level: Level | None
    timeline: Timeline | None
    step: UserStep
    quota_status: QuotaStatus
    reserved_at: str | None
    cta_clicked: bool
    payment_submitted: bool
    payment_verified: bool
    payment_ref: str | None
    payment_amount_toman: int | None
    is_qa: bool
    last_score: int | None
    last_band: str | None
    followup_36h_sent_at: str | None
    created_at: str
    updated_at: str
    failed_eval_attempts: int = 0


@dataclass(frozen=True)
class IncomingAudioMeta:
    file_id: str
    file_unique_id: str | None
    mime_type: str | None
    file_size: int | None
    reported_duration_sec: float | None
    is_voice: bool = True


@dataclass(frozen=True)
class AudioValidationResult:
    ok: bool
    duration_sec: float | None = None
    size_bytes: int | None = None
    detected_format: str | None = None
    rejection_code: AudioRejectionCode | None = None
    user_message_fa: str | None = None
    normalized_path: Path | None = None


@dataclass(frozen=True)
class EvaluationSubscores:
    task_fulfillment: int
    coherence_interaction: int
    vocab_grammar: int
    pronunciation_fluency: int


@dataclass(frozen=True)
class EvaluationErrorItem:
    category: str
    exact_spoken_excerpt: str
    corrected_german: str
    explanation_fa: str


@dataclass(frozen=True)
class EvaluationReport:
    is_valid_german_attempt: bool
    transcript_summary_de: str
    total_score: int
    band: str
    subscores: EvaluationSubscores
    top_3_errors: tuple[EvaluationErrorItem, ...]
    strength_fa: str
    timeline_action_plan_fa: str


@dataclass(frozen=True)
class ParsedEvaluation:
    ok: bool
    report: EvaluationReport | None
    partial_fallback: bool = False
    fallback_reason: str | None = None


@dataclass(frozen=True)
class EvaluatorOutput:
    raw_text: str
    prompt_tokens: int
    candidates_tokens: int
    thoughts_tokens: int = 0
    audio_input_tokens: int | None = None
    text_input_tokens: int | None = None
    model_name: str = ""


@dataclass(frozen=True)
class CostBreakdown:
    audio_input_tokens: int
    text_input_tokens: int
    output_tokens: int
    total_input_tokens: int
    estimated_cost_usd: Decimal
    estimated_cost_toman: int


@dataclass(frozen=True)
class ReservationResult:
    ok: bool
    outcome: ReservationOutcome
    user: UserRecord | None = None


@dataclass(frozen=True)
class ConsumeEvalResult:
    user: UserRecord
    consumed: bool


@dataclass(frozen=True)
class ApprovePaymentResult:
    ok: bool
    outcome: ApprovePaymentOutcome
    user: UserRecord | None = None
    amount_toman: int = 0


@dataclass(frozen=True)
class GateStats:
    total_users: int
    completed_diagnostics: int
    cta_clicks: int
    payment_submissions: int
    paid_pre_orders: int
    total_collected_toman: int
    avg_cost_usd: Decimal
    avg_cost_toman: int
    qa_diagnostics: int
    by_source: dict[str, int]
    by_level: dict[str, int]
    by_band: dict[str, int]


@dataclass(frozen=True)
class TelemetryEvent:
    telegram_user_id: int
    event_type: str
    level: str | None
    timeline: str | None
    audio_duration_sec: float | None
    gemini_input_tokens: int | None
    gemini_output_tokens: int | None
    gemini_thoughts_tokens: int | None
    estimated_cost_usd: Decimal | None
    estimated_cost_toman: int | None
    score: int | None
    band: str | None
    cta_clicked: bool
    payment_submitted: bool
    payment_verified: bool
    source_tag: str
    is_qa: bool
    created_at: str


@dataclass(frozen=True)
class InlineButton:
    text: str
    callback_data: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class ReplyMessage:
    text: str
    parse_mode: str | None = "HTML"
    inline_keyboard: tuple[tuple[InlineButton, ...], ...] = ()


@dataclass(frozen=True)
class AdminNotification:
    text: str
    parse_mode: str | None = "HTML"
    photo_file_id: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    message: ReplyMessage


@dataclass(frozen=True)
class ServiceReply:
    messages: tuple[ReplyMessage, ...]
    callback_answer: str | None = None
    admin_notification: AdminNotification | None = None
    outbound_messages: tuple[OutboundMessage, ...] = ()


@dataclass(frozen=True)
class ServiceConfig:
    gemini_model: str
    gemini_timeout_sec: int
    admin_user_ids: frozenset[int]
    tmp_audio_dir: Path
    zarinlink_url: str
    card_to_card_number: str
    card_to_card_holder: str
    founder_support_handle: str
    usd_to_toman_rate: int
    beta_pack_price_toman: int
    regular_pack_price_toman: int
    reservation_ttl_sec: int
    max_billed_failed_attempts: int = 2
    sensitive_values: tuple[str, ...] = ()
