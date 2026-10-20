"""API виджета на сайте. Посетитель анонимный: при первом открытии получает секретный токен сессии
(лежит в localStorage его браузера) — по нему мы узнаём диалог. Ответы оператора виджет забирает опросом."""

import re
import secrets
import time
from collections import defaultdict, deque
from typing import Annotated

from aiogram import Bot
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from support import chat
from support.company import company
from support.db import Session
from support.db.models import Conversation
from support.db.models import Message as Row

router = APIRouter(prefix="/api/web")
LIMITS = {"session": (20, 600), "ip": (60, 3600)}  # сообщений за секунд: на сессию и на IP
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, kind: str) -> None:
    """Скользящее окно в памяти: ИИ-ответ стоит денег, а виджет открыт всему интернету."""
    limit, window = LIMITS[kind]
    q, now = _hits[f"{kind}:{key}"], time.monotonic()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Слишком много сообщений — подождите немного")
    q.append(now)


def safe_html(text: str) -> str:
    """Ответ модели → HTML для страницы. Разметку экранирует конвертер; ссылки оставляем только http(s):
    иначе подсказка из документа могла бы подсунуть javascript:-ссылку (XSS)."""
    return re.sub(r'href="(?!https?://)[^"]*"', 'href="#"', text)


async def session(x_session: Annotated[str, Header()] = "") -> Conversation:
    if len(x_session) < 20:
        raise HTTPException(401, "Нет сессии")
    async with Session() as s:
        conv = await s.scalar(
            select(Conversation).where(Conversation.channel == "web", Conversation.external_id == x_session)
        )
    if conv is None:
        raise HTTPException(401, "Сессия не найдена — обновите страницу")
    return conv


Conv = Annotated[Conversation, Depends(session)]


class SessionIn(BaseModel):
    name: str = Field("", max_length=60)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class RateIn(BaseModel):
    message_id: int
    value: int = Field(ge=-1, le=1)


def _bot(request: Request) -> Bot:
    return request.app.state.bot


def _row(r: Row) -> dict:
    text = chat.to_html(r.text, r.sources) if r.role == "assistant" else chat.html.escape(r.text)
    return {"id": r.id, "role": r.role, "html": safe_html(text), "answered": r.answered}


@router.get("/config")
async def config() -> dict:
    c = company()
    return {"title": c.widget_title, "accent": c.accent, "welcome": c.welcome, "operators": chat.operators_enabled()}


@router.post("/session")
async def new_session(body: SessionIn, request: Request) -> dict:
    rate_limit(request.client.host if request.client else "?", "ip")
    token = secrets.token_urlsafe(24)
    async with Session() as s, s.begin():
        await chat.conversation(s, "web", token, body.name.strip() or "Посетитель сайта")
    return {"token": token}


@router.post("/message")
async def message(body: MessageIn, conv: Conv, request: Request) -> dict:
    rate_limit(conv.external_id, "session")
    rate_limit(request.client.host if request.client else "?", "ip")
    reply = await chat.on_user_message(_bot(request), conv.id, body.text.strip())
    if reply is None:
        return {"queued": True}  # диалог у оператора — ответ придёт через /messages
    async with Session() as s:
        return {"message": _row(await s.get(Row, reply.message_id))}


@router.get("/messages")
async def messages(conv: Conv, after: int = 0) -> list[dict]:
    """История (after=0) или новые сообщения после id — так виджет получает ответы оператора."""
    async with Session() as s:
        rows = await s.scalars(
            select(Row)
            .where(Row.conversation_id == conv.id, Row.id > after, Row.role != "system")
            .order_by(Row.id)
            .limit(100)
        )
        return [_row(r) for r in rows]


@router.post("/operator")
async def operator(conv: Conv, request: Request) -> dict:
    return {"text": await chat.request_operator(_bot(request), conv.id)}


@router.post("/bot")
async def back_to_bot(conv: Conv, request: Request) -> dict:
    """Клиент на сайте сам возвращается к ИИ, не дожидаясь оператора."""
    return {"ok": bool(await chat.back_to_ai(_bot(request), conv.id, "client"))}


@router.get("/state")
async def state(conv: Conv) -> dict:
    return {"mode": conv.mode}


@router.post("/rate")
async def rate(body: RateIn, conv: Conv) -> dict:
    if body.value == 0 or not await chat.rate(body.message_id, body.value, conv.id):
        raise HTTPException(404, "Сообщение не найдено")
    return {"ok": True}
