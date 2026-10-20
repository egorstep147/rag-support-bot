"""Нарезка документа на куски. Режем по заголовкам Markdown: кусок = раздел со своим путём
заголовков («Тарифы › Турбо»), так он понятен сам по себе. Длинные разделы — по абзацам с перекрытием."""

import re
from dataclasses import dataclass

MAX_CHARS = 1200
MIN_CHARS = 200  # совсем короткие разделы склеиваем с соседними того же родителя
OVERLAP = 150


@dataclass
class Piece:
    heading: str
    text: str


def _sections(text: str) -> list[Piece]:
    stack: list[tuple[int, str]] = []
    out, buf = [], []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append(Piece(" › ".join(h for _, h in stack), body))
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", line)
        if m:
            flush()
            level = len(m.group(1))
            stack[:] = [(lv, h) for lv, h in stack if lv < level] + [(level, m.group(2).strip())]
        else:
            buf.append(line)
    flush()
    return out


def _split_long(piece: Piece) -> list[Piece]:
    if len(piece.text) <= MAX_CHARS:
        return [piece]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", piece.text) if p.strip()]
    parts, cur = [], ""
    for p in paragraphs:
        if cur and len(cur) + len(p) > MAX_CHARS:
            parts.append(cur)
            cur = cur[-OVERLAP:].split(" ", 1)[-1] + "\n\n" + p  # хвост предыдущего — для связности
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    parts.append(cur)
    # Абзац длиннее лимита (таблица, сплошной текст) режем грубо
    return [Piece(piece.heading, x[i : i + MAX_CHARS]) for x in parts for i in range(0, len(x), MAX_CHARS)]


def _parent(heading: str) -> str:
    return heading.rsplit(" › ", 1)[0] if " › " in heading else ""


def split(text: str) -> list[Piece]:
    merged: list[Piece] = []
    for p in _sections(text):
        prev = merged[-1] if merged else None
        child = bool(prev) and p.heading.startswith(prev.heading)
        sibling = bool(prev) and _parent(prev.heading) == _parent(p.heading) != ""
        if prev and len(prev.text) < MIN_CHARS and (child or sibling):
            if sibling and not child:  # «Оплата › Картой» + «Оплата › По СБП» → раздел «Оплата»
                prev.text = f"{prev.heading.rsplit(' › ', 1)[-1]}\n{prev.text}"
                prev.heading = _parent(p.heading)
            prev.text += f"\n\n{p.heading.rsplit(' › ', 1)[-1]}\n{p.text}"
        else:
            merged.append(p)
    return [x for p in merged for x in _split_long(p)]
