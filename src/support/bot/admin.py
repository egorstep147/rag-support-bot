"""Админка в личке: загрузка файлов в базу знаний, синхронизация источников, FAQ, статистика, пробелы базы."""

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import case, func, select

from support import chat
from support.company import company
from support.config import settings
from support.db import Session
from support.db.models import Document, Source
from support.db.models import Message as Row
from support.rag import ingest
from support.rag.loaders import file_to_doc

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id.in_(settings.admin_ids))
_tasks: set[asyncio.Task] = set()

HELP = """🛠 Админка базы знаний
• пришлите файл (PDF, DOCX, MD, TXT) — добавлю в базу
/sources — источники и их состояние
/sync — пересинхронизировать источники из конфига
/faq Вопрос | Ответ — добавить в FAQ
/stats — статистика за 30 дней
/gaps — вопросы, на которые база не ответила"""


@router.message(Command("admin"))
async def admin(message: Message) -> None:
    await message.answer(HELP)


@router.message(F.document)
async def upload(message: Message, bot: Bot) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > 20_000_000:
        await message.answer("Файл больше 20 МБ — Telegram не даёт боту его скачать.")
        return
    data = (await bot.download(doc)).read()
    try:
        raw = await asyncio.to_thread(file_to_doc, doc.file_name or "file", data, f"upload:{doc.file_name}")
    except ValueError as e:
        await message.answer(f"📄 {e}")
        return
    async with Session() as s, s.begin():
        res = await ingest.add_upload(s, raw)
    await message.answer(f"📚 «{html.escape(raw.title)}»: {res}", parse_mode="HTML")


@router.message(Command("sources"))
async def sources(message: Message) -> None:
    async with Session() as s:
        rows = (
            await s.execute(
                select(Source, func.count(Document.id)).outerjoin(Document).group_by(Source.id).order_by(Source.id)
            )
        ).all()
    icon = {"ok": "✅", "error": "⚠️", "new": "⏳"}
    lines = [
        f"{icon.get(src.status, '•')} {src.name} ({src.kind}): документов {n}"
        + (f", синхр. {src.synced_at:%d.%m %H:%M}" if src.synced_at else "")
        + (f"\n   {src.error[:200]}" if src.error else "")
        for src, n in rows
    ]
    await message.answer("\n".join(lines) or "Источников пока нет — /sync")


@router.message(Command("sync"))
async def sync(message: Message, bot: Bot) -> None:
    await message.answer("🔄 Синхронизирую источники, пришлю итог…")

    async def go():
        lines = []
        for spec in company().sources:
            async with Session() as s, s.begin():
                lines.append(f"• {spec.name}: {await ingest.sync(s, spec)}")
        await bot.send_message(message.chat.id, "Готово:\n" + "\n".join(lines))

    task = asyncio.create_task(go())  # сайт на 100 страниц синхронизируется минуты — не держим обработчик
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.message(Command("faq"))
async def faq(message: Message, command: CommandObject) -> None:
    q, sep, a = (command.args or "").partition("|")
    if not sep or not q.strip() or not a.strip():
        await message.answer("Формат: /faq Как оплатить картой? | В личном кабинете без комиссии.")
        return
    async with Session() as s, s.begin():
        await ingest.add_faq(s, q, a)
    await message.answer("📚 Добавлено в FAQ")


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    since = datetime.now(UTC) - timedelta(days=30)
    async with Session() as s:
        r = (
            await s.execute(
                select(
                    func.count().filter(Row.role == "user"),
                    func.count().filter(Row.role == "assistant"),
                    func.count().filter(Row.role == "assistant", Row.answered.is_(True)),
                    func.count().filter(Row.role == "system", Row.text == "Клиент позвал оператора"),
                    func.count().filter(Row.rating == 1),
                    func.count().filter(Row.rating == -1),
                    func.coalesce(func.sum(case((Row.role == "assistant", Row.tokens), else_=0)), 0),
                    func.count(func.distinct(Row.conversation_id)),
                ).where(Row.created_at >= since)
            )
        ).one()
    questions, answers, answered, handoffs, up, down, tokens, dialogs = r
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "—"  # noqa: E731
    await message.answer(
        f"📊 За 30 дней\nДиалогов: {dialogs}, вопросов: {questions}\n"
        f"ИИ ответил по базе: {answered} из {answers} ({pct(answered, answers)})\n"
        f"Позвали оператора: {handoffs} ({pct(handoffs, dialogs)} диалогов)\n"
        f"Оценки: 👍 {up} · 👎 {down} ({pct(up, up + down)} довольны)\nТокенов: {tokens:,}".replace(",", " ")
    )


@router.message(Command("gaps"))
async def gaps(message: Message) -> None:
    """Вопросы, на которые ИИ сказал «не знаю», — подсказка, что дописать в базу знаний."""
    async with Session() as s:
        misses = (
            await s.execute(
                select(Row.conversation_id, Row.id)
                .where(Row.role == "assistant", Row.answered.is_(False))
                .order_by(Row.id.desc())
                .limit(15)
            )
        ).all()
        rows = [q for conv_id, mid in misses if (q := await chat.last_question(s, conv_id, mid))]
    text = "\n".join(f"• {html.escape(t[:150])}" for t in rows)
    await message.answer(
        f"🕳 Без ответа в базе (последние):\n{text}" if rows else "Все вопросы нашли ответ 🎉", parse_mode="HTML"
    )
