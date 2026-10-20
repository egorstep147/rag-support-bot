"""Выбор провайдера по настройкам. Остальной код зовёт get_llm() / get_embedder() / get_stt()
и не знает, Gemini это, Claude или локальная модель."""

from functools import cache

from support.config import settings
from support.db.models import EMBED_DIM
from support.llm.base import LLM, Attachment, Embedder, LLMError, Message, Reply, ToolCall, ToolSpec, Usage

__all__ = [
    "LLM",
    "Attachment",
    "Embedder",
    "LLMError",
    "Message",
    "Reply",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "get_llm",
    "get_embedder",
    "get_stt",
]


def _make(provider: str) -> LLM:
    if provider == "gemini":
        from support.llm.gemini import GeminiLLM

        return GeminiLLM(settings.gemini_api_key.get_secret_value())
    if provider == "anthropic":
        from support.llm.anthropic import AnthropicLLM

        return AnthropicLLM(settings.anthropic_api_key.get_secret_value())
    from support.llm.openai import OpenAILLM

    return OpenAILLM(settings.openai_api_key.get_secret_value(), settings.openai_base_url)


@cache
def get_llm() -> LLM:
    return _make(settings.llm_provider)


@cache
def get_stt() -> LLM:
    """Кто расшифровывает голосовые: Claude не принимает аудио, поэтому это отдельная настройка."""
    return _make(settings.stt_provider)


@cache
def get_embedder() -> Embedder:
    if settings.embed_provider == "gemini":
        from support.llm.gemini import GeminiEmbedder

        return GeminiEmbedder(settings.gemini_api_key.get_secret_value(), settings.embed_model, EMBED_DIM)
    from support.llm.openai import OpenAIEmbedder

    return OpenAIEmbedder(
        settings.openai_api_key.get_secret_value(), settings.embed_model, EMBED_DIM, settings.openai_base_url
    )
