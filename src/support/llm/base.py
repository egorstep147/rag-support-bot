"""Общий язык для всех провайдеров ИИ. Агент работает только с этими типами,
а адаптеры (gemini.py, anthropic.py, openai.py) переводят их в формат конкретного API."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    mime: str  # image/jpeg, audio/ogg, application/pdf
    data: bytes


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)  # у assistant: какие инструменты вызвать
    tool_call_id: str = ""  # у tool: на какой вызов это ответ
    name: str = ""  # у tool: имя инструмента
    raw: Any = None  # исходный ответ провайдера (Gemini требует вернуть его как есть внутри одного хода)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema аргументов


@dataclass
class Usage:
    input: int = 0
    output: int = 0

    def __iadd__(self, other: "Usage") -> "Usage":
        self.input += other.input
        self.output += other.output
        return self


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None


class LLMError(RuntimeError):
    pass


class LLM(ABC):
    """Чат-модель. stream() отдаёт кусочки текста по мере генерации, последним элементом — Reply."""

    @abstractmethod
    def _stream(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> AsyncIterator[str | Reply]: ...

    async def stream(self, **kw) -> AsyncIterator[str | Reply]:
        # Повтор при сбоях сети и перегрузке (429/5xx) — но только пока ничего не отдали пользователю,
        # иначе текст задвоится
        for attempt in range(3):
            started = False
            try:
                async for item in self._stream(**kw):
                    started = True
                    yield item
                return
            except Exception as e:
                if started or attempt == 2 or not _retryable(e):
                    raise LLMError(str(e)) from e
                log.warning("Модель ответила ошибкой, повтор через %s с: %s", 2**attempt, e)
                await asyncio.sleep(2**attempt)

    async def complete(self, **kw) -> Reply:
        async for item in self.stream(**kw):
            if isinstance(item, Reply):
                return item
        raise LLMError("модель не вернула ответ")

    async def transcribe(self, audio: Attachment) -> str:
        raise LLMError("этот провайдер не умеет расшифровывать голос")


class Embedder(ABC):
    @abstractmethod
    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """query=True — вектор для поискового запроса, False — для сохраняемого текста."""


def _retryable(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(e, "code", None) or getattr(e, "status", None)
    return isinstance(e, (TimeoutError, ConnectionError)) or status in (429, 500, 502, 503, 504)
