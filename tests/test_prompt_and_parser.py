from __future__ import annotations

import json

from deutschcoach_phase0.domain import (
    MSG_VPN_OFF_WARNING,
    PROMPT_CARD_B1,
    PROMPT_CARD_B2,
    EvaluationErrorItem,
    EvaluationReport,
    EvaluationSubscores,
    Level,
    Timeline,
)
from deutschcoach_phase0.prompt_builder import (
    SYSTEM_PROMPT,
    TELEGRAM_MAX_MESSAGE_LEN,
    build_user_prompt,
    format_beta_offer_html,
    format_payment_instructions_html,
    format_scorecard_html,
    get_prompt_card,
    parse_evaluation_json,
    render_hour_36_followup,
)


def test_prompt_cards_and_prompts_match_prd_verbatim() -> None:
    assert "Brauchen Jugendliche heute ein eigenes Smartphone?" in PROMPT_CARD_B1
    assert "Gesund leben am Arbeitsplatz" in PROMPT_CARD_B2
    assert get_prompt_card(Level.B1) == PROMPT_CARD_B1
    assert get_prompt_card(Level.B2) == PROMPT_CARD_B2

    assert 'Goethe-Institut certified B1/B2 oral examiner ("Prüfer")' in SYSTEM_PROMPT
    assert "NEVER invent mistakes the speaker did not say." in SYSTEM_PROMPT

    user_prompt = build_user_prompt(Level.B1, Timeline.LESS_THAN_1_MONTH)
    assert "- Target Level: B1" in user_prompt
    assert "کمتر از ۱ ماه دیگر" in user_prompt
    assert "Brauchen Jugendliche heute ein eigenes Smartphone?" in user_prompt
    assert "{{LEVEL}}" not in user_prompt
    assert "{{TIMELINE}}" not in user_prompt
    assert "{{PROMPT_TITLE_AND_BULLETS}}" not in user_prompt


def test_parse_evaluation_json_clean_and_markdown_fenced() -> None:
    payload = {
        "is_valid_german_attempt": True,
        "transcript_summary_de": "Ich habe über Smartphones gesprochen.",
        "total_score": 68,
        "band": "قبول",
        "subscores": {
            "task_fulfillment": 18,
            "coherence_interaction": 17,
            "vocab_grammar": 16,
            "pronunciation_fluency": 17,
        },
        "top_3_errors": [
            {
                "category": "Grammatik",
                "exact_spoken_excerpt": "weil ich habe ein Handy",
                "corrected_german": "weil ich ein Handy habe",
                "explanation_fa": "در جمله پیرو با weil فعل باید به آخر جمله برود.",
            }
        ],
        "strength_fa": "ساختار ۵ بخشی را خوب رعایت کردید.",
        "timeline_action_plan_fa": "روی جایگاه فعل در جملات پیرو کار کنید.",
    }
    fenced = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    parsed = parse_evaluation_json(fenced)
    assert parsed.ok is True
    assert parsed.partial_fallback is False
    assert parsed.report is not None
    assert parsed.report.total_score == 68
    assert parsed.report.band == "قبول"
    assert parsed.report.subscores.task_fulfillment == 18
    assert len(parsed.report.top_3_errors) == 1


def test_parse_evaluation_json_rejects_truncated_or_non_dict_safely() -> None:
    for broken in ("", "{invalid json", '["not", "a", "dict"]', '{"total_score": 70'):
        parsed = parse_evaluation_json(broken)
        assert parsed.ok is False
        assert parsed.report is None
        assert parsed.fallback_reason is not None


def test_parse_evaluation_json_clamps_and_normalizes_partial_dict() -> None:
    messy = {
        "total_score": 150,
        "band": "UNKNOWN_BAND",
        "subscores": {
            "task_fulfillment": 30,
            "coherence_interaction": -5,
            "vocab_grammar": "bad",
            "pronunciation_fluency": 14,
        },
        "top_3_errors": [
            "not-a-dict",
            {
                "category": "Grammatik",
                "exact_spoken_excerpt": "ich gehen",
                "corrected_german": "ich gehe",
                "explanation_fa": "صرف فعل اشتباه است.",
            },
            {
                "category": "Wortschatz",
                "exact_spoken_excerpt": "viel gut",
                "corrected_german": "sehr gut",
                "explanation_fa": "به جای viel از sehr استفاده کنید.",
            },
            {
                "category": "Aussprache",
                "exact_spoken_excerpt": "Handi",
                "corrected_german": "Handy",
                "explanation_fa": "تلفظ انگلیسی حفظ می‌شود.",
            },
            {
                "category": "Struktur",
                "exact_spoken_excerpt": "extra",
                "corrected_german": "extra",
                "explanation_fa": "باید حذف شود.",
            },
        ],
    }

    parsed = parse_evaluation_json(json.dumps(messy, ensure_ascii=False))
    assert parsed.ok is True
    assert parsed.partial_fallback is True
    assert parsed.report is not None
    assert parsed.report.total_score == 100
    assert parsed.report.band == "قبول"
    assert parsed.report.subscores.task_fulfillment == 25
    assert parsed.report.subscores.coherence_interaction == 0
    assert parsed.report.subscores.vocab_grammar == 0
    assert parsed.report.subscores.pronunciation_fluency == 14
    assert len(parsed.report.top_3_errors) == 3


def test_format_scorecard_html_escapes_and_stays_under_telegram_limit() -> None:
    huge = "<script>alert('xss')</script> & " + ("آلمانی " * 1500)
    parsed = parse_evaluation_json(
        json.dumps(
            {
                "is_valid_german_attempt": True,
                "transcript_summary_de": huge,
                "total_score": 55,
                "band": "لب مرز",
                "subscores": {
                    "task_fulfillment": 14,
                    "coherence_interaction": 14,
                    "vocab_grammar": 13,
                    "pronunciation_fluency": 14,
                },
                "top_3_errors": [
                    {
                        "category": "Grammatik",
                        "exact_spoken_excerpt": "<bad> & excerpt " + ("x" * 1000),
                        "corrected_german": "<b>fixed</b> " + ("y" * 1000),
                        "explanation_fa": huge,
                    }
                ],
                "strength_fa": huge,
                "timeline_action_plan_fa": huge,
            },
            ensure_ascii=False,
        )
    )
    assert parsed.report is not None
    html_text = format_scorecard_html(parsed.report, Level.B1)

    assert len(html_text) <= 4096
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "55/100" in html_text
    assert "لب مرز" in html_text
    assert "گوته" in html_text
    assert "ÖSD" in html_text


def test_beta_offer_and_payment_instructions_include_vpn_warning_and_prices() -> None:
    offer = format_beta_offer_html(beta_price_toman=490_000, regular_price_toman=790_000)
    assert "490,000" in offer or "۴۹۰٬۰۰۰" in offer or "490000" in offer
    assert "790,000" in offer or "۷۹۰٬۰۰۰" in offer or "790000" in offer
    assert "10" in offer or "۱۰" in offer
    assert "90" in offer or "۹۰" in offer

    instructions = format_payment_instructions_html(
        beta_price_toman=490_000,
        zarinlink_url="https://zarinp.al/deutschcoach-beta",
        card_to_card_number="6037-9975-1234-5678",
        card_to_card_holder="علی رضایی",
        founder_support_handle="@deutschcoach_support",
    )
    assert MSG_VPN_OFF_WARNING in instructions
    assert "https://zarinp.al/deutschcoach-beta" in instructions
    assert "6037-9975-1234-5678" in instructions


def test_format_scorecard_html_handles_adversarial_entity_expansion() -> None:
    adversarial = "<&>" * 500
    parsed = parse_evaluation_json(
        json.dumps(
            {
                "is_valid_german_attempt": True,
                "transcript_summary_de": adversarial,
                "total_score": 45,
                "band": "رد",
                "subscores": {
                    "task_fulfillment": 10,
                    "coherence_interaction": 11,
                    "vocab_grammar": 12,
                    "pronunciation_fluency": 12,
                },
                "top_3_errors": [
                    {
                        "category": "Grammatik",
                        "exact_spoken_excerpt": adversarial,
                        "corrected_german": adversarial,
                        "explanation_fa": adversarial,
                    }
                    for _ in range(3)
                ],
                "strength_fa": adversarial,
                "timeline_action_plan_fa": adversarial,
            },
            ensure_ascii=False,
        )
    )
    assert parsed.report is not None
    rendered = format_scorecard_html(parsed.report, Level.B2)
    assert len(rendered) <= 4096
    assert rendered.count("<code>") == rendered.count("</code>")
    assert rendered.count("<i>") == rendered.count("</i>")
    assert rendered.count("<b>") == rendered.count("</b>")


def test_render_hour_36_followup_substitutes_placeholders() -> None:
    msg = render_hour_36_followup(score=58, band="لب مرز", first_name="سارا")
    assert "58/100 (لب مرز)" in msg
    assert "سلام سارا 👋" in msg
    assert "{{SCORE}}" not in msg
    assert "{{BAND}}" not in msg

    msg_default = render_hour_36_followup(score=62, band="قبول", first_name=None)
    assert "سلام دوست گرامی 👋" in msg_default


def test_format_scorecard_html_mixed_ascii_and_entities_hits_caps_under_4096() -> None:
    # Mixed ASCII + '&' / '<' strings designed to hit max_escaped_chars ceilings on every field.
    mixed_long = ("abcd&<ef" * 120)[:400]
    report = EvaluationReport(
        is_valid_german_attempt=True,
        transcript_summary_de=mixed_long,
        total_score=100,
        band="لب مرز",
        subscores=EvaluationSubscores(25, 25, 25, 25),
        top_3_errors=tuple(
            EvaluationErrorItem(
                category=mixed_long,
                exact_spoken_excerpt=mixed_long,
                corrected_german=mixed_long,
                explanation_fa=mixed_long,
            )
            for _ in range(3)
        ),
        strength_fa=mixed_long,
        timeline_action_plan_fa=mixed_long,
    )
    rendered = format_scorecard_html(report, Level.B2)
    assert len(rendered) <= 3900
    assert len(rendered) <= TELEGRAM_MAX_MESSAGE_LEN
    assert rendered.count("<code>") == rendered.count("</code>")
    assert rendered.count("<i>") == rendered.count("</i>")
    assert rendered.count("<b>") == rendered.count("</b>")

    # Even with an abnormally long band constructed directly, final 4096 invariant holds.
    extreme = EvaluationReport(
        is_valid_german_attempt=True,
        transcript_summary_de=mixed_long,
        total_score=100,
        band="لب مرز " + ("&" * 1200),
        subscores=EvaluationSubscores(25, 25, 25, 25),
        top_3_errors=report.top_3_errors,
        strength_fa=mixed_long,
        timeline_action_plan_fa=mixed_long,
    )
    rendered_extreme = format_scorecard_html(extreme, Level.B2)
    assert len(rendered_extreme) <= TELEGRAM_MAX_MESSAGE_LEN
    assert rendered_extreme.count("<code>") == rendered_extreme.count("</code>")
    assert rendered_extreme.count("<i>") == rendered_extreme.count("</i>")
    assert rendered_extreme.count("<b>") == rendered_extreme.count("</b>")


def test_parse_evaluation_json_rejects_empty_object_and_handles_non_finite_floats() -> None:
    for invalid_payload in (
        "{}",
        '{"unexpected": "error"}',
        '{"error": "rate limit"}',
        '{"total_score": NaN, "subscores": {}}',
        '{"total_score": Infinity}',
        '{"total_score": -Infinity}',
        '{"total_score": 1e309}',
    ):
        parsed = parse_evaluation_json(invalid_payload)
        assert parsed.ok is False, f"Expected failure for {invalid_payload}"
        assert parsed.report is None
        assert parsed.fallback_reason == "missing_core_evaluation_fields"

    # Partial dict with NaN total_score but valid subscore normalizes safely
    partial_nan_total = parse_evaluation_json(
        '{"total_score": NaN, "subscores": {"task_fulfillment": 18}}'
    )
    assert partial_nan_total.ok is True
    assert partial_nan_total.partial_fallback is True
    assert partial_nan_total.report is not None
    assert partial_nan_total.report.total_score == 18

    # Valid total_score with Infinity subscore clamps/falls back without raising
    partial_inf_sub = parse_evaluation_json(
        '{"total_score": 70, "subscores": {"task_fulfillment": Infinity}}'
    )
    assert partial_inf_sub.ok is True
    assert partial_inf_sub.partial_fallback is True
    assert partial_inf_sub.report is not None
    assert partial_inf_sub.report.total_score == 70
    assert partial_inf_sub.report.subscores.task_fulfillment == 0


def test_parse_evaluation_json_reconciles_disagreeing_band_with_score() -> None:
    for score, wrong_band, expected_band in (
        (40, "قبول", "رد"),
        (55, "قبول", "لب مرز"),
        (65, "رد", "قبول"),
    ):
        parsed = parse_evaluation_json(
            json.dumps(
                {
                    "is_valid_german_attempt": True,
                    "transcript_summary_de": "Test.",
                    "total_score": score,
                    "band": wrong_band,
                    "subscores": {
                        "task_fulfillment": 15,
                        "coherence_interaction": 15,
                        "vocab_grammar": 15,
                        "pronunciation_fluency": 15,
                    },
                    "top_3_errors": [],
                    "strength_fa": "خوب",
                    "timeline_action_plan_fa": "تمرین",
                },
                ensure_ascii=False,
            )
        )
        assert parsed.ok is True
        assert parsed.partial_fallback is True
        assert parsed.report is not None
        assert parsed.report.band == expected_band
