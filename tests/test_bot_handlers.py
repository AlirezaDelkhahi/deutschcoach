from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpcore
import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from telegram import Chat, InaccessibleMessage
from telegram.error import TelegramError
from telegram.ext import MessageHandler, filters

from deutschcoach_phase0.bootstrap import create_application, main
from deutschcoach_phase0.bot import BotHandlers, _extract_audio_meta
from deutschcoach_phase0.domain import (
    AdminNotification,
    InlineButton,
    OutboundMessage,
    ReplyMessage,
    ServiceReply,
    TransientEvaluatorError,
)
from deutschcoach_phase0.gemini_evaluator import GeminiSpeechEvaluator


@pytest.mark.asyncio
async def test_gemini_evaluator_parses_usage_and_redacts_errors(sample_settings) -> None:
    fake_response = SimpleNamespace(
        text='{"total_score": 70}',
        usage_metadata=SimpleNamespace(
            prompt_token_count=3200,
            candidates_token_count=550,
            thoughts_token_count=120,
            prompt_tokens_details=[
                SimpleNamespace(modality=genai_types.MediaModality.AUDIO, token_count=2800),
                SimpleNamespace(modality=genai_types.MediaModality.TEXT, token_count=400),
            ],
        ),
    )

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)
    fake_client.aio.aclose = AsyncMock()

    evaluator = GeminiSpeechEvaluator(
        api_key=sample_settings.gemini_api_key,
        model=sample_settings.gemini_model,
        thinking_budget=sample_settings.gemini_thinking_budget,
        extra_secrets=sample_settings.sensitive_values(),
        client=fake_client,
    )

    out = await evaluator.evaluate_audio(
        audio_bytes=b"ogg-data",
        mime_type="audio/ogg",
        system_prompt="SYS",
        user_prompt="USR",
        timeout_sec=30,
    )
    assert out.raw_text == '{"total_score": 70}'
    assert out.prompt_tokens == 3200
    assert out.candidates_tokens == 550
    assert out.thoughts_tokens == 120
    assert out.audio_input_tokens == 2800
    assert out.text_input_tokens == 400

    # APIError must raise TransientEvaluatorError without leaking API key
    fake_client.aio.models.generate_content = AsyncMock(
        side_effect=genai_errors.APIError(
            503,
            {"error": {"message": f"bad key={sample_settings.gemini_api_key}"}},
        )
    )
    with pytest.raises(TransientEvaluatorError) as exc_info:
        await evaluator.evaluate_audio(b"x", "audio/ogg", "SYS", "USR", 30)
    assert sample_settings.gemini_api_key not in str(exc_info.value)

    await evaluator.aclose()
    fake_client.aio.aclose.assert_awaited_once()


def _make_update(
    user_id: int = 5001,
    text: str | None = None,
    callback_data: str | None = None,
    voice: SimpleNamespace | None = None,
    photo: list[SimpleNamespace] | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id, username="tester", first_name="Tester")
    update.effective_chat = SimpleNamespace(id=user_id)
    if callback_data is not None:
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.message = MagicMock()
        query.message.reply_text = AsyncMock()
        update.callback_query = query
        update.message = None
    else:
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.voice = voice
        msg.audio = None
        msg.document = None
        msg.photo = photo or []
        msg.reply_text = AsyncMock()
        update.message = msg
        update.callback_query = None
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    ctx.error = None
    return ctx


@pytest.mark.asyncio
async def test_bot_handlers_dispatch_and_render_replies(sample_settings) -> None:
    fake_service = MagicMock()
    fake_service.handle_start = AsyncMock(
        return_value=ServiceReply(
            messages=(
                ReplyMessage(
                    text="سلام!",
                    inline_keyboard=((InlineButton("B1", callback_data="level:B1"),),),
                ),
            )
        )
    )
    fake_service.handle_level_selected = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="زمان آزمون؟"),),
            callback_answer="B1 انتخاب شد",
        )
    )
    fake_service.handle_voice = AsyncMock(
        return_value=ServiceReply(messages=(ReplyMessage(text="کارنامه آماده شد"),))
    )
    fake_service.handle_admin_followup = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="ارسال شد"),),
            outbound_messages=(OutboundMessage(chat_id=7001, message=ReplyMessage(text="پیگیری")),),
        )
    )
    fake_service.mark_followup_sent = AsyncMock()

    handlers = BotHandlers(
        service=fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
    )

    u_start = _make_update(text="/start tg_1")
    await handlers.start_cmd(u_start, _make_context(args=["tg_1"]))
    fake_service.handle_start.assert_awaited_once_with(5001, "tester", "Tester", "tg_1")
    u_start.message.reply_text.assert_awaited_once()

    u_cb = _make_update(callback_data="level:B1")
    await handlers.callback_router(u_cb, _make_context())
    fake_service.handle_level_selected.assert_awaited_once_with(5001, "B1", "tester", "Tester")
    u_cb.callback_query.answer.assert_awaited_once_with("B1 انتخاب شد")
    u_cb.callback_query.message.reply_text.assert_awaited_once()

    u_voice = _make_update(
        voice=SimpleNamespace(
            file_id="f1",
            file_unique_id="u1",
            mime_type="audio/ogg",
            file_size=120_000,
            duration=45,
        )
    )
    await handlers.voice_or_audio_msg(u_voice, _make_context())
    fake_service.handle_voice.assert_awaited_once()

    u_fu = _make_update(user_id=999001, text="/followup")
    ctx_fu = _make_context()
    await handlers.followup_cmd(u_fu, ctx_fu)
    ctx_fu.bot.send_message.assert_awaited_once()
    fake_service.mark_followup_sent.assert_awaited_once_with(7001)


@pytest.mark.asyncio
async def test_bot_error_handler_replies_cleanly_without_stack_trace(sample_settings) -> None:
    fake_service = MagicMock()
    handlers = BotHandlers(
        service=fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        extra_secrets=sample_settings.sensitive_values(),
    )

    u = _make_update(text="hi")
    ctx = _make_context()
    ctx.error = RuntimeError(f"boom {sample_settings.telegram_bot_token}")

    await handlers.error_handler(u, ctx)
    u.message.reply_text.assert_awaited_once()
    sent_text = u.message.reply_text.await_args.args[0]
    assert sample_settings.telegram_bot_token not in sent_text
    assert "RuntimeError" not in sent_text


@pytest.mark.asyncio
async def test_bot_admin_approve_stats_and_bootstrap_wiring(sample_settings) -> None:
    fake_service = MagicMock()
    fake_service.handle_admin_approve = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="تأیید شد"),),
            outbound_messages=(OutboundMessage(chat_id=5001, message=ReplyMessage(text="مبارک")),),
        )
    )
    fake_service.handle_admin_stats = AsyncMock(
        return_value=ServiceReply(messages=(ReplyMessage(text="آمار"),))
    )
    fake_service.handle_text_message = AsyncMock(
        return_value=ServiceReply(messages=(ReplyMessage(text="ثبت شد"),))
    )
    handlers = BotHandlers(
        fake_service,
        admin_notify_chat_id=999001,
        admin_user_ids=sample_settings.admin_user_ids,
    )

    u_bad_args = _make_update(user_id=999001, text="/approve")
    await handlers.approve_cmd(u_bad_args, _make_context(args=[]))
    assert "فرمت صحیح" in u_bad_args.message.reply_text.await_args.args[0]

    u_approve = _make_update(user_id=999001, text="/approve 5001 ZP77")
    ctx_app = _make_context(args=["5001", "ZP77"])
    await handlers.approve_cmd(u_approve, ctx_app)
    fake_service.handle_admin_approve.assert_awaited_once_with(999001, 5001, "ZP77")
    ctx_app.bot.send_message.assert_awaited_once()

    u_stats = _make_update(user_id=999001, text="/stats")
    await handlers.stats_cmd(u_stats, _make_context())
    fake_service.handle_admin_stats.assert_awaited_once_with(999001)

    u_photo = _make_update(user_id=5001, photo=[SimpleNamespace(file_id="p1")])
    u_photo.message.caption = "رسید"
    await handlers.text_or_photo_msg(u_photo, _make_context())
    fake_service.handle_text_message.assert_awaited_once_with(
        5001, "رسید", has_photo=True, photo_file_id="p1"
    )

    app = create_application(sample_settings)
    assert app is not None
    assert sample_settings.sqlite_path.is_file()


@pytest.mark.asyncio
async def test_bot_photo_receipt_without_caption_forwards_via_send_photo(sample_settings) -> None:
    fake_service = MagicMock()
    admin_caption = (
        "💳 <b>رسید پرداخت جدید ثبت شد</b>\n"
        "• کاربر: <code>5001</code> (@tester)\n"
        "• متن/مرجع: <code>photo_receipt</code>\n\n"
        "برای تأیید:\n<code>/approve 5001 TX_REF</code>"
    )
    fake_service.handle_text_message = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="✅ رسید شما ثبت شد", parse_mode=None),),
            admin_notification=AdminNotification(
                text=admin_caption,
                parse_mode="HTML",
                photo_file_id="photo_hi_res_id",
            ),
        )
    )
    handlers = BotHandlers(
        fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        admin_user_ids=sample_settings.admin_user_ids,
    )

    u_photo = _make_update(
        user_id=5001,
        photo=[SimpleNamespace(file_id="photo_thumb"), SimpleNamespace(file_id="photo_hi_res_id")],
    )
    u_photo.message.caption = None
    ctx = _make_context()
    await handlers.text_or_photo_msg(u_photo, ctx)

    fake_service.handle_text_message.assert_awaited_once_with(
        5001, None, has_photo=True, photo_file_id="photo_hi_res_id"
    )
    ctx.bot.send_photo.assert_awaited_once_with(
        chat_id=999001,
        photo="photo_hi_res_id",
        caption=admin_caption,
        parse_mode="HTML",
    )
    ctx.bot.send_message.assert_not_awaited()

    # Over-1024 char caption must safely truncate without broken HTML tags
    huge_caption = "<b>رسید</b> " + ("&" * 1200)
    fake_service.handle_text_message = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="ok", parse_mode=None),),
            admin_notification=AdminNotification(
                text=huge_caption,
                parse_mode="HTML",
                photo_file_id="photo_hi_res_id",
            ),
        )
    )
    ctx_huge = _make_context()
    await handlers.text_or_photo_msg(u_photo, ctx_huge)
    ctx_huge.bot.send_photo.assert_awaited_once()
    sent_kwargs = ctx_huge.bot.send_photo.await_args.kwargs
    assert len(sent_kwargs["caption"]) <= 1024
    assert sent_kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_gemini_evaluator_translates_httpx_and_httpx2_errors_and_redacts(
    sample_settings,
) -> None:
    fake_client = MagicMock()
    fake_client.aio.aclose = AsyncMock()
    evaluator = GeminiSpeechEvaluator(
        api_key=sample_settings.gemini_api_key,
        model=sample_settings.gemini_model,
        thinking_budget=sample_settings.gemini_thinking_budget,
        extra_secrets=sample_settings.sensitive_values(),
        client=fake_client,
    )

    transient_exceptions: list[Exception] = [
        httpx.TimeoutException(f"timeout key={sample_settings.gemini_api_key}"),
        httpx.ConnectError(f"connect failed token={sample_settings.telegram_bot_token}"),
        httpcore.ConnectError(f"httpcore fail key={sample_settings.gemini_api_key}"),
    ]
    try:
        import httpx2

        transient_exceptions.append(
            httpx2.TimeoutException(f"httpx2 timeout key={sample_settings.gemini_api_key}")
        )
    except ImportError:
        pass

    for exc in transient_exceptions:
        fake_client.aio.models.generate_content = AsyncMock(side_effect=exc)
        with pytest.raises(TransientEvaluatorError) as exc_info:
            await evaluator.evaluate_audio(b"ogg", "audio/ogg", "SYS", "USR", 30)
        assert sample_settings.gemini_api_key not in str(exc_info.value)
        assert sample_settings.telegram_bot_token not in str(exc_info.value)

    await evaluator.aclose()


@pytest.mark.asyncio
async def test_bot_approve_cmd_checks_admin_auth_before_syntax(sample_settings) -> None:
    fake_service = MagicMock()
    fake_service.handle_admin_approve = AsyncMock()
    handlers = BotHandlers(
        fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        admin_user_ids=sample_settings.admin_user_ids,
    )

    u_bare = _make_update(user_id=5001, text="/approve")
    await handlers.approve_cmd(u_bare, _make_context(args=[]))
    assert u_bare.message.reply_text.await_args.args[0] == "⛔ دسترسی مجاز نیست."

    u_bad = _make_update(user_id=5001, text="/approve not_an_int")
    await handlers.approve_cmd(u_bad, _make_context(args=["not_an_int"]))
    assert u_bad.message.reply_text.await_args.args[0] == "⛔ دسترسی مجاز نیست."
    fake_service.handle_admin_approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_error_handler_swallows_secondary_telegram_errors(sample_settings) -> None:
    fake_service = MagicMock()
    handlers = BotHandlers(
        service=fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        extra_secrets=sample_settings.sensitive_values(),
        admin_user_ids=sample_settings.admin_user_ids,
    )

    u_msg = _make_update(text="hi")
    u_msg.message.reply_text = AsyncMock(
        side_effect=TelegramError(f"Forbidden bot={sample_settings.telegram_bot_token}")
    )
    ctx = _make_context()
    ctx.error = RuntimeError("primary failure")
    await handlers.error_handler(u_msg, ctx)
    u_msg.message.reply_text.assert_awaited_once()

    u_cb = _make_update(callback_data="level:B1")
    u_cb.callback_query.answer = AsyncMock(side_effect=TelegramError("Query is too old"))
    await handlers.error_handler(u_cb, ctx)
    u_cb.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_gemini_evaluator_classifies_potentially_billed_vs_unbilled_errors(
    sample_settings,
) -> None:
    fake_client = MagicMock()
    fake_client.aio.aclose = AsyncMock()
    evaluator = GeminiSpeechEvaluator(
        api_key=sample_settings.gemini_api_key,
        model=sample_settings.gemini_model,
        thinking_budget=sample_settings.gemini_thinking_budget,
        extra_secrets=sample_settings.sensitive_values(),
        client=fake_client,
    )

    billed_cases: list[Exception] = [
        TimeoutError("client timeout"),
        httpx.ReadTimeout("read timed out"),
        httpcore.ReadTimeout("httpcore read timed out"),
        genai_errors.APIError(400, {"error": {"message": "bad request"}}),
    ]
    unbilled_cases: list[Exception] = [
        OSError("dns error"),
        httpx.ConnectTimeout("connect timeout"),
        httpx.ConnectError("connect failed"),
        httpx.ProxyError("proxy down"),
        httpcore.ConnectTimeout("httpcore connect timeout"),
        httpcore.ConnectError("httpcore connect error"),
        httpcore.ProxyError("httpcore proxy error"),
        genai_errors.APIError(429, {"error": {"message": "rate limit"}}),
        genai_errors.APIError(503, {"error": {"message": "unavailable"}}),
    ]
    try:
        import httpcore2
        import httpx2

        billed_cases.extend([httpx2.ReadTimeout("r2"), httpcore2.ReadTimeout("cr2")])
        unbilled_cases.extend(
            [
                httpx2.ConnectTimeout("ct2"),
                httpx2.ConnectError("ce2"),
                httpx2.ProxyError("p2"),
                httpcore2.ConnectTimeout("cct2"),
                httpcore2.ConnectError("cce2"),
                httpcore2.ProxyError("cp2"),
            ]
        )
    except ImportError:
        pass

    for exc in billed_cases:
        fake_client.aio.models.generate_content = AsyncMock(side_effect=exc)
        with pytest.raises(TransientEvaluatorError) as exc_info:
            await evaluator.evaluate_audio(b"ogg", "audio/ogg", "SYS", "USR", 30)
        assert exc_info.value.potentially_billed is True, f"Expected billed for {type(exc)}"

    for exc in unbilled_cases:
        fake_client.aio.models.generate_content = AsyncMock(side_effect=exc)
        with pytest.raises(TransientEvaluatorError) as exc_info:
            await evaluator.evaluate_audio(b"ogg", "audio/ogg", "SYS", "USR", 30)
        assert exc_info.value.potentially_billed is False, f"Expected unbilled for {type(exc)}"

    # Empty response without usage_metadata must be treated as potentially_billed=True
    fake_client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text="", usage_metadata=None)
    )
    with pytest.raises(TransientEvaluatorError) as empty_exc:
        await evaluator.evaluate_audio(b"ogg", "audio/ogg", "SYS", "USR", 30)
    assert empty_exc.value.potentially_billed is True

    await evaluator.aclose()


@pytest.mark.asyncio
async def test_bot_rejects_document_and_audio_attachments_without_calling_handle_voice(
    sample_settings,
) -> None:
    fake_service = MagicMock()
    fake_service.handle_voice = AsyncMock()
    handlers = BotHandlers(
        service=fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        admin_user_ids=sample_settings.admin_user_ids,
    )

    doc_msg = SimpleNamespace(
        voice=None,
        audio=None,
        document=SimpleNamespace(file_id="d1", mime_type="audio/ogg", file_size=50_000),
    )
    assert _extract_audio_meta(doc_msg) is None

    u_doc = _make_update(user_id=5001)
    u_doc.message.document = SimpleNamespace(file_id="d1", mime_type="audio/ogg", file_size=50_000)
    await handlers.non_voice_audio_or_doc_msg(u_doc, _make_context())
    u_doc.message.reply_text.assert_awaited_once()
    assert "وُیس تلگرام" in u_doc.message.reply_text.await_args.args[0]

    u_audio = _make_update(user_id=5001)
    u_audio.message.audio = SimpleNamespace(file_id="a1", mime_type="audio/mpeg", file_size=50_000)
    await handlers.non_voice_audio_or_doc_msg(u_audio, _make_context())
    assert u_audio.message.reply_text.await_count == 1
    fake_service.handle_voice.assert_not_awaited()

    app = create_application(sample_settings)
    msg_handlers = [h for h in app.handlers[0] if isinstance(h, MessageHandler)]
    voice_handlers = [h for h in msg_handlers if h.callback.__name__ == "voice_or_audio_msg"]
    assert len(voice_handlers) == 1
    assert voice_handlers[0].filters is filters.VOICE


@pytest.mark.asyncio
async def test_bot_send_reply_falls_back_to_send_message_on_inaccessible_message(
    sample_settings,
) -> None:
    fake_service = MagicMock()
    fake_service.handle_level_selected = AsyncMock(
        return_value=ServiceReply(
            messages=(ReplyMessage(text="سطح ثبت شد", parse_mode=None),),
            callback_answer="B1",
        )
    )
    handlers = BotHandlers(
        service=fake_service,
        admin_notify_chat_id=sample_settings.admin_notify_chat_id,
        admin_user_ids=sample_settings.admin_user_ids,
    )

    update = MagicMock()
    update.effective_user = SimpleNamespace(id=5001, username="tester", first_name="Tester")
    update.effective_chat = SimpleNamespace(id=5001)
    query = MagicMock()
    query.data = "level:B1"
    query.answer = AsyncMock()
    query.message = InaccessibleMessage(chat=Chat(id=5001, type="private"), message_id=99)
    update.callback_query = query
    update.message = None

    ctx = _make_context()
    await handlers.callback_router(update, ctx)
    query.answer.assert_awaited_once_with("B1")
    ctx.bot.send_message.assert_awaited_once_with(
        chat_id=5001,
        text="سطح ثبت شد",
        parse_mode=None,
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_bootstrap_schedules_hourly_audio_cleanup_and_emits_no_warnings(
    sample_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app = create_application(sample_settings)
        assert app is not None
    assert not caught

    # When JobQueue is present, hourly purge job (interval=3600, first=3600) is registered
    stale = sample_settings.tmp_audio_dir / "old_25h.ogg"
    stale.write_bytes(b"stale")
    old_ts = time.time() - (25 * 3600)
    import os

    os.utime(stale, (old_ts, old_ts))

    fake_job_queue = MagicMock()
    fake_job_queue.run_repeating = MagicMock()

    from telegram.ext import Application

    monkeypatch.setattr(Application, "job_queue", property(lambda _self: fake_job_queue))
    create_application(sample_settings)
    fake_job_queue.run_repeating.assert_called_once()
    args, kwargs = fake_job_queue.run_repeating.call_args
    assert kwargs.get("interval") == 3600
    assert kwargs.get("first") == 3600
    callback = args[0]
    await callback(MagicMock())
    assert not stale.exists()


def test_bootstrap_configures_telegram_timeouts_and_main_bootstrap_retries(
    sample_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_application(sample_settings)

    regular_req = app.bot.request
    assert regular_req.read_timeout == 30.0
    assert regular_req._client.timeout == httpx.Timeout(
        connect=15.0,
        read=30.0,
        write=30.0,
        pool=10.0,
    )
    assert regular_req._media_write_timeout == 60.0

    assert app.updater is not None
    get_updates_req = app.updater.bot._request[0]
    assert get_updates_req.read_timeout == 30.0
    assert get_updates_req._client.timeout == httpx.Timeout(
        connect=15.0,
        read=30.0,
        write=30.0,
        pool=10.0,
    )

    fake_app = MagicMock()
    fake_app.run_polling = MagicMock()
    monkeypatch.setattr("deutschcoach_phase0.bootstrap.load_settings", lambda: sample_settings)
    monkeypatch.setattr("deutschcoach_phase0.bootstrap.configure_logging", lambda *_a, **_kw: None)
    monkeypatch.setattr("deutschcoach_phase0.bootstrap.create_application", lambda _s: fake_app)

    main()
    fake_app.run_polling.assert_called_once_with(
        drop_pending_updates=False,
        bootstrap_retries=-1,
    )
