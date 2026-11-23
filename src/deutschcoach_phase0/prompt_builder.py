from __future__ import annotations

import html
import json
import math
import re
from typing import TypeGuard

from deutschcoach_phase0.domain import (
    HOUR_36_FOLLOWUP_TEMPLATE,
    MSG_UNOFFICIAL_DISCLAIMER,
    MSG_VPN_OFF_WARNING,
    PROMPT_CARD_B1,
    PROMPT_CARD_B2,
    VALID_ERROR_CATEGORIES,
    EvaluationErrorItem,
    EvaluationReport,
    EvaluationSubscores,
    Level,
    ParsedEvaluation,
    Timeline,
)

TELEGRAM_MAX_MESSAGE_LEN = 4096

SYSTEM_PROMPT = (
    "You are an experienced, strict, yet constructive Goethe-Institut certified B1/B2 "
    'oral examiner ("Prüfer") who coaches Iranian Persian-speaking candidates.\n'
    "You are evaluating an unscripted audio recording from a candidate practicing for "
    "Goethe Sprechen.\n\n"
    "CRITICAL ANTI-HALLUCINATION & STRICTNESS RULES:\n"
    "1. Listen directly to the attached audio. NEVER invent mistakes the speaker did not say.\n"
    '2. Every item in "top_3_errors" MUST include an "exact_spoken_excerpt" quoted verbatim '
    "from what the speaker actually said in the audio (including "
    "grammatical/pronunciation/lexical mistakes). If the speaker spoke less than 20 seconds "
    "or gave off-topic/non-German audio, score appropriately (< 35) and explain honestly "
    "in Persian.\n"
    "3. Score strictly according to authentic Goethe-Zertifikat CEFR B1 or B2 standards:\n"
    '   - Passing score is 60/100 ("قبول"). 50–59 is borderline ("لب مرز"). '
    '< 50 is failing ("نیاز به تمرین فوری / رد").\n'
    "   - Evaluate 4 dimensions out of 25 points each (sum = total_score 0..100):\n"
    "     a) task_fulfillment (Aufgabenerfüllung / پوشش ساختار و نکات کارت)\n"
    "     b) coherence_interaction (Kohärenz & Redemittel / انسجام، جملات ربط و ساختار ارائه)\n"
    "     c) vocab_grammar (Wortschatz & Strukturen / دایره واژگان و دقت گرامری سطح هدف)\n"
    "     d) pronunciation_fluency (Aussprache & Flüssigkeit / تلفظ، آهنگ کلام و روانی)\n"
    "4. Write all feedback, explanations, and coaching tips in crisp, natural, encouraging yet "
    "honest Persian (Farsi), keeping German examples/corrections in German script.\n"
    "5. Output ONLY valid JSON matching the exact schema below."
)

USER_PROMPT_TEMPLATE = (
    "Evaluate this candidate recording.\n"
    "- Target Level: {{LEVEL}}   (B1 or B2)\n"
    "- Exam Timeline: {{TIMELINE}}\n"
    "- Prompt Card Given: {{PROMPT_TITLE_AND_BULLETS}}\n\n"
    "Return JSON with this exact structure:\n"
    "{\n"
    '  "is_valid_german_attempt": true,\n'
    '  "transcript_summary_de": "Brief 2-3 sentence faithful summary/excerpt of what '
    'the user actually said in German",\n'
    '  "total_score": 0,\n'
    '  "band": "قبول | لب مرز | رد",\n'
    '  "subscores": {\n'
    '    "task_fulfillment": 0,\n'
    '    "coherence_interaction": 0,\n'
    '    "vocab_grammar": 0,\n'
    '    "pronunciation_fluency": 0\n'
    "  },\n"
    '  "top_3_errors": [\n'
    "    {\n"
    '      "category": "Grammatik | Aussprache | Wortschatz | Struktur",\n'
    '      "exact_spoken_excerpt": "exact wrong phrase heard in audio",\n'
    '      "corrected_german": "natural corrected B1/B2 German sentence",\n'
    '      "explanation_fa": "clear 1-2 sentence explanation in Persian of why it lowers '
    'the Goethe score"\n'
    "    }\n"
    "  ],\n"
    '  "strength_fa": "1 sentence highlighting what the candidate already did well",\n'
    '  "timeline_action_plan_fa": "1-2 concrete sentences tailored to their timeline '
    '({{TIMELINE}}) telling them what to fix before exam day"\n'
    "}"
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_SUBSCORE_KEYS = (
    "task_fulfillment",
    "coherence_interaction",
    "vocab_grammar",
    "pronunciation_fluency",
)


def get_prompt_card(level: Level) -> str:
    return PROMPT_CARD_B1 if level == Level.B1 else PROMPT_CARD_B2


def build_user_prompt(level: Level, timeline: Timeline) -> str:
    timeline_str = f"{timeline.label_fa} ({timeline.value})"
    return (
        USER_PROMPT_TEMPLATE.replace("{{LEVEL}}", level.value)
        .replace("{{TIMELINE}}", timeline_str)
        .replace("{{PROMPT_TITLE_AND_BULLETS}}", get_prompt_card(level))
    )


def _strip_markdown_fences(raw_text: str) -> str:
    stripped = raw_text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp_int(value: object, low: int, high: int) -> tuple[int, bool]:
    if not _is_finite_number(value):
        return low, True
    if value > high:
        return high, True
    if value < low:
        return low, True
    numeric = int(round(value))
    clamped = max(low, min(high, numeric))
    return clamped, clamped != value


def _derive_band(total_score: int) -> str:
    if total_score >= 60:
        return "قبول"
    if total_score >= 50:
        return "لب مرز"
    return "رد"


def _clean_text(value: object, fallback: str) -> tuple[str, bool]:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned, False
    return fallback, True


def parse_evaluation_json(raw_text: str) -> ParsedEvaluation:
    """Safely parse and normalize Gemini JSON output without raising on malformed data."""
    if not raw_text or not raw_text.strip():
        return ParsedEvaluation(ok=False, report=None, fallback_reason="empty_response")

    candidate = _strip_markdown_fences(raw_text)
    try:
        data = json.loads(candidate, parse_constant=lambda _const: None)
    except json.JSONDecodeError as exc:
        return ParsedEvaluation(
            ok=False,
            report=None,
            fallback_reason=f"invalid_json:{exc.msg}",
        )

    if not isinstance(data, dict):
        return ParsedEvaluation(
            ok=False,
            report=None,
            fallback_reason="json_root_not_object",
        )

    raw_subscores = data.get("subscores")
    has_valid_subscore = isinstance(raw_subscores, dict) and any(
        _is_finite_number(raw_subscores.get(key)) for key in _SUBSCORE_KEYS
    )
    has_valid_total = _is_finite_number(data.get("total_score"))
    if not has_valid_total and not has_valid_subscore:
        return ParsedEvaluation(
            ok=False,
            report=None,
            fallback_reason="missing_core_evaluation_fields",
        )

    partial = False

    is_valid_raw = data.get("is_valid_german_attempt")
    if isinstance(is_valid_raw, bool):
        is_valid_attempt = is_valid_raw
    else:
        is_valid_attempt = True
        partial = True

    if not isinstance(raw_subscores, dict):
        raw_subscores = {}
        partial = True

    task_f, c1 = _clamp_int(raw_subscores.get("task_fulfillment"), 0, 25)
    coh_i, c2 = _clamp_int(raw_subscores.get("coherence_interaction"), 0, 25)
    voc_g, c3 = _clamp_int(raw_subscores.get("vocab_grammar"), 0, 25)
    pro_f, c4 = _clamp_int(raw_subscores.get("pronunciation_fluency"), 0, 25)
    if c1 or c2 or c3 or c4:
        partial = True

    subscores = EvaluationSubscores(
        task_fulfillment=task_f,
        coherence_interaction=coh_i,
        vocab_grammar=voc_g,
        pronunciation_fluency=pro_f,
    )

    raw_total = data.get("total_score")
    if _is_finite_number(raw_total):
        total_score, clamped_total = _clamp_int(raw_total, 0, 100)
        if clamped_total:
            partial = True
    else:
        total_score = task_f + coh_i + voc_g + pro_f
        partial = True

    derived_band = _derive_band(total_score)
    raw_band = data.get("band")
    if isinstance(raw_band, str) and raw_band.strip() == derived_band:
        band = derived_band
    else:
        band = derived_band
        partial = True

    summary_de, s_fb = _clean_text(
        data.get("transcript_summary_de"),
        "خلاصه گفتار ثبت نشد.",
    )
    strength_fa, st_fb = _clean_text(
        data.get("strength_fa"),
        "تلاش شما برای پاسخ‌گویی به موضوع آزمون ثبت شد.",
    )
    plan_fa, pl_fb = _clean_text(
        data.get("timeline_action_plan_fa"),
        "برای آمادگی بهتر، روی ساختاردهی پاسخ و دقت گرامری طبق زمان‌بندی آزمون تمرین کنید.",
    )
    if s_fb or st_fb or pl_fb:
        partial = True

    raw_errors = data.get("top_3_errors")
    errors: list[EvaluationErrorItem] = []
    if isinstance(raw_errors, list):
        for item in raw_errors:
            if not isinstance(item, dict):
                partial = True
                continue
            if len(errors) >= 3:
                partial = True
                break
            cat_raw = item.get("category")
            category = (
                cat_raw.strip()
                if isinstance(cat_raw, str) and cat_raw.strip() in VALID_ERROR_CATEGORIES
                else "Grammatik"
            )
            if category != cat_raw:
                partial = True
            excerpt, e1 = _clean_text(item.get("exact_spoken_excerpt"), "—")
            corrected, e2 = _clean_text(item.get("corrected_german"), "—")
            expl, e3 = _clean_text(item.get("explanation_fa"), "نیاز به بازبینی ساختار جمله.")
            if e1 or e2 or e3:
                partial = True
            errors.append(
                EvaluationErrorItem(
                    category=category,
                    exact_spoken_excerpt=excerpt,
                    corrected_german=corrected,
                    explanation_fa=expl,
                )
            )
    else:
        partial = True

    report = EvaluationReport(
        is_valid_german_attempt=is_valid_attempt,
        transcript_summary_de=summary_de,
        total_score=total_score,
        band=band,
        subscores=subscores,
        top_3_errors=tuple(errors),
        strength_fa=strength_fa,
        timeline_action_plan_fa=plan_fa,
    )
    return ParsedEvaluation(ok=True, report=report, partial_fallback=partial)


def _cap_and_escape(text: str, max_chars: int, max_escaped_chars: int | None = None) -> str:
    if max_chars <= 0 or (max_escaped_chars is not None and max_escaped_chars <= 0):
        return ""
    clipped = text.strip()
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 1].rstrip() + "…"
    escaped = html.escape(clipped, quote=False)
    if max_escaped_chars is not None and len(escaped) > max_escaped_chars:
        # Shrink raw text until entity-expanded HTML fits without mid-tag truncation.
        while clipped and len(escaped) > max_escaped_chars:
            clipped = clipped[:-1].rstrip()
            candidate = (clipped + "…") if clipped else "…"
            escaped = html.escape(candidate, quote=False)
        if len(escaped) > max_escaped_chars:
            return ""
    return escaped


def _scaled_budget(max_chars: int, max_escaped_chars: int, scale: float) -> tuple[int, int]:
    if scale <= 0:
        return 0, 0
    return (
        max(1, int(max_chars * scale)),
        max(1, int(max_escaped_chars * scale)),
    )


def _render_scorecard_html(report: EvaluationReport, level: Level, scale: float) -> str:
    band_cap = _scaled_budget(32, 48, scale)
    summary_cap = _scaled_budget(240, 360, scale)
    cat_cap = _scaled_budget(32, 48, scale)
    excerpt_cap = _scaled_budget(130, 190, scale)
    corrected_cap = _scaled_budget(130, 190, scale)
    expl_cap = _scaled_budget(170, 250, scale)
    strength_cap = _scaled_budget(200, 300, scale)
    plan_cap = _scaled_budget(220, 340, scale)

    lines: list[str] = [
        f"📊 <b>کارنامه ارزیابی شبیه‌ساز گوته {html.escape(level.value)}</b>",
        (
            f"🎯 <b>نمره کل:</b> {report.total_score}/100 — "
            f"<b>{_cap_and_escape(report.band, *band_cap)}</b>"
        ),
        "",
        "<b>ریزنمرات معیارهای آزمون (هر بخش از ۲۵):</b>",
        f"• پوشش ساختار و نکات کارت: {report.subscores.task_fulfillment}/25",
        f"• انسجام و جملات ربط: {report.subscores.coherence_interaction}/25",
        f"• واژگان و دقت گرامری: {report.subscores.vocab_grammar}/25",
        f"• تلفظ و روانی کلام: {report.subscores.pronunciation_fluency}/25",
        "",
        (
            "📝 <b>خلاصه گفتار شما:</b>\n"
            f"<i>{_cap_and_escape(report.transcript_summary_de, *summary_cap)}</i>"
        ),
    ]

    if report.top_3_errors:
        lines.append("")
        lines.append("🔍 <b>۳ نکته کلیدی برای اصلاح فوری:</b>")
        for idx, err in enumerate(report.top_3_errors[:3], start=1):
            cat = _cap_and_escape(err.category, *cat_cap)
            excerpt = _cap_and_escape(err.exact_spoken_excerpt, *excerpt_cap)
            corrected = _cap_and_escape(err.corrected_german, *corrected_cap)
            expl = _cap_and_escape(err.explanation_fa, *expl_cap)
            lines.append(
                f"{idx}. <b>[{cat}]</b>\n"
                f"   ❌ گفته شما: <code>{excerpt}</code>\n"
                f"   ✅ شکل صحیح: <code>{corrected}</code>\n"
                f"   💡 {expl}"
            )

    lines.extend(
        [
            "",
            f"💪 <b>نقطه قوت شما:</b> {_cap_and_escape(report.strength_fa, *strength_cap)}",
            (
                "🗓️ <b>برنامه اقدام تا روز آزمون:</b> "
                f"{_cap_and_escape(report.timeline_action_plan_fa, *plan_cap)}"
            ),
            "",
            f"<i>{html.escape(MSG_UNOFFICIAL_DISCLAIMER)}</i>",
        ]
    )

    return "\n".join(lines)


def format_scorecard_html(report: EvaluationReport, level: Level) -> str:
    """Format the evaluation report as valid Telegram HTML strictly <= 4096 chars."""
    scale = 1.0
    rendered = _render_scorecard_html(report, level, scale)
    while len(rendered) > TELEGRAM_MAX_MESSAGE_LEN and scale > 0.05:
        scale *= 0.8
        rendered = _render_scorecard_html(report, level, scale)
    if len(rendered) > TELEGRAM_MAX_MESSAGE_LEN:
        rendered = _render_scorecard_html(report, level, 0.0)
    return rendered


def format_toman(amount: int) -> str:
    return f"{amount:,}"


def format_beta_offer_html(beta_price_toman: int, regular_price_toman: int) -> str:
    """Format the Founder-Beta Pre-Order offer message immediately following the scorecard."""
    return (
        "🚀 <b>پیشنهاد ویژه بتای بنیان‌گذار دویچ‌کوچ</b>\n\n"
        f"💎 <b>پکیج ۱۰ آزمون کامل نمره‌دهی‌شده + تمرین مصاحبه آوسبیلدونگ:</b>\n"
        f"• قیمت بتای بنیان‌گذار: <b>{format_toman(beta_price_toman)} تومان</b> "
        f"(به‌جای قیمت عادی <s>{format_toman(regular_price_toman)} تومان</s>)\n"
        "• شامل <b>10</b> شبیه‌سازی کامل B1/B2 (بخش‌های ۱ تا ۳ گوته / بخش‌های ۱ تا ۲) "
        "+ تمرین مصاحبه کاری <b>Ausbildung</b>\n"
        "• روی وب‌اپ اختصاصی و بدون نیاز به فیلترشکن (راه‌اندازی ظرف ≤ ۱۴ روز آینده)\n"
        "• اعتبار بسته: <b>90 روز</b>\n"
        "• 🛡️ <b>گارانتی ۱۰۰٪ بازگشت وجه</b> در صورت تأخیر بیش از ۱۴ روز در راه‌اندازی "
        "یا نارضایتی بعد از اولین آزمون کامل.\n\n"
        "اگر می‌خواهید سهمیه خودتان را با قیمت بتا رزرو کنید، دکمه زیر را بزنید 👇"
    )


def format_payment_instructions_html(
    beta_price_toman: int,
    zarinlink_url: str,
    card_to_card_number: str,
    card_to_card_holder: str,
    founder_support_handle: str,
) -> str:
    """Format Phase 0 payment instructions with the verbatim Shaparak VPN-off warning."""
    parts: list[str] = [
        f"💳 <b>رزرو پکیج بتای بنیان‌گذار ({format_toman(beta_price_toman)} تومان)</b>",
        "",
        f"<b>{html.escape(MSG_VPN_OFF_WARNING)}</b>",
    ]

    if zarinlink_url.strip():
        safe_url = html.escape(zarinlink_url.strip())
        parts.extend(
            [
                "",
                "🔹 <b>روش اول (زرین‌لینک — پرداخت آنلاین شاپرک):</b>",
                f"لینک پرداخت: {safe_url}",
                "پس از پرداخت موفق، شماره پیگیری یا رسید را همین‌جا ارسال کنید.",
            ]
        )

    if card_to_card_number.strip():
        safe_card = html.escape(card_to_card_number.strip())
        safe_holder = (
            html.escape(card_to_card_holder.strip()) if card_to_card_holder.strip() else "صاحب حساب"
        )
        parts.extend(
            [
                "",
                "🔹 <b>روش دوم (کارت‌به‌کارت مستقیم):</b>",
                f"شماره کارت: <code>{safe_card}</code>",
                f"به نام: <b>{safe_holder}</b>",
                "پس از واریز، <b>۴ رقم آخر کارت خودتان + زمان واریز</b> "
                "(یا تصویر رسید) را همین‌جا بفرستید تا تأیید شود.",
            ]
        )

    if not zarinlink_url.strip() and not card_to_card_number.strip():
        parts.extend(
            [
                "",
                "لطفاً برای دریافت لینک پرداخت یا شماره کارت مستقیم با پشتیبانی پیام دهید "
                "و سپس رسید را همین‌جا ارسال کنید.",
            ]
        )

    if founder_support_handle.strip():
        parts.extend(
            [
                "",
                f"📩 پشتیبانی مستقیم بنیان‌گذار: {html.escape(founder_support_handle.strip())}",
            ]
        )

    return "\n".join(parts)


def render_hour_36_followup(score: int, band: str, first_name: str | None = None) -> str:
    """Render the Hour-36 polite follow-up template from PRD 4.5 #4."""
    display_name = first_name.strip() if first_name and first_name.strip() else "دوست گرامی"
    return (
        HOUR_36_FOLLOWUP_TEMPLATE.replace("[نام/دوست گرامی]", display_name)
        .replace("{{SCORE}}", str(score))
        .replace("{{BAND}}", band)
    )
