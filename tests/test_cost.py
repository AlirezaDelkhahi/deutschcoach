from __future__ import annotations

from decimal import Decimal

from deutschcoach_phase0.cost import calculate_gemini_cost
from deutschcoach_phase0.domain import EvaluatorOutput


def test_calculate_cost_uses_reported_modality_tokens_with_exact_decimal_and_int() -> None:
    output = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=3280,
        candidates_tokens=600,
        thoughts_tokens=200,
        audio_input_tokens=2880,
        text_input_tokens=400,
        model_name="gemini-2.5-flash",
    )
    cost = calculate_gemini_cost(
        output=output,
        audio_duration_sec=90.0,
        usd_to_toman_rate=273_000,
    )

    assert isinstance(cost.estimated_cost_usd, Decimal)
    assert isinstance(cost.estimated_cost_toman, int)
    assert cost.audio_input_tokens == 2880
    assert cost.text_input_tokens == 400
    assert cost.output_tokens == 800
    # 2880 * 1.00/1e6 + 400 * 0.30/1e6 + 800 * 2.50/1e6
    # = 0.00288 + 0.00012 + 0.00200 = 0.005000 USD
    assert cost.estimated_cost_usd == Decimal("0.005000")
    # 0.005000 * 273000 = 1365 Toman
    assert cost.estimated_cost_toman == 1365


def test_calculate_cost_fallback_estimation_when_modality_details_missing() -> None:
    output = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=1000,
        candidates_tokens=400,
        thoughts_tokens=100,
        audio_input_tokens=None,
        text_input_tokens=None,
    )
    # ceil(15.1 * 32) = ceil(483.2) = 484 audio tokens, 516 text tokens
    cost = calculate_gemini_cost(
        output=output,
        audio_duration_sec=15.1,
        usd_to_toman_rate=273_000,
    )

    assert cost.audio_input_tokens == 484
    assert cost.text_input_tokens == 516
    assert cost.output_tokens == 500
    # 484 * 1e-6 + 516 * 0.3e-6 + 500 * 2.5e-6
    # = 0.000484 + 0.0001548 + 0.00125 = 0.0018888 -> 0.001889
    assert cost.estimated_cost_usd == Decimal("0.001889")
    # 0.001889 * 273000 = 515.697 -> 516
    assert cost.estimated_cost_toman == 516


def test_calculate_cost_clamps_audio_tokens_to_prompt_tokens() -> None:
    output = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=300,
        candidates_tokens=100,
        thoughts_tokens=0,
        audio_input_tokens=None,
        text_input_tokens=None,
    )
    cost = calculate_gemini_cost(
        output=output,
        audio_duration_sec=90.0,
        usd_to_toman_rate=273_000,
    )
    assert cost.audio_input_tokens == 300
    assert cost.text_input_tokens == 0
    assert cost.estimated_cost_usd == Decimal("0.000550")
    # 0.000550 * 273000 = 150.15 -> 150
    assert cost.estimated_cost_toman == 150


def test_calculate_cost_uses_gemini_38_flash_rates_with_exact_decimal_and_int() -> None:
    output = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=3280,
        candidates_tokens=600,
        thoughts_tokens=200,
        audio_input_tokens=2880,
        text_input_tokens=400,
        model_name="gemini-3.8-flash",
    )
    cost = calculate_gemini_cost(
        output=output,
        audio_duration_sec=90.0,
        usd_to_toman_rate=273_000,
    )

    assert isinstance(cost.estimated_cost_usd, Decimal)
    assert isinstance(cost.estimated_cost_toman, int)
    assert cost.audio_input_tokens == 2880
    assert cost.text_input_tokens == 400
    assert cost.total_input_tokens == 3280
    assert cost.output_tokens == 800
    # (2880 + 400) * 0.75/1e6 + 800 * 3.75/1e6 = 0.002460 + 0.003000 = 0.005460 USD
    assert cost.estimated_cost_usd == Decimal("0.005460")
    # 0.005460 * 273000 = 1490.58 -> 1491 Toman
    assert cost.estimated_cost_toman == 1491

    prefixed = EvaluatorOutput(
        raw_text="{}",
        prompt_tokens=1500,
        candidates_tokens=200,
        thoughts_tokens=100,
        audio_input_tokens=1000,
        text_input_tokens=500,
        model_name="models/gemini-3.8-flash",
    )
    prefixed_cost = calculate_gemini_cost(
        output=prefixed,
        audio_duration_sec=30.0,
        usd_to_toman_rate=273_000,
    )
    # 1500 * 0.75/1e6 + 300 * 3.75/1e6 = 0.001125 + 0.001125 = 0.002250 USD
    assert prefixed_cost.estimated_cost_usd == Decimal("0.002250")
    # 0.002250 * 273000 = 614.25 -> 614 Toman
    assert prefixed_cost.estimated_cost_toman == 614
