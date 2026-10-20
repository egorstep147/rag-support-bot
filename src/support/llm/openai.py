"""OpenAI и всё, что говорит на его языке: OpenRouter, DeepSeek, локальная Ollama (OPENAI_BASE_URL)."""

import base64
import io
import json
from collections.abc import AsyncIterator

import openai

from support.llm.base import LLM, Attachment, Embedder, Message, Reply, ToolCall, ToolSpec, Usage


def _messages(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "assistant":
            msg: dict = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)},
                    }
                    for c in m.tool_calls
                ]
            out.append(msg)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
        else:
            parts: list[dict] = [
                {"type": "image_url", "image_url": {"url": f"data:{a.mime};base64,{base64.b64encode(a.data).decode()}"}}
                for a in m.attachments
                if a.mime.startswith("image/")
            ]
            out.append({"role": "user", "content": [*parts, {"type": "text", "text": m.text or "(без текста)"}]})
    return out


class OpenAILLM(LLM):
    def __init__(self, api_key: str, base_url: str = ""):
        self.client = openai.AsyncOpenAI(api_key=api_key or "none", base_url=base_url or None)

    async def _stream(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> AsyncIterator[str | Reply]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=_messages(system, messages),
            stream=True,
            stream_options={"include_usage": True},
            tools=[
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            or openai.omit,
        )
        text, calls, usage = [], {}, Usage()
        async for chunk in stream:
            if chunk.usage:
                usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text.append(delta.content)
                yield delta.content
            # Вызов инструмента приходит кусками: имя и аргументы склеиваем по номеру вызова
            for tc in delta.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""
        tool_calls = [ToolCall(c["id"], c["name"], json.loads(c["args"] or "{}")) for c in calls.values()]
        yield Reply("".join(text), tool_calls, usage)

    async def transcribe(self, audio: Attachment) -> str:
        f = io.BytesIO(audio.data)
        f.name = "voice.ogg"
        r = await self.client.audio.transcriptions.create(model="whisper-1", file=f)
        return r.text.strip()


class OpenAIEmbedder(Embedder):
    def __init__(self, api_key: str, model: str, dim: int, base_url: str = ""):
        self.client = openai.AsyncOpenAI(api_key=api_key or "none", base_url=base_url or None)
        self.model, self.dim, self.base_url = model, dim, base_url

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        # dimensions понимает только OpenAI; Ollama и другие совместимые API отдают вектор своей длины
        extra = {"dimensions": self.dim} if not self.base_url else {}
        r = await self.client.embeddings.create(model=self.model, input=texts, **extra)
        return [d.embedding for d in r.data]
