"""Конфиг компании: companies/<name>/config.yaml — кто мы, как говорим, откуда брать знания."""

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from support.config import settings


class SourceSpec(BaseModel):
    """Источник знаний. files/faq — пути внутри папки компании; site — адрес; gdoc — id папки Drive;
    notion — id страниц."""

    kind: Literal["files", "faq", "site", "gdoc", "notion"]
    name: str
    path: str = ""  # files: папка, faq: yaml-файл
    url: str = ""  # site: стартовая страница или sitemap.xml
    ids: list[str] = []  # gdoc: папки, notion: страницы
    max_pages: int = 50


class Company(BaseModel):
    name: str
    product: str  # чем занимается — модель отвечает только в этих рамках
    tone: str = "вежливо, кратко, по делу, на «вы»"
    welcome: str
    hours: str = ""  # когда работают операторы
    contacts: str = ""
    sources: list[SourceSpec]
    top_k: int = 6  # сколько кусков базы знаний давать модели
    min_score: float = 0.0  # отсечка по похожести (0 — без отсечки)
    fts_weight: float = 0.3  # вес поиска по словам в гибриде (вектор — 1.0); подбирается по `support eval`
    widget_title: str = "Поддержка"
    accent: str = "#2481cc"

    @property
    def folder(self) -> Path:
        return settings.companies_dir / settings.company


@cache
def company() -> Company:
    path = settings.companies_dir / settings.company / "config.yaml"
    return Company.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
