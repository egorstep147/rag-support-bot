"""Клиент в личке с ботом и операторы в темах группы поддержки."""

import asyncio
import contextlib
import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from support import chat
from support.company import company
from support.config import settings
from support.db import Session
from support.db.models import Message as Row
from support.llm import Attachment, get_stt
from support.rag import ingest

router = Router()
TG_LIMIT = 4000


def answer_kb(message_id: int, answered: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👍", callback_data=f"rate:{message_id}:1")
    kb.button(text="👎", callback_data=f"rate:{message_id}:-1")
    if chat.operators_enabled():
        kb.button(text="🙋 Позвать оператора" if answered else "🙋 Да, позвать оператора", callback_data="operator")
    kb.adjust(2, 1)
    return kb.as_markup()


async def _typing(bot: Bot, chat_id: int) -> None:
    with contextlib.suppress(Exception):
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4.5)


async def _conv_id(message: Message) -> int:
    u = message.from_user
    async with Session() as s, s.begin():
        return (await chat.conversation(s, "tg", str(u.id), u.full_name)).id


# ---------- Клиент ----------


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message) -> None:
    await _conv_id(message)
    await message.answer(company().welcome)


@router.message(Command("operator"), F.chat.type == "private")
async def operator_cmd(message: Message, bot: Bot) -> None:
    await message.answer(await chat.request_operator(bot, await _conv_id(message)))


@router.callback_query(F.data == "operator")
async def operator_button(callback: CallbackQuery, bot: Bot) -> None:
    async with Session() as s, s.begin():
        conv = await chat.conversation(s, "tg", str(callback.from_user.id), callback.from_user.full_name)
    await callback.message.answer(await chat.request_operator(bot, conv.id))
    await callback.answer()


@router.callback_query(F.data.startswith("rate:"))
async def rate(callback: CallbackQuery) -> None:
    _, mid, value = callback.data.split(":")
    async with Session() as s, s.begin():
        conv = await chat.conversation(s, "tg", str(callback.from_user.id))
    ok = await chat.rate(int(mid), int(value), conv.id)
    await callback.answer("Спасибо за оценку!" if ok else "Не получилось", show_alert=not ok)
    if ok and int(value) < 0 and chat.operators_enabled():
        await callback.message.answer(
            "Жаль, что не помог 😔 Позвать оператора?",
            reply_markup=InlineKeyboardBuilder()
            .button(text="🙋 Позвать оператора", callback_data="operator")
            .as_markup(),
        )


async def _reply(message: Message, bot: Bot, text: str) -> None:
    conv_id = await _conv_id(message)
    typing = asyncio.create_task(_typing(bot, message.chat.id))
    try:
        reply = await chat.on_user_message(bot, conv_id, text)
    finally:
        typing.cancel()
    if reply is None:
        return  # диалог у оператора: вопрос ушёл в его тему, ответит человек
    body = chat.to_html(reply.text, reply.sources)
    await message.answer(
        body[:TG_LIMIT],
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=answer_kb(reply.message_id, reply.answered),
    )


@router.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    await _reply(message, bot, message.text[:2000])


@router.message(F.chat.type == "private", F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    data = (await bot.download(message.voice)).read()
    try:
        text = await get_stt().transcribe(Attachment("audio/ogg", data))
    except Exception:
        await message.answer("Не получилось распознать голосовое — напишите, пожалуйста, текстом.")
        return
    await message.answer(f"🎤 <i>{html.escape(text)}</i>", parse_mode="HTML")
    await _reply(message, bot, text)


# ---------- Операторы: группа с темами ----------


def _in_topic(message: Message) -> bool:
    return (
        message.chat.id == settings.operator_chat_id
        and bool(message.message_thread_id)
        and not message.from_user.is_bot
    )


@router.message(Command("close"), F.func(_in_topic))
async def close_topic(message: Message, bot: Bot) -> None:
    conv = await chat.close(bot, message.message_thread_id)
    await message.reply("✅ Диалог возвращён боту" if conv else "Это не тема клиента")


@router.message(F.text, F.func(_in_topic))
async def operator_says(message: Message, bot: Bot) -> None:
    result = await chat.from_operator(bot, message.message_thread_id, message.text)
    if result is None:
        return
    _, row_id = result
    kb = InlineKeyboardBuilder().button(text="📚 В FAQ", callback_data=f"faq:{row_id}").as_markup()
    await message.reply("✓ отправлено клиенту", reply_markup=kb, disable_notification=True)


@router.callback_query(F.data.startswith("faq:"), F.message.chat.id == settings.operator_chat_id)
async def to_faq(callback: CallbackQuery) -> None:
    """Ответ оператора → FAQ: со следующим таким вопросом бот справится сам."""
    async with Session() as s, s.begin():
        row = await s.get(Row, int(callback.data[4:]))
        question = await chat.last_question(s, row.conversation_id, row.id) if row else ""
        if not row or not question:
            await callback.answer("Не нашёл вопрос клиента", show_alert=True)
            return
        await ingest.add_faq(s, question, row.text)
    await callback.message.edit_text(f"📚 Добавлено в FAQ:\n<b>{html.escape(question[:200])}</b>", parse_mode="HTML")
    await callback.answer("Бот теперь знает ответ")
