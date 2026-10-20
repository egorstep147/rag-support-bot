"""Оценка качества (evals): `support eval`. Без замеров «стало лучше» — это ощущение, а не факт.

Поиск (для каждого режима: vector / fts / hybrid):
  hit@1, hit@k — нужный документ первым / среди k найденных; MRR — среднее 1/место первого нужного.
Ответы (гибридный поиск + модель):
  correct  — судья-модель сверяет ответ с эталоном: 2 верно, 1 частично, 0 неверно;
  faithful — в ответе нет фактов, которых нет во фрагментах (галлюцинаций);
  refusal  — на вопрос не по теме / без ответа в базе бот честно говорит «не знаю»."""

import json
import re
import time
from dataclasses import dataclass, field

import yaml

from support.company import company
from support.config import settings
from support.db import AsyncSession
from support.llm import Message, get_llm
from support.rag.answer import answer
from support.rag.retrieve import search

MODES = ("vector", "fts", "hybrid")


@dataclass
class Case:
    q: str
    expect: list[str] = field(default_factory=list)
    answer: str = ""
    type: str = "answerable"


def load_cases() -> list[Case]:
    raw = yaml.safe_load((company().folder / "evals.yaml").read_text(encoding="utf-8"))
    return [Case(**x) for x in raw]


def first_hit(uris: list[str], expect: list[str]) -> int | None:
    """Место (с 1) первого найденного куска из ожидаемого документа."""
    return next((i for i, uri in enumerate(uris, 1) if any(e in uri for e in expect)), None)


async def retrieval(s: AsyncSession, cases: list[Case], k: int) -> dict[str, dict]:
    out = {}
    for mode in MODES:
        ranks = []
        for c in cases:
            if c.type == "answerable":
                ranks.append(first_hit([h.uri for h in await search(s, c.q, k, mode)], c.expect))
        n = len(ranks)
        out[mode] = {
            "hit@1": sum(r == 1 for r in ranks) / n,
            f"hit@{k}": sum(r is not None for r in ranks) / n,
            "mrr": sum(1 / r for r in ranks if r) / n,
            "misses": [
                c.q for c, r in zip([c for c in cases if c.type == "answerable"], ranks, strict=True) if r is None
            ],
        }
    return out


JUDGE = """Ты проверяешь ответ бота поддержки. Верни только JSON:
{"correct": 0|1|2, "faithful": true|false, "why": "кратко"}
correct: 2 — ответ по сути совпадает с эталоном; 1 — частично или неполно; 0 — неверно или не по вопросу.
faithful: false, если в ответе есть факты (цифры, сроки, условия), которых нет во фрагментах."""


async def judge(c: Case, text: str, fragments: str) -> dict:
    reply = await get_llm().complete(
        model=settings.model_fast,
        tools=[],
        system=JUDGE,
        messages=[
            Message(
                role="user", text=f"Вопрос: {c.q}\nЭталон: {c.answer}\nОтвет бота: {text}\n\nФрагменты:\n{fragments}"
            )
        ],
    )
    m = re.search(r"\{.*\}", reply.text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


async def generation(s: AsyncSession, cases: list[Case]) -> dict:
    rows, started = [], time.monotonic()
    for c in cases:
        a = await answer(s, c.q)
        if c.type == "refuse":
            rows.append({"q": c.q, "type": "refuse", "ok": not a.answered, "answer": a.text})
            continue
        fragments = "\n\n".join(f"[{i}] {h.text}" for i, h in enumerate(a.hits, 1))
        verdict = (
            await judge(c, a.text, fragments)
            if a.answered
            else {"correct": 0, "faithful": True, "why": "бот не нашёл ответа"}
        )
        rows.append(
            {
                "q": c.q,
                "type": "answerable",
                "correct": verdict.get("correct", 0),
                "faithful": verdict.get("faithful", False),
                "why": verdict.get("why", ""),
                "answer": a.text,
            }
        )
    ans = [r for r in rows if r["type"] == "answerable"]
    ref = [r for r in rows if r["type"] == "refuse"]
    return {
        "correct": sum(r["correct"] for r in ans) / (2 * len(ans)) if ans else 0,
        "fully_correct": sum(r["correct"] == 2 for r in ans) / len(ans) if ans else 0,
        "faithful": sum(bool(r["faithful"]) for r in ans) / len(ans) if ans else 0,
        "refusal": sum(r["ok"] for r in ref) / len(ref) if ref else 1,
        "seconds_per_answer": (time.monotonic() - started) / len(rows),
        "rows": rows,
    }


def report(ret: dict, gen: dict | None, k: int) -> str:
    pct = lambda x: f"{x * 100:.0f}%"  # noqa: E731
    lines = [
        f"# Evals: {company().name}",
        "",
        f"Модель ответа: `{settings.llm_provider}/{settings.model_smart}`, эмбеддинги: `{settings.embed_model}`",
        "",
        "## Поиск",
        "",
        f"| Режим | hit@1 | hit@{k} | MRR |",
        "|---|---|---|---|",
    ]
    lines += [f"| {m} | {pct(r['hit@1'])} | {pct(r[f'hit@{k}'])} | {r['mrr']:.2f} |" for m, r in ret.items()]
    if gen:
        lines += [
            "",
            "## Ответы (гибридный поиск)",
            "",
            "| Верно (взвешенно) | Полностью верно | Без выдумок | Честный отказ |",
            "|---|---|---|---|",
            f"| {pct(gen['correct'])} | {pct(gen['fully_correct'])} | {pct(gen['faithful'])} | {pct(gen['refusal'])} |",
            "",
            f"Среднее время ответа: {gen['seconds_per_answer']:.1f} с",
        ]
        bad = [
            r
            for r in gen["rows"]
            if (r["type"] == "refuse" and not r["ok"])
            or (r["type"] == "answerable" and (r["correct"] < 2 or not r["faithful"]))
        ]
        if bad:
            lines += ["", "## Ошибки", ""]
            lines += [
                f"- **{r['q']}** — {r.get('why') or 'должен был отказаться'}\n  > {r['answer'][:300]}" for r in bad
            ]
    misses = {m: r["misses"] for m, r in ret.items() if r["misses"]}
    if misses:
        lines += ["", "## Поиск не нашёл", ""] + [f"- {m}: {', '.join(q)}" for m, q in misses.items()]
    return "\n".join(lines) + "\n"
