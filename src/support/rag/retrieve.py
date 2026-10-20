"""Поиск по базе знаний. Два способа дополняют друг друга:
- векторный (по смыслу): «не грузится интернет» найдёт «нет подключения к сети»;
- полнотекстовый (по словам): точно ловит артикулы, названия тарифов, коды ошибок («ошибка 651»).
Гибрид объединяет оба списка методом Reciprocal Rank Fusion: score = Σ 1 / (60 + место в списке)."""

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select

from support.db import AsyncSession
from support.db.models import Chunk
from support.llm import get_embedder

Mode = Literal["vector", "fts", "hybrid"]
CANDIDATES = 20  # сколько берём из каждого способа перед объединением
RRF_K = 60  # константа RRF: сглаживает разницу между первыми местами


@dataclass
class Hit:
    chunk_id: int
    score: float
    heading: str
    text: str
    title: str
    url: str
    uri: str


def _hit(c: Chunk, score: float) -> Hit:
    return Hit(c.id, score, c.heading, c.text, c.document.title, c.document.url, c.document.uri)


def tsquery(text: str) -> str:
    """Слова через «|» (ИЛИ): вопрос клиента длинный, требовать все слова (И) — почти всегда ноль находок."""
    words = re.findall(r"[\wёЁ-]{2,}", text.lower())
    return " | ".join(dict.fromkeys(w.strip("-") for w in words if w.strip("-")))


async def _vector(s: AsyncSession, query: str, limit: int) -> list[tuple[Chunk, float]]:
    vec = (await get_embedder().embed([query], query=True))[0]
    dist = Chunk.embedding.cosine_distance(vec)
    rows = await s.execute(select(Chunk, dist).order_by(dist).limit(limit))
    return [(c, 1 - d) for c, d in rows.unique()]


async def _fts(s: AsyncSession, query: str, limit: int) -> list[tuple[Chunk, float]]:
    q = tsquery(query)
    if not q:
        return []
    tsq = func.to_tsquery("russian", q)
    rank = func.ts_rank_cd(Chunk.tsv, tsq)
    rows = await s.execute(select(Chunk, rank).where(Chunk.tsv.op("@@")(tsq)).order_by(rank.desc()).limit(limit))
    return [(c, float(r)) for c, r in rows.unique()]


async def search(s: AsyncSession, query: str, k: int = 6, mode: Mode = "hybrid") -> list[Hit]:
    if mode == "vector":
        return [_hit(c, sc) for c, sc in await _vector(s, query, k)]
    if mode == "fts":
        return [_hit(c, sc) for c, sc in await _fts(s, query, k)]
    fused: dict[int, tuple[Chunk, float]] = {}
    for results in (await _vector(s, query, CANDIDATES), await _fts(s, query, CANDIDATES)):
        for rank, (c, _) in enumerate(results):
            prev = fused.get(c.id, (c, 0.0))[1]
            fused[c.id] = (c, prev + 1 / (RRF_K + rank + 1))
    best = sorted(fused.values(), key=lambda x: x[1], reverse=True)[:k]
    return [_hit(c, sc) for c, sc in best]
