# -*- coding: utf-8 -*-
"""Модель дивидендного события и слияние данных из нескольких источников."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Статусы выплаты
STATUS_PAID = "Выплачен"
STATUS_UPCOMING = "Объявлен (предстоящий)"
STATUS_FORECAST = "Прогноз (не объявлен)"


def parse_date(value) -> Optional[dt.date]:
    """Разбирает дату в любом из форматов, которые встречаются у источников."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s or s.lower() in {"n/a", "-", "—", "нет", "не определена", "?"}:
        return None
    # выкидываем пометки вида "(прогноз)", "(оценка)"
    s = re.sub(r"\(.*?\)", " ", s).strip()

    # ISO (так отдаёт MOEX ISS) проверяем ПЕРВЫМ: иначе "2025-07-17"
    # ошибочно читается как 25.07.2017 по русскому шаблону.
    m = re.match(r"\s*(\d{4})[-./](\d{1,2})[-./](\d{1,2})\b", s)
    if m:
        y, mth, d = (int(x) for x in m.groups())
        try:
            return dt.date(y, mth, d)
        except ValueError:
            return None

    # Русский формат ДД.ММ.ГГГГ (и ДД.ММ.ГГ)
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b", s)
    if not m:
        m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})\b", s)
    if m:
        d, mth, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        try:
            return dt.date(y, mth, d)
        except ValueError:
            return None
    return None


def parse_number(value) -> Optional[float]:
    """'37,64 ₽' -> 37.64 ; '1 234,5' -> 1234.5 ; '13,6%' -> 13.6"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    s = s.replace("\xa0", " ").replace(" ", " ").replace("−", "-")
    s = re.sub(r"[^\d,.\-]", "", s.replace(" ", ""))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v


def is_forecast(*raw_values) -> bool:
    joined = " ".join(str(v) for v in raw_values if v)
    return bool(re.search(r"прогноз|оценк|ожида(ет|ем)|предвар", joined, re.I))


@dataclass
class DividendEvent:
    """Одна дивидендная выплата по одной бумаге."""

    ticker: str
    record_date: Optional[dt.date] = None       # дата закрытия реестра
    last_buy_date: Optional[dt.date] = None     # последний день покупки (T-1/T-2)
    announce_date: Optional[dt.date] = None     # дата появления информации
    period: str = ""                            # период выплаты (2024 год, 9 мес 2025 ...)
    dividend: Optional[float] = None            # размер, руб/акцию
    price_close: Optional[float] = None         # цена закрытия
    price_date: Optional[dt.date] = None        # к какой дате относится цена
    yield_pct: Optional[float] = None           # доходность, %
    currency: str = "RUB"
    status: str = ""
    sources: set = field(default_factory=set)
    notes: list = field(default_factory=list)

    # -------------------------------------------------------------- служебное
    @property
    def sort_key(self) -> dt.date:
        return self.record_date or self.last_buy_date or self.announce_date or dt.date.min

    def resolve_status(self, today: Optional[dt.date] = None) -> str:
        """Статус вычисляем от сегодняшней даты, а не хардкодим.
        Именно это позволяет одному и тому же коду работать и через два года."""
        today = today or dt.date.today()
        if self.status == STATUS_FORECAST:
            return self.status
        rd = self.record_date
        if rd is None:
            return self.status or STATUS_FORECAST
        return STATUS_PAID if rd < today else STATUS_UPCOMING

    def merge_from(self, other: "DividendEvent", prefer_other: bool = False) -> None:
        """Дополняет пустые поля данными другого источника.

        Если источники расходятся по размеру дивиденда или дате отсечки —
        не выбираем молча «правильный», а фиксируем расхождение в примечании,
        чтобы это было видно в отчёте.
        """
        if self.dividend and other.dividend:
            base = max(abs(self.dividend), abs(other.dividend))
            if base and abs(self.dividend - other.dividend) / base > 0.02:
                self.notes.append(
                    "расхождение источников по размеру: %s=%.4f, %s=%.4f"
                    % (
                        ",".join(sorted(self.sources)), self.dividend,
                        ",".join(sorted(other.sources)), other.dividend,
                    )
                )
        if self.record_date and other.record_date and self.record_date != other.record_date:
            self.notes.append(
                "расхождение по дате отсечки: %s и %s"
                % (self.record_date.strftime("%d.%m.%Y"), other.record_date.strftime("%d.%m.%Y"))
            )
        for fld in (
            "record_date", "last_buy_date", "announce_date", "period",
            "dividend", "price_close", "price_date", "yield_pct",
        ):
            mine = getattr(self, fld)
            theirs = getattr(other, fld)
            if theirs in (None, "") :
                continue
            if mine in (None, "") or prefer_other:
                setattr(self, fld, theirs)
        self.sources |= other.sources
        self.notes.extend(n for n in other.notes if n not in self.notes)
        # Выплата — прогноз, только если ВСЕ источники считают её прогнозом.
        # Пустой статус означает «источник не пометил строку как прогноз»,
        # то есть выплата у него значится объявленной; одного такого
        # источника достаточно, чтобы снять пометку прогноза.
        if self.status == STATUS_FORECAST and other.status != STATUS_FORECAST:
            self.status = ""


def merge_events(
    groups: Iterable[Iterable[DividendEvent]],
    date_tolerance_days: int = 5,
) -> list[DividendEvent]:
    """Сливает списки событий из разных источников.

    Ключ сопоставления — дата закрытия реестра с допуском (источники изредка
    расходятся на день-два, а по будущим выплатам дата может уточняться).
    Если даты реестра нет — пробуем сопоставить по периоду и размеру дивиденда.
    """
    merged: list[DividendEvent] = []

    def find_match(ev: DividendEvent) -> Optional[DividendEvent]:
        for cand in merged:
            if cand.ticker != ev.ticker:
                continue
            if ev.record_date and cand.record_date:
                if abs((cand.record_date - ev.record_date).days) <= date_tolerance_days:
                    return cand
                continue
            if ev.period and cand.period and _norm_period(ev.period) == _norm_period(cand.period):
                if ev.dividend and cand.dividend and abs(ev.dividend - cand.dividend) < 0.01:
                    return cand
        return None

    for group in groups:
        for ev in group:
            match = find_match(ev)
            if match is None:
                merged.append(ev)
            else:
                match.merge_from(ev)

    merged.sort(key=lambda e: e.sort_key, reverse=True)
    return merged


def _norm_period(p: str) -> str:
    return re.sub(r"[^\w\d]+", "", (p or "").lower())
