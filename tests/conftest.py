"""Тесты: отдельная база (support_test или TEST_DATABASE_URL), компания «provider»,
поддельные модель и эмбеддинги — без сети и ключей. Настройки задаём до импорта support."""

import os
from pathlib import Path

import pytest


def _local_test_db() -> str:
    env = Path(__file__).parents[1] / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    pw = next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("POSTGRES_PASSWORD=")), "support")
    return f"postgresql://support:{pw}@127.0.0.1:5435/support_test"  # 127.0.0.1: на Windows localhost медленный


os.environ |= {
    "BOT_TOKEN": "123456:TEST-TOKEN",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or _local_test_db(),
    "COMPANY": "provider",
    "ADMIN_IDS": "1",
    "OPERATOR_CHAT_ID": "-1001",
    "POLLING": "false",
    "REDIS_URL": "",
    "PUBLIC_URL": "https://support.example",
    "WIDGET_ORIGINS": "",
}

from sqlalchemy import text  # noqa: E402

from support.company import company  # noqa: E402
from support.db import Session, engine  # noqa: E402
from support.db.models import Base  # noqa: E402
from support.rag import ingest  # noqa: E402
from tests.fakes import FakeEmbedder, FakeLLM  # noqa: E402

EMB = FakeEmbedder()


@pytest.fixture(scope="session", autouse=True)
def patch_embedder():
    mp = pytest.MonkeyPatch()
    for target in ("support.rag.ingest.get_embedder", "support.rag.retrieve.get_embedder"):
        mp.setattr(target, lambda: EMB)
    yield
    mp.undo()


@pytest.fixture(scope="session", autouse=True)
async def database(patch_embedder):
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    for spec in company().sources:  # база знаний провайдера «Волна»
        async with Session() as s, s.begin():
            await ingest.sync(s, spec)
    yield
    await engine.dispose()


@pytest.fixture
def llm(monkeypatch):
    """llm.script = [...] — что «ответит» модель."""
    fake = FakeLLM([])
    for target in ("support.rag.answer.get_llm", "support.evals.get_llm"):
        monkeypatch.setattr(target, lambda: fake)
    return fake
