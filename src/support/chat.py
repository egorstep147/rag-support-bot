"""Диалоги, общие для Telegram и виджета на сайте: история, ответ ИИ, передача оператору.
Канал отличается только доставкой: в Telegram бот пишет сам, виджет забирает новые сообщения запросом."""

import html
import logging
from dataclasses import dataclass

from aiogram import Bot
from chatgpt_md_converter import telegram_format
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from support.company import company
from support.config import settings
from support.db import AsyncSession, Session
from support.db.models import Conversation
from support.db.models import Message as Row
from support.llm import Message
from support.rag.answer import answer

log = logging.getLogger(__name__)
HISTORY = 8  # реплик диалога для переписывания вопроса и контекста ответа


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
    async with Session() as s:
        a = await answer(s, text, past)
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
        await bot.send_message(int(conv.external_id), f"👩‍💼 Оператор: {text}")
    return conv, row.id


async def close(bot: Bot, topic_id: int) -> Conversation | None:
    async with Session() as s, s.begin():
        conv = await s.scalar(select(Conversation).where(Conversation.topic_id == topic_id))
        if conv is None:
            return None
        conv.mode = "ai"
        s.add(Row(conversation_id=conv.id, role="system", text="Оператор завершил диалог"))
    if conv.channel == "tg":
        await bot.send_message(
            int(conv.external_id), "Оператор завершил диалог. Если будут вопросы — пишите, отвечу сразу 🙂"
        )
    return conv


async def last_question(s: AsyncSession, conv_id: int, before_id: int | None = None) -> str:
    q = select(Row.text).where(Row.conversation_id == conv_id, Row.role == "user")
    if before_id:
        q = q.where(Row.id < before_id)
    return await s.scalar(q.order_by(Row.id.desc()).limit(1)) or ""
