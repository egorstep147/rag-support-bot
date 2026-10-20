from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Настройки из окружения и .env. Имена без учёта регистра: BOT_TOKEN → bot_token."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    database_url: str
    redis_url: str = ""
    company: str = "provider"  # папка в companies/: настройки, база знаний, FAQ, контрольные вопросы
    companies_dir: Path = ROOT / "companies"
    admin_ids: Annotated[set[int], NoDecode] = set()
    operator_chat_id: int = 0  # группа с темами для операторов (бот — админ с правом управлять темами)

    # --- Модели (как в ai-assistant-bot) ---
    llm_provider: Literal["gemini", "anthropic", "openai"] = "gemini"
    model_fast: str = "gemini-flash-latest"  # переписывание вопроса, судья в evals
    model_smart: str = "gemini-flash-latest"  # ответ клиенту
    embed_provider: Literal["gemini", "openai"] = "gemini"
    embed_model: str = "gemini-embedding-001"
    # Длина вектора. Задаётся до первой миграции: у Gemini — любая до 3072 (берём 768),
    # у bge-m3 в Ollama — 1024. Смена потом — новая миграция и пересчёт всех эмбеддингов
    embed_dim: int = 768
    stt_provider: Literal["gemini", "openai"] = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""

    # --- Источники ---
    google_credentials_file: str = ""  # сервисный аккаунт для Google Docs
    notion_token: SecretStr = SecretStr("")

    webhook_url: str = ""
    webhook_secret: SecretStr = SecretStr("")
    public_url: str = ""  # публичный адрес сервиса — для виджета на сайте
    widget_origins: Annotated[set[str], NoDecode] = set()  # сайты, где разрешён виджет; пусто — любые
    polling: bool = True
    host: str = "127.0.0.1"
    port: int = 8086

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        return {int(x) for x in v.split(",") if x.strip()} if isinstance(v, str) else v

    @field_validator("widget_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        return {x.strip().rstrip("/") for x in v.split(",") if x.strip()} if isinstance(v, str) else v

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        return v.replace("postgresql://", "postgresql+asyncpg://", 1) if v.startswith("postgresql://") else v


settings = Settings()
