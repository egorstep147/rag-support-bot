"""Синхронизация источника с базой: новые документы добавляем, изменённые пересчитываем,
исчезнувшие удаляем. Неизменённые (тот же sha256 текста) не трогаем — эмбеддинги стоят денег."""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from support.company import SourceSpec
from support.db import AsyncSession
from support.db.models import Chunk, Document, Source
from support.llm import get_embedder
from support.rag import chunking
from support.rag.loaders import LOADERS, RawDoc, faq_doc

log = logging.getLogger(__name__)
BATCH = 50  # кусков в одном запросе к эмбеддингам


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"новых {self.added}, обновлено {self.updated}, удалено {self.removed}, без изменений {self.unchanged}"
            f" · кусков {self.chunks}" + (f" · ошибки: {'; '.join(self.errors)}" if self.errors else "")
        )


def digest(doc: RawDoc) -> str:
    return hashlib.sha256(f"{doc.title}\n{doc.url}\n{doc.text}".encode()).hexdigest()


async def _source(s: AsyncSession, name: str, kind: str) -> Source:
    src = await s.scalar(select(Source).where(Source.name == name))
    if src is None:
        src = Source(name=name, kind=kind)
        s.add(src)
        await s.flush()
    return src


async def _embed_chunks(doc: RawDoc) -> list[Chunk]:
    pieces = chunking.split(doc.text)
    # В эмбеддинг кладём и название документа с заголовками: «Турбо — 100 Мбит» без контекста неясен
    texts = [f"{doc.title}\n{p.heading}\n{p.text}" for p in pieces]
    vectors = []
    for i in range(0, len(texts), BATCH):
        vectors += await get_embedder().embed(texts[i : i + BATCH])
    return [
        Chunk(idx=i, heading=p.heading, text=p.text, embedding=v)
        for i, (p, v) in enumerate(zip(pieces, vectors, strict=True))
    ]


async def upsert_docs(s: AsyncSession, src: Source, docs: list[RawDoc], *, full: bool = True) -> SyncResult:
    """full=True — список полный: документы источника, которых в нём нет, удаляются."""
    res = SyncResult()
    existing = {d.uri: d for d in await s.scalars(select(Document).where(Document.source_id == src.id))}
    for raw in docs:
        h = digest(raw)
        old = existing.pop(raw.uri, None)
        if old is not None and old.content_hash == h:
            res.unchanged += 1
            continue
        try:
            chunks = await _embed_chunks(raw)
        except Exception as e:
            log.exception("Не удалось обработать %s", raw.uri)
            res.errors.append(f"{raw.title}: {type(e).__name__}")
            continue
        if old is None:
            s.add(
                Document(
                    source_id=src.id, uri=raw.uri, title=raw.title[:300], url=raw.url, content_hash=h, chunks=chunks
                )
            )
            res.added += 1
        else:
            await s.refresh(old, ["chunks"])
            old.title, old.url, old.content_hash, old.chunks = raw.title[:300], raw.url, h, chunks
            res.updated += 1
        res.chunks += len(chunks)
    if full:
        for doc in existing.values():
            await s.delete(doc)
            res.removed += 1
    await s.flush()
    return res


async def sync(s: AsyncSession, spec: SourceSpec) -> SyncResult:
    src = await _source(s, spec.name, spec.kind)
    try:
        docs = await LOADERS[spec.kind](spec)
    except Exception as e:
        log.exception("Источник %s не загрузился", spec.name)
        src.status, src.error = "error", f"{type(e).__name__}: {e}"[:500]
        return SyncResult(errors=[src.error])
    res = await upsert_docs(s, src, docs)
    src.status, src.error, src.synced_at = (
        ("error" if res.errors else "ok"),
        "; ".join(res.errors)[:500],
        datetime.now(UTC),
    )
    return res


async def add_upload(s: AsyncSession, doc: RawDoc) -> SyncResult:
    """Файл, присланный админом боту: отдельный источник, документы не удаляются при синхронизации."""
    return await upsert_docs(s, await _source(s, "Загруженные файлы", "upload"), [doc], full=False)


async def add_faq(s: AsyncSession, question: str, answer: str) -> SyncResult:
    """Ответ оператора → FAQ: следующий клиент с таким вопросом получит ответ сразу от бота."""
    return await upsert_docs(s, await _source(s, "FAQ от операторов", "faq"), [faq_doc(question, answer)], full=False)
