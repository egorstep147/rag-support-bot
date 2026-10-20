# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

ИИ-поддержка для бизнеса: бот в Telegram + виджет на сайт отвечают по базе знаний компании (RAG),
сложное передают операторам в темы группы. Компания — папка `companies/<name>/`: `config.yaml`
(тон, источники, веса поиска), `knowledge/`, `faq.yaml`, `evals.yaml` (контрольные вопросы). Выбор — `COMPANY`.

## Tech stack

Python 3.13, uv, ruff, pytest · aiogram 3 + FastAPI в одном процессе · SQLAlchemy 2 async + Alembic ·
PostgreSQL + pgvector (HNSW) + полнотекстовый tsvector('russian') · адаптеры моделей `llm/` (Gemini,
Claude, OpenAI-совместимые, в т.ч. Ollama) · виджет — чистый JS в `web/`.

## Commands

- `docker compose up -d` — PostgreSQL с pgvector (порт 5435)
- `uv run support migrate | sync | eval [--no-judge] | run`
- `uv run pytest` — база support_test (127.0.0.1:5435) или TEST_DATABASE_URL; модель и эмбеддинги поддельные
- `uv run ruff check src tests && uv run ruff format src tests`
- Длина вектора `EMBED_DIM` задаётся до первой миграции (миграция читает её из настроек)

## Architecture

- `rag/loaders.py` → RawDoc; `rag/ingest.py` — синхронизация по sha256 (неизменённое не пересчитывается);
  `rag/chunking.py` — куски по заголовкам с путём «Раздел › Подраздел»
- `rag/retrieve.py` — vector / fts / hybrid (взвешенный RRF, вес `company.fts_weight` подобран по evals)
- `rag/answer.py` — переписывание вопроса по истории → поиск → ответ только по фрагментам со ссылками [n];
  `НЕТ_ОТВЕТА` → честный отказ; ссылки перенумеровываются по списку источников
- `evals.py` — hit@k/MRR по режимам + судья-модель (correct/faithful) + проверка отказов; отчёт в `evals/<company>.md`
- `chat.py` — общий для Telegram и сайта: диалоги, режим ai/operator, темы в группе операторов, «В FAQ»
- `api/web.py` — API виджета (сессия-токен, лимиты, safe_html против javascript:-ссылок), `web/` — виджет и чат

## Rules

- Все ответы и комментарии в коде на русском
- Код компактный, без лишних абстракций
- Всё асинхронно; HTTP — `httpx.AsyncClient`
- Секреты только через `.env`
- Зависимости только через `uv add`
- Любое изменение поиска/подсказки — с прогоном `support eval` до и после
- Перед изменением нескольких файлов — сначала показать план
- После изменений объяснять, что и зачем сделано (я учусь)
