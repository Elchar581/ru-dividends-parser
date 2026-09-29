# -*- coding: utf-8 -*-
"""Разбор HTML-таблиц с поиском колонок по названиям заголовков.

Ключевая идея живучести: мы НЕ опираемся на порядковые номера колонок,
классы и вёрстку. Ищем таблицу, в шапке которой есть нужные слова, и
работаем с колонками по именам. Если сайт переставит колонки, добавит
новую или переименует класс — парсер продолжит работать.
"""
from __future__ import annotations

import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup


def soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml не собрался — штатный парсер
        return BeautifulSoup(html, "html.parser")


def norm(text: str) -> str:
    """Нормализация заголовка: нижний регистр, только буквы и цифры."""
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return re.sub(r"[^\w\d ]+", "", text)


def cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("\xa0", " ").strip()


def iter_tables(html: str) -> Iterator[tuple[object, list[str], list[list[str]]]]:
    """Отдаёт (tag, заголовки, строки) для каждой таблицы документа."""
    for table in soup(html).find_all("table"):
        rows = [
            [cell_text(c) for c in tr.find_all(["td", "th"], recursive=False)]
            for tr in table.find_all("tr")
        ]
        rows = [r for r in rows if r]
        if not rows:
            continue
        header_idx = _find_header(rows)
        if header_idx is None:
            continue
        yield table, rows[header_idx], rows[header_idx + 1:]


def _find_header(rows: list[list[str]]) -> Optional[int]:
    """Шапка — первая строка, где >=2 непустых ячеек и нет чисел-значений.
    Смотрим первые пять строк: сайты любят вставлять заголовок-подпись сверху."""
    for i, row in enumerate(rows[:5]):
        filled = [c for c in row if c]
        if len(filled) < 2:
            continue
        numeric = sum(1 for c in filled if re.fullmatch(r"[\d\s.,%₽\-]+", c))
        if numeric <= len(filled) // 3:
            return i
    return None


class Columns:
    """Сопоставление «что нам нужно» -> «номер колонки в этой таблице»."""

    def __init__(self, header: list[str]) -> None:
        self.header = [norm(h) for h in header]

    def find(self, *patterns: str) -> Optional[int]:
        """Номер первой колонки, чей заголовок совпадает с одним из regex."""
        for pat in patterns:
            rx = re.compile(pat)
            for i, h in enumerate(self.header):
                if rx.search(h):
                    return i
        return None

    def get(self, row: list[str], index: Optional[int]) -> str:
        if index is None or index >= len(row):
            return ""
        return row[index]
