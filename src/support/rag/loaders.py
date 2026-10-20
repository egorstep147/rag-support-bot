"""Загрузчики источников → RawDoc (текст в Markdown-подобном виде). Каждый источник отдаёт
полный список документов: чего нет в списке — удалено, у чего изменился текст — пересчитается."""

import asyncio
import html
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from docx import Document as Docx
from pypdf import PdfReader

from support.company import SourceSpec, company
from support.config import settings

log = logging.getLogger(__name__)
TIMEOUT = httpx.Timeout(20, connect=10)
TEXT_EXT = {".md", ".txt"}


@dataclass
class RawDoc:
    uri: str
    title: str
    text: str
    url: str = ""


# ---------- Файлы ----------


def _docx_to_md(data: bytes) -> str:
    """Заголовки Word («Heading 1», «Заголовок 2») → «#», «##» — чтобы резать по разделам."""
    lines = []
    for p in Docx(io.BytesIO(data)).paragraphs:
        m = re.search(r"(?:Heading|Заголовок)\s*(\d)", p.style.name or "")
        lines.append(("#" * int(m.group(1)) + " " if m else "") + p.text)
    return "\n".join(lines)


def file_to_doc(name: str, data: bytes, uri: str | None = None) -> RawDoc:
    ext = Path(name).suffix.lower()
    if ext in TEXT_EXT:
        text = data.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    elif ext == ".docx":
        text = _docx_to_md(data)
    elif ext in (".html", ".htm"):
        text = html_to_text(data.decode("utf-8", errors="replace"))
    else:
        raise ValueError(f"Формат {ext} не поддерживается: PDF, DOCX, MD, TXT, HTML")
    first = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), name)
    return RawDoc(uri=uri or name, title=first[:120] if text.lstrip().startswith("#") else Path(name).stem, text=text)


async def load_files(spec: SourceSpec) -> list[RawDoc]:
    folder = company().folder / spec.path

    def read():
        return [
            file_to_doc(p.name, p.read_bytes(), uri=p.relative_to(folder).as_posix())
            for p in sorted(folder.rglob("*"))
            if p.is_file() and not p.name.startswith(".")
        ]

    return await asyncio.to_thread(read)


# ---------- FAQ ----------


def faq_doc(question: str, answer: str, url: str = "") -> RawDoc:
    q = question.strip()
    return RawDoc(uri=f"faq:{q.lower()}", title=q, text=f"Вопрос: {q}\nОтвет: {answer.strip()}", url=url)


async def load_faq(spec: SourceSpec) -> list[RawDoc]:
    items = yaml.safe_load((company().folder / spec.path).read_text(encoding="utf-8")) or []
    return [faq_doc(x["q"], x["a"], x.get("url", "")) for x in items]


# ---------- Сайт ----------


def html_to_text(page: str) -> str:
    """HTML → текст с заголовками «#». Меню, шапку и подвал выкидываем: в базе знаний они только шумят."""
    page = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|header|footer|form)\b.*?</\1>", " ", page)
    page = re.sub(
        r"(?is)<h([1-4])[^>]*>(.*?)</h\1>", lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n", page
    )
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|section|article)>", "\n", page)
    page = re.sub(r"(?i)<li[^>]*>", "\n- ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def load_site(spec: SourceSpec) -> list[RawDoc]:
    """sitemap.xml — берём адреса из него; иначе обходим ссылки того же домена (не больше max_pages)."""
    start = urlparse(spec.url)
    docs, seen = [], set()
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": "SupportBot/1.0 (+knowledge sync)"}
    ) as c:
        if spec.url.endswith(".xml"):
            queue = re.findall(r"<loc>\s*(.*?)\s*</loc>", (await c.get(spec.url)).text)
            crawl = False
        else:
            queue, crawl = [spec.url], True
        while queue and len(docs) < spec.max_pages:
            url = queue.pop(0).split("#")[0]
            if url in seen:
                continue
            seen.add(url)
            try:
                r = await c.get(url)
            except httpx.HTTPError as e:
                log.warning("Сайт: %s недоступен: %s", url, e)
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            title = re.search(r"(?is)<title>(.*?)</title>", r.text)
            text = html_to_text(r.text)
            if len(text) > 100:
                docs.append(
                    RawDoc(uri=url, url=url, text=text, title=html.unescape(title.group(1).strip()) if title else url)
                )
            if crawl:
                for href in re.findall(r'(?i)href="([^"#]+)"', r.text):
                    link = urljoin(url, href)
                    if (
                        urlparse(link).netloc == start.netloc
                        and link not in seen
                        and not re.search(r"\.(png|jpe?g|gif|svg|pdf|zip|css|js)$", link, re.I)
                    ):
                        queue.append(link)
            await asyncio.sleep(0.3)  # бережём чужой сайт
    return docs


# ---------- Google Docs (сервисный аккаунт) ----------

_google_token: tuple[str, float] = ("", 0.0)


async def _google(path: str, **params) -> httpx.Response:
    global _google_token
    from google.auth import crypt, jwt

    if _google_token[1] < time.time() + 60:
        info = json.loads(await asyncio.to_thread(Path(settings.google_credentials_file).read_text, encoding="utf-8"))
        now = int(time.time())
        assertion = jwt.encode(
            crypt.RSASigner.from_service_account_info(info),
            {
                "iss": info["client_email"],
                "scope": "https://www.googleapis.com/auth/drive.readonly",
                "aud": info["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
        )
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                info["token_uri"],
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion.decode()},
            )
            r.raise_for_status()
        _google_token = (r.json()["access_token"], now + r.json()["expires_in"])
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(
            "https://www.googleapis.com/drive/v3" + path,
            params=params,
            headers={"Authorization": f"Bearer {_google_token[0]}"},
        )
        r.raise_for_status()
        return r


async def load_gdoc(spec: SourceSpec) -> list[RawDoc]:
    """Google Документы из папок Drive, к которым открыт доступ сервисному аккаунту."""
    docs = []
    for folder in spec.ids:
        files = (
            await _google(
                "/files",
                q=f"'{folder}' in parents and mimeType='application/vnd.google-apps.document' and trashed=false",
                fields="files(id,name,webViewLink)",
            )
        ).json()["files"]
        for f in files:
            text = (await _google(f"/files/{f['id']}/export", mimeType="text/markdown")).text
            docs.append(RawDoc(uri=f"gdoc:{f['id']}", title=f["name"], text=text, url=f.get("webViewLink", "")))
    return docs


# ---------- Notion ----------


async def _notion(c: httpx.AsyncClient, path: str) -> dict:
    r = await c.get("https://api.notion.com/v1" + path)
    r.raise_for_status()
    return r.json()


def _rich(items: list) -> str:
    return "".join(x.get("plain_text", "") for x in items)


async def _notion_blocks(c: httpx.AsyncClient, block_id: str, depth: int = 0) -> list[str]:
    lines, cursor = [], ""
    while True:
        data = await _notion(
            c, f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        )
        for b in data["results"]:
            t, body = b["type"], b.get(b["type"], {})
            text = _rich(body.get("rich_text", []))
            prefix = {
                "heading_1": "# ",
                "heading_2": "## ",
                "heading_3": "### ",
                "bulleted_list_item": "- ",
                "numbered_list_item": "1. ",
                "to_do": "- [ ] ",
                "quote": "> ",
            }.get(t, "")
            if text:
                lines.append(prefix + text)
            if b.get("has_children") and t != "child_page" and depth < 3:
                lines += await _notion_blocks(c, b["id"], depth + 1)
        if not data.get("has_more"):
            return lines
        cursor = data["next_cursor"]


async def load_notion(spec: SourceSpec) -> list[RawDoc]:
    headers = {"Authorization": f"Bearer {settings.notion_token.get_secret_value()}", "Notion-Version": "2022-06-28"}
    docs = []
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as c:
        for page_id in spec.ids:
            page = await _notion(c, f"/pages/{page_id}")
            title = next((_rich(p["title"]) for p in page["properties"].values() if p["type"] == "title"), page_id)
            docs.append(
                RawDoc(
                    uri=f"notion:{page_id}",
                    title=title,
                    url=page.get("url", ""),
                    text="\n".join(await _notion_blocks(c, page_id)),
                )
            )
    return docs


LOADERS = {"files": load_files, "faq": load_faq, "site": load_site, "gdoc": load_gdoc, "notion": load_notion}
