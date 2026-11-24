from __future__ import annotations

import asyncio
from collections.abc import Iterable

import httpcore
import httpx
from google import genai
from google.genai import errors, types

from deutschcoach_phase0.domain import EvaluatorOutput, TransientEvaluatorError
from deutschcoach_phase0.security import redact_sensitive_text


def _build_http_error_groups() -> tuple[
    tuple[type[BaseException], ...],
    tuple[type[BaseException], ...],
    tuple[type[BaseException], ...],
]:
    transient: list[type[BaseException]] = [
        httpx.HTTPError,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        httpcore.TimeoutException,
        httpcore.ProxyError,
    ]
    read_timeouts: list[type[BaseException]] = [
        TimeoutError,
        httpx.ReadTimeout,
        httpcore.ReadTimeout,
    ]
    pre_send: list[type[BaseException]] = [
        OSError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ProxyError,
        httpx.PoolTimeout,
        httpcore.ConnectError,
        httpcore.ConnectTimeout,
        httpcore.ProxyError,
        httpcore.PoolTimeout,
    ]
    try:
        import httpx2

        transient.append(httpx2.HTTPError)
        read_timeouts.append(httpx2.ReadTimeout)
        pre_send.extend(
            (
                httpx2.ConnectError,
                httpx2.ConnectTimeout,
                httpx2.ProxyError,
                httpx2.PoolTimeout,
            )
        )
    except ImportError:
        pass
    try:
        import httpcore2

        transient.extend(
            (
                httpcore2.NetworkError,
                httpcore2.ProtocolError,
                httpcore2.TimeoutException,
                httpcore2.ProxyError,
            )
        )
        read_timeouts.append(httpcore2.ReadTimeout)
        pre_send.extend(
            (
                httpcore2.ConnectError,
                httpcore2.ConnectTimeout,
                httpcore2.ProxyError,
                httpcore2.PoolTimeout,
            )
        )
    except ImportError:
        pass
    try:
        import aiohttp

        transient.append(aiohttp.ClientError)
    except ImportError:
        pass
    return tuple(transient), tuple(read_timeouts), tuple(pre_send)


_TRANSIENT_HTTP_ERRORS, _READ_TIMEOUT_ERRORS, _PRE_SEND_ERRORS = _build_http_error_groups()


def _is_potentially_billed(exc: BaseException) -> bool:
    # Check read/client timeouts before OSError because TimeoutError subclasses OSError.
    if isinstance(exc, _READ_TIMEOUT_ERRORS):
        return True
    if isinstance(exc, _PRE_SEND_ERRORS):
        return False
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        if code == 429 or (isinstance(code, int) and 500 <= code <= 599):
            return False
        return True
    return False


class GeminiSpeechEvaluator:
    """Async Gemini native audio evaluator using google-genai SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        thinking_budget: int = 512,
        thinking_level: str = "low",
        extra_secrets: Iterable[str] = (),
        client: genai.Client | None = None,
    ) -> None:
        self._model = model
        self._thinking_budget = thinking_budget
        self._thinking_level = thinking_level.strip().lower()
        self._extra_secrets = tuple(s for s in extra_secrets if s)
        self._client = client if client is not None else genai.Client(api_key=api_key)

    def _build_thinking_config(self) -> types.ThinkingConfig:
        normalized_model = self._model.strip().lower().removeprefix("models/")
        if normalized_model.startswith("gemini-3"):
            return types.ThinkingConfig(thinking_level=self._thinking_level)
        return types.ThinkingConfig(thinking_budget=self._thinking_budget)

    async def evaluate_audio(
        self,
        audio_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        timeout_sec: int,
    ) -> EvaluatorOutput:
        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=0.2,
            thinking_config=self._build_thinking_config(),
        )

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[user_prompt, audio_part],
                    config=config,
                ),
                timeout=timeout_sec,
            )
        except (
            errors.APIError,
            TimeoutError,
            OSError,
            *_TRANSIENT_HTTP_ERRORS,
        ) as exc:
            safe_msg = redact_sensitive_text(str(exc), self._extra_secrets)
            raise TransientEvaluatorError(
                f"Gemini evaluation transient failure: {safe_msg}",
                potentially_billed=_is_potentially_billed(exc),
            ) from exc

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        candidates_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
        audio_in, text_in = _extract_prompt_modality_tokens(usage)

        try:
            raw_text = response.text or ""
        except ValueError:
            raw_text = ""

        if not raw_text and usage is None:
            raise TransientEvaluatorError(
                "Gemini returned empty response without usage metadata",
                potentially_billed=True,
            )

        return EvaluatorOutput(
            raw_text=raw_text,
            prompt_tokens=prompt_tokens,
            candidates_tokens=candidates_tokens,
            thoughts_tokens=thoughts_tokens,
            audio_input_tokens=audio_in,
            text_input_tokens=text_in,
            model_name=self._model,
        )

    async def aclose(self) -> None:
        await self._client.aio.aclose()


def _extract_prompt_modality_tokens(usage: object) -> tuple[int | None, int | None]:
    details = getattr(usage, "prompt_tokens_details", None)
    if not details:
        return None, None

    audio_tokens = 0
    text_tokens = 0
    saw_known_modality = False

    for entry in details:
        modality = getattr(entry, "modality", None)
        token_count = int(getattr(entry, "token_count", 0) or 0)
        mod_name = str(getattr(modality, "value", modality)).upper()
        if mod_name.endswith("AUDIO"):
            audio_tokens += token_count
            saw_known_modality = True
        elif mod_name.endswith("TEXT"):
            text_tokens += token_count
            saw_known_modality = True

    if not saw_known_modality:
        return None, None
    return audio_tokens, text_tokens
