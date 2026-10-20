"""Диалоги, общие для Telegram и виджета на сайте: история, ответ ИИ, передача оператору.
Канал отличается только доставкой: в Telegram бот пишет сам, виджет забирает новые сообщения запросом."""

import html
import logging
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from chatgpt_md_converter import telegram_format
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from support.company import company
from support.config import settings
from support.db import AsyncSession, Session
from support.db.models import Conversation
from support.db.models import Message as Row
from support.llm import Message
from support.rag.answer import Answer, answer

log = logging.getLogger(__name__)
HISTORY = 8  # реплик диалога для переписывания вопроса и контекста ответа
BUSY = "Сейчас не получается ответить — сервис перегружен. Попробуйте чуть позже или позовите оператора."


@dataclass
class Reply:
    message_id: int
    text: str
    sources: list[dict]
    answered: bool


async def conversation(s: AsyncSession, channel: str, external_id: str, name: str = "") -> Conversation:
    stmt = insert(Conversation).values(channel=channel, external_id=external_id, name=name)
    await s.execute(stmt.on_conflict_do_nothing(index_elements=["channel", "external_id"]))
    conv = await s.scalar(
        select(Conversation).where(Conversation.channel == channel, Conversation.external_id == external_id)
    )
    if name and conv.name != name:
        conv.name = name
    return conv


async def history(s: AsyncSession, conv_id: int) -> list[Message]:
    rows = await s.scalars(
        select(Row)
        .where(Row.conversation_id == conv_id, Row.role.in_(("user", "assistant", "operator")))
        .order_by(Row.id.desc())
        .limit(HISTORY)
    )
    return [Message(role="user" if r.role == "user" else "assistant", text=r.text) for r in reversed(list(rows))]


def sources_html(sources: list[dict]) -> str:
    if not sources:
        return ""
    items = [
        f'{i}. <a href="{html.escape(x["url"])}">{html.escape(x["title"])}</a>'
        if x.get("url")
        else f"{i}. {html.escape(x['title'])}"
        for i, x in enumerate(sources, 1)
    ]
    return "\n\n<i>Источники:</i>\n" + "\n".join(items)


def to_html(text: str, sources: list[dict]) -> str:
    return telegram_format(text) + sources_html(sources)


# ---------- Клиент написал ----------


async def on_user_message(bot: Bot, conv_id: int, text: str) -> Reply | None:
    """Сохраняем вопрос; если диалог у оператора — пересылаем в его тему, иначе отвечает ИИ."""
    async with Session() as s, s.begin():
        conv = await s.get(Conversation, conv_id)
        past = await history(s, conv_id)
        s.add(Row(conversation_id=conv_id, role="user", text=text[:4000]))
    if conv.mode == "operator":
        await to_operator(bot, conv, f"👤 {html.escape(text)}")
        return None
    try:
        async with Session() as s:
            a = await answer(s, text, past)
    except Exception:  # лимит или сбой модели/эмбеддингов: клиент получает понятный ответ, а не ошибку
        log.exception("Диалог %s: модель недоступна", conv_id)
        a = Answer(BUSY, False)
    async with Session() as s, s.begin():
        row = Row(
            conversation_id=conv_id,
            role="assistant",
            text=a.text,
            sources=a.sources,
            answered=a.answered,
            tokens=a.usage.input + a.usage.output,
        )
        s.add(row)
        await s.flush()
    log.info(
        "Диалог %s: %s → %s, источников %s",
        conv_id,
        "ответ" if a.answered else "НЕТ ответа",
        a.question[:80],
        len(a.sources),
    )
    return Reply(row.id, a.text, a.sources, a.answered)


async def rate(message_id: int, value: int, conv_id: int | None = None) -> bool:
    async with Session() as s, s.begin():
        row = await s.get(Row, message_id)
        if row is None or row.role != "assistant" or (conv_id and row.conversation_id != conv_id):
            return False
        row.rating = value
    return True


# ---------- Оператор ----------


def operators_enabled() -> bool:
    return bool(settings.operator_chat_id)


async def to_operator(bot: Bot, conv: Conversation, text: str) -> None:
    """Сообщение в тему клиента в группе операторов (тему создаём при первой передаче)."""
    if not operators_enabled():
        return
    if conv.topic_id is None:
        icon = "🌐" if conv.channel == "web" else "✈️"
        topic = await bot.create_forum_topic(settings.operator_chat_id, f"{icon} {conv.name or 'Клиент'} · #{conv.id}")
        async with Session() as s, s.begin():
            (await s.get(Conversation, conv.id)).topic_id = topic.message_thread_id
        conv.topic_id = topic.message_thread_id
    await bot.send_message(
        settings.operator_chat_id,
        text,
        message_thread_id=conv.topic_id,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def request_operator(bot: Bot, conv_id: int) -> str:
    """Клиент просит человека: переключаем диалог, в тему уходит последняя переписка."""
    if not operators_enabled():
        return f"Сейчас операторы недоступны. Свяжитесь с нами: {company().contacts}"
    async with Session() as s, s.begin():
        conv = await s.get(Conversation, conv_id)
        already = conv.mode == "operator"
        conv.mode = "operator"
        past = await history(s, conv_id)
        s.add(Row(conversation_id=conv_id, role="system", text="Клиент позвал оператора"))
    if already:
        return "Оператор уже подключён — просто напишите вопрос."
    transcript = "\n".join(f"{'👤' if m.role == 'user' else '🤖'} {html.escape(m.text[:600])}" for m in past)
    await to_operator(
        bot,
        conv,
        f"🆘 <b>Клиент зовёт оператора</b>\n\n{transcript or 'Переписки ещё нет'}\n\n"
        "<i>Ответьте в этой теме — сообщение уйдёт клиенту. /close — вернуть клиента боту.</i>",
    )
    return (
        f"Передал ваш вопрос оператору — ответим здесь же. Время работы: {company().hours}."
        if company().hours
        else "Передал ваш вопрос оператору — ответим здесь же."
    )


async def from_operator(bot: Bot, topic_id: int, text: str) -> tuple[Conversation, int] | None:
    """Оператор написал в тему → сохраняем и доставляем клиенту (в Telegram — сразу, на сайт — при опросе)."""
    async with Session() as s, s.begin():
        conv = await s.scalar(select(Conversation).where(Conversation.topic_id == topic_id))
        if conv is None:
            return None
        row = Row(conversation_id=conv.id, role="operator", text=text[:4000])
        s.add(row)
        await s.flush()
    if conv.channel == "tg":
        await bot.send_message(int(conv.external_id), f"👩‍💼 Оператор: {text}", reply_markup=back_kb())
    return conv, row.id


def back_kb() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="🤖 Вернуться к боту", callback_data="bot")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# Кто вернул диалог ИИ → (запись в теме операторов, сообщение клиенту)
BACK = {
    "client": ("🤖 Клиент вернулся к боту", "Снова отвечаю я, ИИ-помощник 🙂 Оператора можно позвать в любой момент."),
    "operator": ("", "Оператор завершил диалог. Если будут вопросы — пишите, отвечу сразу 🙂"),
    "timeout": (
        "⏱ Оператор не ответил за {m} мин — клиент возвращён боту",
        "Оператор пока не ответил, поэтому снова отвечаю я, ИИ-помощник. Оператора можно позвать ещё раз.",
    ),
}


async def back_to_ai(bot: Bot, conv_id: int, reason: str) -> Conversation | None:
    """Диалог снова у ИИ: клиент сам, оператор (/close) или оператор молчит дольше operator_timeout.
    None — диалог и так у ИИ."""
    topic_note, client_note = BACK[reason]
    async with Session() as s, s.begin():
        conv = await s.get(Conversation, conv_id)
        if conv is None or conv.mode != "operator":
            return None
        conv.mode = "ai"
        s.add(Row(conversation_id=conv_id, role="system", text=f"Диалог снова у ИИ: {reason}"))
        s.add(Row(conversation_id=conv_id, role="notice", text=client_note))  # виджет покажет при опросе
    if conv.channel == "tg":
        await bot.send_message(int(conv.external_id), client_note)
    if topic_note and conv.topic_id:
        await to_operator(bot, conv, topic_note.format(m=company().operator_timeout))
    return conv


async def close(bot: Bot, topic_id: int) -> Conversation | None:
    async with Session() as s:
        conv_id = await s.scalar(select(Conversation.id).where(Conversation.topic_id == topic_id))
    return await back_to_ai(bot, conv_id, "operator") if conv_id else None


async def expire_operators(bot: Bot) -> int:
    """Оператор молчит дольше operator_timeout минут (с передачи или своего последнего ответа) — диалог снова у ИИ,
    чтобы клиент не остался без ответа, если оператор занят или забыл /close."""
    minutes = company().operator_timeout
    if not minutes:
        return 0
    async with Session() as s:
        last = (
            select(Row.conversation_id, func.max(Row.created_at).label("at"))
            .where(Row.role.in_(("operator", "system")))
            .group_by(Row.conversation_id)
            .subquery()
        )
        ids = list(
            await s.scalars(
                select(Conversation.id)
                .join(last, last.c.conversation_id == Conversation.id)
                .where(Conversation.mode == "operator", last.c.at < func.now() - timedelta(minutes=minutes))
            )
        )
    for conv_id in ids:
        await back_to_ai(bot, conv_id, "timeout")
    return len(ids)


async def last_question(s: AsyncSession, conv_id: int, before_id: int | None = None) -> str:
    q = select(Row.text).where(Row.conversation_id == conv_id, Row.role == "user")
    if before_id:
        q = q.where(Row.id < before_id)
    return await s.scalar(q.order_by(Row.id.desc()).limit(1)) or ""
