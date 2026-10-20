"""Точка входа: `support [run|migrate|sync|eval]`.
sync — загрузить источники знаний из companies/<COMPANY>/config.yaml
eval — замерить качество поиска и ответов, отчёт в evals/<COMPANY>.md (--no-judge — только поиск)"""

import argparse
import asyncio
import logging


def run() -> None:
    import uvicorn

    from support.app import create_app
    from support.config import settings

    uvicorn.run(create_app(), host=settings.host, port=settings.port, proxy_headers=True, log_level="info")


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from support.config import ROOT

    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def sync() -> None:
    from support.company import company
    from support.db import Session, engine
    from support.rag import ingest

    async def go() -> None:
        for spec in company().sources:
            async with Session() as s, s.begin():
                print(f"{spec.name}: {await ingest.sync(s, spec)}")
        await engine.dispose()

    asyncio.run(go())


def evaluate(judge: bool) -> None:
    from support import evals
    from support.company import company
    from support.config import ROOT, settings
    from support.db import Session, engine

    async def go() -> None:
        cases, k = evals.load_cases(), company().top_k
        async with Session() as s:
            ret = await evals.retrieval(s, cases, k)
            gen = await evals.generation(s, cases) if judge else None
        await engine.dispose()
        text = evals.report(ret, gen, k)
        path = ROOT / "evals" / f"{settings.company}.md"
        path.write_text(text, encoding="utf-8")
        print(text, f"\nОтчёт: {path}", sep="")

    asyncio.run(go())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="support", description="ИИ-поддержка по базе знаний")
    p.add_argument("command", nargs="?", default="run", choices=["run", "migrate", "sync", "eval"])
    p.add_argument("--no-judge", action="store_true", help="eval: только метрики поиска, без модели-судьи")
    args = p.parse_args()
    if args.command == "eval":
        evaluate(not args.no_judge)
    else:
        {"run": run, "migrate": migrate, "sync": sync}[args.command]()
