from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from telegram import Bot, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from deutschcoach_phase0.domain import (
    IncomingAudioMeta,
    InlineButton,
    ReplyMessage,
    ServiceReply,
)
from deutschcoach_phase0.security import redact_sensitive_text
from deutschcoach_phase0.service import DiagnosticService

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CAPTION_LEN = 1024
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class TelegramAudioFetcher:
    """Downloads Telegram voice/audio file bytes to a local temp path."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def download_to_path(self, file_id: str, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tg_file = await self._bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=destination)
        return destination.stat().st_size


class BotHandlers:
    """Thin async python-telegram-bot v22 handlers delegating to DiagnosticService."""

    def __init__(
        self,
        service: DiagnosticService,
        admin_notify_chat_id: int | None = None,
        extra_secrets: Iterable[str] = (),
        admin_user_ids: Iterable[int] = (),
    ) -> None:
        self._service = service
        self._admin_notify_chat_id = admin_notify_chat_id
        self._extra_secrets = tuple(s for s in extra_secrets if s)
        self._admin_user_ids = frozenset(admin_user_ids)

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        start_arg = context.args[0] if context.args else None
        reply = await self._service.handle_start(user.id, user.username, user.first_name, start_arg)
        await self._send_reply(update, context, reply)

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        data = query.data or ""

        if data.startswith("level:"):
            reply = await self._service.handle_level_selected(
                user.id, data.split(":", 1)[1], user.username, user.first_name
            )
        elif data.startswith("timeline:"):
            reply = await self._service.handle_timeline_selected(
                user.id, data.split(":", 1)[1], user.username, user.first_name
            )
        elif data == "cta:beta":
            reply = await self._service.handle_cta_click(user.id)
        elif data == "payment:submit":
            reply = await self._service.handle_start_payment_proof(user.id)
        else:
            await query.answer("گزینه ناشناخته")
            return

        await self._send_reply(update, context, reply)

    async def voice_or_audio_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return

        meta = _extract_audio_meta(msg)
        if meta is None:
            return
        reply = await self._service.handle_voice(user.id, meta, user.username, user.first_name)
        await self._send_reply(update, context, reply)

    async def non_voice_audio_or_doc_msg(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return
        await msg.reply_text(
            "🎙️ لطفاً پاسخ خود را به‌صورت وُیس تلگرام (با نگه داشتن دکمه میکروفون) ارسال کنید."
        )

    async def text_or_photo_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return
        has_photo = bool(msg.photo)
        photo_file_id = msg.photo[-1].file_id if msg.photo else None
        text = msg.caption if has_photo else msg.text
        reply = await self._service.handle_text_message(
            user.id,
            text,
            has_photo=has_photo,
            photo_file_id=photo_file_id,
        )
        await self._send_reply(update, context, reply)

    async def approve_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return

        if user.id not in self._admin_user_ids:
            await msg.reply_text("⛔ دسترسی مجاز نیست.")
            return

        if not context.args or len(context.args) < 2:
            await msg.reply_text("فرمت صحیح: /approve <telegram_user_id> <tx_ref>")
            return

        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await msg.reply_text("شناسه کاربر باید عدد صحیح باشد.")
            return

        tx_ref = " ".join(context.args[1:]).strip()
        reply = await self._service.handle_admin_approve(user.id, target_user_id, tx_ref)
        await self._send_reply(update, context, reply)

    async def stats_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        reply = await self._service.handle_admin_stats(user.id)
        await self._send_reply(update, context, reply)

    async def followup_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return
        dry_run = bool(context.args and context.args[0].lower() in {"dry", "preview"})
        reply = await self._service.handle_admin_followup(user.id, dry_run=dry_run)

        for reply_msg in reply.messages:
            await msg.reply_text(
                reply_msg.text,
                parse_mode=reply_msg.parse_mode,
                reply_markup=_to_markup(reply_msg),
            )

        if reply.outbound_messages:
            sent_count = 0
            failed_count = 0
            for out in reply.outbound_messages:
                try:
                    await context.bot.send_message(
                        chat_id=out.chat_id,
                        text=out.message.text,
                        parse_mode=out.message.parse_mode,
                        reply_markup=_to_markup(out.message),
                    )
                    await self._service.mark_followup_sent(out.chat_id)
                    sent_count += 1
                except TelegramError as exc:
                    failed_count += 1
                    logger.warning(
                        "Failed sending Hour-36 follow-up to %s: %s",
                        out.chat_id,
                        redact_sensitive_text(str(exc), self._extra_secrets),
                    )
            await msg.reply_text(
                f"نتیجه ارسال پیگیری ۳۶ ساعته: موفق={sent_count} | ناموفق={failed_count}"
            )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err_text = redact_sensitive_text(str(context.error), self._extra_secrets)
        logger.error("Unhandled Telegram update error: %s", err_text)
        if (
            isinstance(update, Update)
            or hasattr(update, "message")
            or hasattr(update, "callback_query")
        ):
            msg = getattr(update, "message", None)
            query = getattr(update, "callback_query", None)
            can_reply_msg = (
                msg is not None
                and not isinstance(msg, InaccessibleMessage)
                and callable(getattr(msg, "reply_text", None))
            )
            try:
                if can_reply_msg:
                    await msg.reply_text(
                        "متأسفانه خطای موقت رخ داد. لطفاً چند لحظه دیگر دوباره تلاش کنید."
                    )
                elif query is not None:
                    await query.answer("خطای موقت رخ داد", show_alert=False)
            except TelegramError as exc:
                logger.warning(
                    "Failed sending error notification: %s",
                    redact_sensitive_text(str(exc), self._extra_secrets),
                )

    async def _send_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        reply: ServiceReply,
    ) -> None:
        query = update.callback_query
        if query is not None:
            await query.answer(reply.callback_answer)
            target_msg = query.message
        else:
            target_msg = update.message

        can_reply = (
            target_msg is not None
            and not isinstance(target_msg, InaccessibleMessage)
            and callable(getattr(target_msg, "reply_text", None))
        )
        if can_reply:
            for item in reply.messages:
                await target_msg.reply_text(
                    item.text,
                    parse_mode=item.parse_mode,
                    reply_markup=_to_markup(item),
                )
        elif update.effective_chat is not None:
            for item in reply.messages:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=item.text,
                    parse_mode=item.parse_mode,
                    reply_markup=_to_markup(item),
                )

        if reply.admin_notification is not None and self._admin_notify_chat_id is not None:
            notif = reply.admin_notification
            try:
                if notif.photo_file_id is not None:
                    caption, caption_parse_mode = _safe_telegram_caption(
                        notif.text, notif.parse_mode
                    )
                    await context.bot.send_photo(
                        chat_id=self._admin_notify_chat_id,
                        photo=notif.photo_file_id,
                        caption=caption,
                        parse_mode=caption_parse_mode,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=self._admin_notify_chat_id,
                        text=notif.text,
                        parse_mode=notif.parse_mode,
                    )
            except TelegramError as exc:
                logger.warning(
                    "Failed admin notification: %s",
                    redact_sensitive_text(str(exc), self._extra_secrets),
                )

        for out in reply.outbound_messages:
            try:
                await context.bot.send_message(
                    chat_id=out.chat_id,
                    text=out.message.text,
                    parse_mode=out.message.parse_mode,
                    reply_markup=_to_markup(out.message),
                )
            except TelegramError as exc:
                logger.warning(
                    "Failed outbound message to %s: %s",
                    out.chat_id,
                    redact_sensitive_text(str(exc), self._extra_secrets),
                )


def _safe_telegram_caption(text: str, parse_mode: str | None) -> tuple[str, str | None]:
    if len(text) <= TELEGRAM_MAX_CAPTION_LEN:
        return text, parse_mode
    plain = html.unescape(_HTML_TAG_RE.sub("", text)) if parse_mode == "HTML" else text
    if len(plain) > TELEGRAM_MAX_CAPTION_LEN:
        plain = plain[: TELEGRAM_MAX_CAPTION_LEN - 1].rstrip() + "…"
    return plain, None


def _extract_audio_meta(msg: object) -> IncomingAudioMeta | None:
    voice = getattr(msg, "voice", None)
    if voice is not None:
        return IncomingAudioMeta(
            file_id=voice.file_id,
            file_unique_id=getattr(voice, "file_unique_id", None),
            mime_type=getattr(voice, "mime_type", "audio/ogg") or "audio/ogg",
            file_size=getattr(voice, "file_size", None),
            reported_duration_sec=(
                float(voice.duration) if getattr(voice, "duration", None) is not None else None
            ),
            is_voice=True,
        )

    audio = getattr(msg, "audio", None)
    if audio is not None:
        return IncomingAudioMeta(
            file_id=audio.file_id,
            file_unique_id=getattr(audio, "file_unique_id", None),
            mime_type=getattr(audio, "mime_type", None),
            file_size=getattr(audio, "file_size", None),
            reported_duration_sec=(
                float(audio.duration) if getattr(audio, "duration", None) is not None else None
            ),
            is_voice=False,
        )
    return None


def _to_markup(msg: ReplyMessage) -> InlineKeyboardMarkup | None:
    if not msg.inline_keyboard:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for row in msg.inline_keyboard:
        rows.append([_to_tg_button(btn) for btn in row])
    return InlineKeyboardMarkup(rows)


def _to_tg_button(btn: InlineButton) -> InlineKeyboardButton:
    if btn.url is not None:
        return InlineKeyboardButton(text=btn.text, url=btn.url)
    return InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data)


def register_handlers(application: Application, handlers: BotHandlers) -> None:
    """Register all Phase 0 Telegram command, callback, and message handlers."""
    application.add_handler(CommandHandler("start", handlers.start_cmd))
    application.add_handler(CommandHandler("approve", handlers.approve_cmd))
    application.add_handler(CommandHandler("stats", handlers.stats_cmd))
    application.add_handler(CommandHandler("followup", handlers.followup_cmd))
    application.add_handler(CallbackQueryHandler(handlers.callback_router))
    application.add_handler(MessageHandler(filters.VOICE, handlers.voice_or_audio_msg))
    application.add_handler(
        MessageHandler(
            filters.AUDIO | filters.Document.ALL,
            handlers.non_voice_audio_or_doc_msg,
        )
    )
    application.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
            handlers.text_or_photo_msg,
        )
    )
    application.add_error_handler(handlers.error_handler)
