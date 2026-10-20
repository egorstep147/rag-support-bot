"""Приложение: бот (polling или webhook) + API и страница виджета + ежедневная синхронизация источников."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from support.api.web import router as web_router
from support.bot import COMMANDS, create_bot, create_dispatcher
from support.company import company
from support.config import settings
from support.db import Session, engine
from support.rag import ingest

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"
WEBHOOK_PATH = "/tg/webhook"
_tasks: set[asyncio.Task] = set()


async def daily_sync() -> None:
    """Сайт, Google Docs и Notion меняются без нас — раз в сутки забираем изменения (только изменённое)."""
    while True:
        await asyncio.sleep(24 * 3600)
        for spec in company().sources:
            if spec.kind in ("site", "gdoc", "notion"):
                try:
                    async with Session() as s, s.begin():
                        log.info("Синхронизация %s: %s", spec.name, await ingest.sync(s, spec))
                except Exception:
                    log.exception("Синхронизация %s не удалась", spec.name)


async def setup_telegram(bot: Bot, dp: Dispatcher) -> asyncio.Task | None:
    if not settings.webhook_url and not settings.polling:
        return None
    await bot.set_my_commands(COMMANDS)
    if settings.webhook_url:
        if not settings.webhook_secret.get_secret_value():
            raise RuntimeError("Для вебхука задайте WEBHOOK_SECRET")
        await bot.set_webhook(
            settings.webhook_url.rstrip("/") + WEBHOOK_PATH,
            secret_token=settings.webhook_secret.get_secret_value(),
            allowed_updates=dp.resolve_used_update_types(),
        )
        return None
    await bot.delete_webhook()
    return asyncio.create_task(dp.start_polling(bot, handle_signals=False))


def create_app(bot: Bot | None = None, dp: Dispatcher | None = None) -> FastAPI:
    bot, dp = bot or create_bot(), dp or create_dispatcher()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bot = bot
        tasks = [asyncio.create_task(daily_sync())]
        if polling := await setup_telegram(bot, dp):
            tasks.append(polling)
        log.info("Поддержка «%s» запущена", company().name)
        yield
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await bot.session.close()
        await engine.dispose()

    app = FastAPI(title=f"{company().name} — поддержка", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.bot = bot
    app.include_router(web_router)

    @app.get("/widget.js", include_in_schema=False)
    async def widget() -> FileResponse:
        return FileResponse(
            WEB / "widget.js", media_type="application/javascript", headers={"Cache-Control": "public, max-age=3600"}
        )

    @app.get("/chat", include_in_schema=False)
    async def chat_page() -> FileResponse:
        # frame-ancestors: окно чата можно встроить только на разрешённые сайты (если список задан)
        ancestors = " ".join(sorted(settings.widget_origins)) or "*"
        return FileResponse(WEB / "chat.html", headers={"Content-Security-Policy": f"frame-ancestors {ancestors}"})

    @app.post(WEBHOOK_PATH, include_in_schema=False)
    async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header("")) -> dict:
        if x_telegram_bot_api_secret_token != settings.webhook_secret.get_secret_value():
            raise HTTPException(403)
        task = asyncio.create_task(
            dp.feed_update(bot, Update.model_validate(await request.json(), context={"bot": bot}))
        )
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return {"ok": True}

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"ok": True, "company": settings.company}

    return app
