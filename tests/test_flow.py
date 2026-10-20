"""Сквозные сценарии: виджет на сайте, клиент в Telegram, оператор в теме группы, админка.
Модель и Telegram поддельные, база настоящая."""

import itertools
from datetime import UTC, datetime

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import CreateForumTopic, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from support.api import web
from support.app import create_app
from support.bot import create_dispatcher
from support.config import settings
from support.db import Session
from support.db.models import Document as Doc
from tests.fakes import FakeSession

ids = itertools.count(1)
GROUP = settings.operator_chat_id


@pytest.fixture(scope="session")
def tg():
    session = FakeSession()
    return Bot("123456:TEST-TOKEN", session=session), session, create_dispatcher()


@pytest.fixture(scope="session")
async def client(tg):
    app = create_app(tg[0], tg[2])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def user(uid):
    return TgUser(id=uid, is_bot=False, first_name="Анна", last_name="К")


def msg(chat_id, uid, chat_type="private", **kw):
    return Message(
        message_id=next(ids), date=datetime.now(UTC), chat=Chat(id=chat_id, type=chat_type), from_user=user(uid), **kw
    )


async def feed(tg, **update):
    bot, session, dp = tg
    session.requests.clear()
    await dp.feed_update(bot, Update(update_id=next(ids), **update))
    return session


def sent(session, chat_id=None):
    return [m for m in session.requests if isinstance(m, SendMessage) and (chat_id is None or m.chat_id == chat_id)]


async def new_web_session(client) -> dict:
    token = (await client.post("/api/web/session", json={"name": "Сайт"})).json()["token"]
    return {"X-Session": token}


def test_safe_html_blocks_javascript_links():
    assert (
        web.safe_html('<a href="javascript:alert(1)">x</a> <a href="https://ok.ru">y</a>')
        == '<a href="#">x</a> <a href="https://ok.ru">y</a>'
    )


async def test_widget_files_served(client):
    assert "currentScript" in (await client.get("/widget.js")).text
    page = await client.get("/chat")
    assert page.headers["content-security-policy"] == "frame-ancestors *"
    assert (await client.get("/api/web/config")).json()["operators"] is True


async def test_web_chat_answer_and_rating(client, llm):
    h = await new_web_session(client)
    llm.script = ["Тариф «Турбо» стоит 790 ₽ в месяц [1]."]
    r = (await client.post("/api/web/message", headers=h, json={"text": "Сколько стоит Турбо?"})).json()["message"]
    assert r["role"] == "assistant" and r["answered"] and "Источники" in r["html"]
    assert (await client.post("/api/web/rate", headers=h, json={"message_id": r["id"], "value": 1})).json()["ok"]
    other = await new_web_session(client)  # чужую оценку не поставить
    assert (
        await client.post("/api/web/rate", headers=other, json={"message_id": r["id"], "value": -1})
    ).status_code == 404
    assert (
        await client.post("/api/web/message", headers={"X-Session": "x" * 30}, json={"text": "?"})
    ).status_code == 401


async def test_web_rate_limit(client, llm, monkeypatch):
    monkeypatch.setitem(web.LIMITS, "session", (2, 600))
    h = await new_web_session(client)
    llm.script = ["a [1]", "b [1]"]
    codes = [(await client.post("/api/web/message", headers=h, json={"text": "вопрос"})).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_operator_handoff_from_web(client, tg, llm):
    bot, session, dp = tg
    h = await new_web_session(client)
    llm.script = ["НЕТ_ОТВЕТА"]
    r = (
        await client.post("/api/web/message", headers=h, json={"text": "Можно ли продлить блокировку на 120 дней?"})
    ).json()
    assert r["message"]["answered"] is False
    session.requests.clear()
    assert "Передал" in (await client.post("/api/web/operator", headers=h)).json()["text"]
    topic = next(m for m in session.requests if isinstance(m, CreateForumTopic))
    assert topic.name.startswith("🌐 Сайт")
    note = sent(session, GROUP)[0]
    assert "Клиент зовёт оператора" in note.text and "120 дней" in note.text
    thread = note.message_thread_id

    # Пока диалог у оператора, ИИ молчит: вопрос уходит в тему
    calls = len(llm.calls)
    assert (await client.post("/api/web/message", headers=h, json={"text": "Жду ответа"})).json() == {"queued": True}
    assert len(llm.calls) == calls

    # Оператор отвечает в теме → сообщение у клиента на сайте + кнопка «В FAQ»
    answer = "Да, блокировку можно продлить до 120 дней по заявлению в офисе."
    s = await feed(tg, message=msg(GROUP, 77, "supergroup", text=answer, message_thread_id=thread))
    faq_button = sent(s, GROUP)[0].reply_markup.inline_keyboard[0][0].callback_data
    news = (await client.get("/api/web/messages", headers=h, params={"after": r["message"]["id"]})).json()
    assert [m["role"] for m in news if m["role"] != "user"] == ["operator"]

    cb = CallbackQuery(
        id="c",
        from_user=user(77),
        chat_instance="x",
        data=faq_button,
        message=msg(GROUP, 77, "supergroup", text="✓", message_thread_id=thread),
    )
    await feed(tg, callback_query=cb)
    async with Session() as s:
        faq = await s.scalar(
            select(Doc).where(Doc.uri.like("faq:жду ответа%") | Doc.uri.like("faq:можно ли продлить%"))
        )
    assert faq is not None and faq.source.name == "FAQ от операторов"

    s = await feed(tg, message=msg(GROUP, 77, "supergroup", text="/close", message_thread_id=thread))
    assert "возвращён боту" in sent(s, GROUP)[0].text


async def test_telegram_client_and_dislike(tg, llm):
    llm.script = ["Пароль от Wi-Fi написан на наклейке снизу роутера [1]."]
    s = await feed(tg, message=msg(500, 500, text="где пароль от вайфая"))
    reply = sent(s, 500)[-1]
    assert reply.parse_mode == "HTML" and "Источники" in reply.text
    rate_down = reply.reply_markup.inline_keyboard[0][1].callback_data
    cb = CallbackQuery(id="r", from_user=user(500), chat_instance="x", data=rate_down, message=msg(500, 500, text="…"))
    s = await feed(tg, callback_query=cb)
    assert "Позвать оператора?" in sent(s, 500)[0].text


async def test_admin_upload_stats_gaps(tg, llm, monkeypatch):
    async def fake_download(self, file, *a, **kw):
        import io

        return io.BytesIO("# Акция\nДо конца месяца подключение частного дома за 1500 ₽.".encode())

    monkeypatch.setattr(Bot, "download", fake_download)
    doc = Document(file_id="f", file_unique_id="u", file_name="promo.md", file_size=100)
    s = await feed(tg, message=msg(1, 1, document=doc))
    assert "новых 1" in sent(s, 1)[0].text
    llm.script = ["НЕТ_ОТВЕТА"]
    await feed(tg, message=msg(501, 501, text="Есть ли у вас тариф для дачи?"))
    s = await feed(tg, message=msg(1, 1, text="/gaps"))
    assert "тариф для дачи" in sent(s, 1)[0].text
    s = await feed(tg, message=msg(1, 1, text="/stats"))
    assert "Диалогов" in sent(s, 1)[0].text
    s = await feed(tg, message=msg(502, 502, text="/stats"))  # не админ — это вопрос боту, а не команда
    assert not any("Диалогов" in m.text for m in sent(s))


async def test_model_failure_gives_polite_reply(client, llm, monkeypatch):
    """Лимит/сбой модели — посетитель получает понятный ответ, а не 500."""
    from support.llm import LLMError
    from support.rag import answer as answer_mod

    async def boom(*a, **kw):
        raise LLMError("429 quota")

    monkeypatch.setattr(answer_mod, "search", boom)
    token = (await client.post("/api/web/session", json={})).json()["token"]
    r = await client.post("/api/web/message", json={"text": "Сколько стоит Турбо?"}, headers={"X-Session": token})
    assert r.status_code == 200 and "перегружен" in r.json()["message"]["html"]
