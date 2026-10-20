"""Подделки для тестов: модель со сценарием ответов, эмбеддинги по словам и Telegram без сети."""

import hashlib
import itertools
import re
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import Chat
from aiogram.types import Message as TgMessage

from support.llm import LLM, Attachment, Embedder, Reply, ToolCall, Usage


class FakeLLM(LLM):
    """Отвечает по сценарию: каждый элемент — текст ответа или список вызовов инструментов."""

    def __init__(self, script: list):
        self.script, self.calls = list(script), []

    async def _stream(self, *, model, system, messages, tools):
        self.calls.append(
            {"model": model, "system": system, "messages": list(messages), "tools": [t.name for t in tools]}
        )
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, list):  # вызовы инструментов: [("remember", {"fact": "..."}), ...]
            calls = [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(step)]
            yield Reply("", calls, Usage(100, 10))
            return
        for word in re.findall(r"\S+\s*", step):  # «потоковый» ответ по словам
            yield word
        yield Reply(step, [], Usage(100, 20))

    async def transcribe(self, audio: Attachment) -> str:
        return "напомни купить хлеб"


class FakeEmbedder(Embedder):
    """Мешок слов в 768 измерениях: тексты с общими словами получаются близкими."""

    async def embed(self, texts, *, query=False):
        out = []
        for t in texts:
            v = [0.0] * 768
            for w in re.findall(r"\w{3,}", t.lower()):
                v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 768] += 1.0
            v[0] += 0.01  # ненулевой вектор, иначе косинусное расстояние не определено
            out.append(v)
        return out


class FakeSession(BaseSession):
    """Сессия aiogram без сети: запоминает запросы к Telegram и возвращает правдоподобные ответы."""

    def __init__(self):
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.ids = itertools.count(1000)

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout=None):  # noqa: ASYNC109 — сигнатура aiogram
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat_id = getattr(method, "chat_id", None) or 1
            msg = TgMessage(
                message_id=getattr(method, "message_id", None) or next(self.ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
            return msg.as_(bot)
        if isinstance(method, EditMessageReplyMarkup):
            return True
        if method.__returning__ is bool:
            return True
        if method.__class__.__name__ == "CreateInvoiceLink":
            return "https://t.me/$invoice"
        if method.__class__.__name__ == "CreateForumTopic":
            from aiogram.types import ForumTopic

            return ForumTopic(message_thread_id=next(self.ids), name=method.name, icon_color=0)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage | EditMessageText)]
