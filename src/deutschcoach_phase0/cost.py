from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from deutschcoach_phase0.domain import CostBreakdown, EvaluatorOutput

AUDIO_INPUT_USD_PER_1M = Decimal("1.00")
TEXT_INPUT_USD_PER_1M = Decimal("0.30")
OUTPUT_USD_PER_1M = Decimal("2.50")

_ONE_MILLION = Decimal("1000000")
_USD_QUANTUM = Decimal("0.000001")
_TOMAN_QUANTUM = Decimal("1")
_AUDIO_TOKENS_PER_SEC = 32


def calculate_gemini_cost(
    output: EvaluatorOutput,
    audio_duration_sec: float,
    usd_to_toman_rate: int,
) -> CostBreakdown:
    """Compute exact Decimal USD micro-cost and integer Toman cost for a Gemini call."""
    prompt_tokens = max(0, output.prompt_tokens)
    if output.audio_input_tokens is not None and output.text_input_tokens is not None:
        audio_in = max(0, output.audio_input_tokens)
        text_in = max(0, output.text_input_tokens)
    else:
        estimated_audio = math.ceil(max(0.0, audio_duration_sec) * _AUDIO_TOKENS_PER_SEC)
        audio_in = min(prompt_tokens, estimated_audio)
        text_in = max(0, prompt_tokens - audio_in)

    output_tokens = max(0, output.candidates_tokens) + max(0, output.thoughts_tokens)
    total_input_tokens = audio_in + text_in

    raw_usd = (
        (Decimal(audio_in) * AUDIO_INPUT_USD_PER_1M)
        + (Decimal(text_in) * TEXT_INPUT_USD_PER_1M)
        + (Decimal(output_tokens) * OUTPUT_USD_PER_1M)
    ) / _ONE_MILLION

    estimated_cost_usd = raw_usd.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    estimated_cost_toman = int(
        (estimated_cost_usd * Decimal(usd_to_toman_rate)).quantize(
            _TOMAN_QUANTUM, rounding=ROUND_HALF_UP
        )
    )

    return CostBreakdown(
        audio_input_tokens=audio_in,
        text_input_tokens=text_in,
        output_tokens=output_tokens,
        total_input_tokens=total_input_tokens,
        estimated_cost_usd=estimated_cost_usd,
        estimated_cost_toman=estimated_cost_toman,
    )
