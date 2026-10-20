import uuid
from collections.abc import AsyncIterator

import httpx
from google import genai
from google.genai import types

from support.config import settings
from support.llm.base import LLM, Attachment, Embedder, Message, Reply, ToolCall, ToolSpec, Usage


def _contents(messages: list[Message]) -> list[types.Content]:
    out: list[types.Content] = []
    for m in messages:
        if m.role == "assistant" and m.raw is not None:
            out.append(m.raw)  # ответ модели как есть: в нём «подписи мыслей», без них Gemini вернёт ошибку
        elif m.role == "assistant":
            parts = [types.Part(text=m.text)] if m.text else []
            parts += [
                types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.args)) for c in m.tool_calls
            ]
            out.append(types.Content(role="model", parts=parts))
        elif m.role == "tool":
            part = types.Part.from_function_response(name=m.name, response={"result": m.text})
            part.function_response.id = m.tool_call_id
            # Ответы на несколько вызовов подряд — одним сообщением
            if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[0].function_response:
                out[-1].parts.append(part)
            else:
                out.append(types.Content(role="user", parts=[part]))
        else:
            parts = [types.Part.from_bytes(data=a.data, mime_type=a.mime) for a in m.attachments]
            out.append(types.Content(role="user", parts=[*parts, types.Part(text=m.text or "(без текста)")]))
    return out


def _client(api_key: str) -> genai.Client:
    """GEMINI_BIND — с какого адреса ходить к Gemini. Google решает «регион» по IP: на нашем VPS
    API пускает только с IPv6, а без привязки клиент выбирает IPv4 или IPv6 как придётся."""
    if not settings.gemini_bind:
        return genai.Client(api_key=api_key)
    transport = httpx.AsyncHTTPTransport(local_address=settings.gemini_bind)
    return genai.Client(
        api_key=api_key, http_options=types.HttpOptions(httpx_async_client=httpx.AsyncClient(transport=transport))
    )


class GeminiLLM(LLM):
    def __init__(self, api_key: str):
        self.client = _client(api_key)

    async def _stream(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> AsyncIterator[str | Reply]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name, description=t.description, parameters_json_schema=t.parameters
                        )
                        for t in tools
                    ]
                )
            ]
            if tools
            else None,
            # Инструменты вызывает наш агентный цикл, а не SDK
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        text, calls, parts, usage = [], [], [], Usage()
        async for chunk in await self.client.aio.models.generate_content_stream(
            model=model, contents=_contents(messages), config=config
        ):
            if chunk.usage_metadata:
                u = chunk.usage_metadata
                usage = Usage(
                    u.prompt_token_count or 0, (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
                )
            for part in (
                (chunk.candidates[0].content.parts or []) if chunk.candidates and chunk.candidates[0].content else []
            ):
                parts.append(part)
                if part.function_call:
                    fc = part.function_call
                    calls.append(ToolCall(id=fc.id or uuid.uuid4().hex[:12], name=fc.name, args=dict(fc.args or {})))
                elif part.text and not part.thought:
                    text.append(part.text)
                    yield part.text
        yield Reply("".join(text), calls, usage, raw=types.Content(role="model", parts=parts))

    async def transcribe(self, audio: Attachment) -> str:
        from support.config import settings

        r = await self.client.aio.models.generate_content(
            model=settings.model_fast,
            contents=[
                types.Part.from_bytes(data=audio.data, mime_type=audio.mime),
                "Дословно расшифруй эту запись. Верни только текст, без пояснений.",
            ],
        )
        return (r.text or "").strip()


class GeminiEmbedder(Embedder):
    def __init__(self, api_key: str, model: str, dim: int):
        self.client, self.model, self.dim = _client(api_key), model, dim

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        r = await self.client.aio.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT", output_dimensionality=self.dim
            ),
        )
        return [e.values for e in r.embeddings]
