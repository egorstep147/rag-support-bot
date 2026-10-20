import base64
from collections.abc import AsyncIterator

import anthropic

from support.llm.base import LLM, Message, Reply, ToolCall, ToolSpec, Usage


def _messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.role == "assistant":
            blocks = [{"type": "text", "text": m.text}] if m.text else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}
            # Результаты нескольких вызовов — одним сообщением пользователя, как требует API
            if out and out[-1]["role"] == "user" and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            blocks = []
            for a in m.attachments:
                kind = "document" if a.mime == "application/pdf" else "image"
                if kind == "image" and not a.mime.startswith("image/"):
                    continue  # аудио Claude не принимает — его заранее расшифровывает stt
                data = base64.b64encode(a.data).decode()
                blocks.append({"type": kind, "source": {"type": "base64", "media_type": a.mime, "data": data}})
            out.append({"role": "user", "content": [*blocks, {"type": "text", "text": m.text or "(без текста)"}]})
    return out


class AnthropicLLM(LLM):
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _stream(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> AsyncIterator[str | Reply]:
        async with self.client.messages.stream(
            model=model,
            max_tokens=4096,
            system=system,
            messages=_messages(messages),
            tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        text = "".join(b.text for b in final.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, args=dict(b.input)) for b in final.content if b.type == "tool_use"]
        yield Reply(text, calls, Usage(final.usage.input_tokens, final.usage.output_tokens))
