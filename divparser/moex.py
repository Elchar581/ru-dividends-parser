# -*- coding: utf-8 -*-
"""Клиент MOEX ISS — официальный источник цен, торгового календаря и
карточки бумаги. Ключ/регистрация не нужны.
"""
from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from typing import Optional

from .model import parse_date

log = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
BOARD = "TQBR"  # основной режим торгов акциями


class Moex:
    def __init__(self, http) -> None:
        self.http = http
        self._history_cache: dict[tuple[str, int], list[tuple[dt.date, float]]] = {}

    # ------------------------------------------------------------ карточка ЦБ
    def security_info(self, secid: str) -> dict:
        """Полное наименование, ISIN, уровень листинга и т.п."""
        url = f"{ISS}/securities/{secid}.json?iss.meta=off"
        data = self.http.get_json(url)
        out: dict[str, object] = {}
        if not data or "description" not in data:
            return out
        block = data["description"]
        idx = {c: i for i, c in enumerate(block["columns"])}
        for row in block["data"]:
            out[row[idx["name"]]] = row[idx["value"]]
        return out

    def is_traded(self, secid: str) -> bool:
        url = f"{ISS}/securities/{secid}.json?iss.meta=off&iss.only=boards"
        data = self.http.get_json(url)
        if not data or "boards" not in data:
            return False
        idx = {c: i for i, c in enumerate(data["boards"]["columns"])}
        for row in data["boards"]["data"]:
            if row[idx.get("boardid", 0)] == BOARD and row[idx.get("is_traded", 0)] == 1:
                return True
        return False

    def resolve_secid(self, ticker: str, aliases: Optional[list] = None) -> str:
        """Возвращает актуальный код бумаги.

        Тикеры на Мосбирже меняются (TCSG -> T, YNDX -> YDEX). Сначала проверяем
        основной код, потом перечисленные в конфиге псевдонимы. Это и есть
        страховка на будущее: конфиг правится без правки кода.
        """
        for cand in [ticker] + list(aliases or []):
            if self.is_traded(cand):
                if cand != ticker:
                    log.warning("тикер %s не торгуется, использую %s", ticker, cand)
                return cand
        log.warning("не удалось подтвердить торгуемость %s на %s", ticker, BOARD)
        return ticker

    # ------------------------------------------------------------------ цены
    def history(self, secid: str, year_from: int) -> list[tuple[dt.date, float]]:
        """Дневная история (дата, цена закрытия) с постраничной выкачкой.

        Берём LEGALCLOSEPRICE — официальную цену закрытия основной сессии;
        именно её используют для расчёта дивидендной доходности.
        """
        key = (secid, year_from)
        if key in self._history_cache:
            return self._history_cache[key]

        rows: list[tuple[dt.date, float]] = []
        start = 0
        till = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        while True:
            url = (
                f"{ISS}/history/engines/stock/markets/shares/boards/{BOARD}"
                f"/securities/{secid}.json?iss.meta=off"
                f"&from={year_from}-01-01&till={till}&start={start}"
                f"&iss.only=history&history.columns=TRADEDATE,LEGALCLOSEPRICE,CLOSE"
            )
            data = self.http.get_json(url)
            if not data or "history" not in data:
                break
            block = data["history"]
            idx = {c: i for i, c in enumerate(block["columns"])}
            chunk = block["data"]
            if not chunk:
                break
            for row in chunk:
                d = parse_date(row[idx["TRADEDATE"]])
                price = row[idx.get("LEGALCLOSEPRICE", -1)]
                if price in (None, 0):
                    price = row[idx.get("CLOSE", -1)]
                if d and price:
                    rows.append((d, float(price)))
            start += len(chunk)
            if len(chunk) < 100:
                break
            if start > 20000:  # предохранитель от бесконечного цикла
                log.warning("слишком длинная история по %s, обрываю", secid)
                break

        rows.sort()
        self._history_cache[key] = rows
        return rows

    def close_on(self, secid: str, day: dt.date, year_from: int) -> tuple[Optional[float], Optional[dt.date]]:
        """Цена закрытия на указанный день; если торгов не было — ближайший
        предшествующий торговый день."""
        rows = self.history(secid, year_from)
        best: tuple[Optional[float], Optional[dt.date]] = (None, None)
        for d, price in rows:
            if d <= day:
                best = (price, d)
            else:
                break
        return best

    def last_close(self, secid: str, year_from: int) -> tuple[Optional[float], Optional[dt.date]]:
        rows = self.history(secid, year_from)
        if not rows:
            return (None, None)
        d, price = rows[-1]
        return (price, d)

    # ------------------------------------------------- торговый календарь T-N
    def trading_days(self, secid: str, year_from: int) -> list[dt.date]:
        return [d for d, _ in self.history(secid, year_from)]

    def shift_trading_days(self, secid: str, day: dt.date, n: int, year_from: int) -> Optional[dt.date]:
        """День, отстоящий на n торговых дней назад от `day`."""
        days = self.trading_days(secid, year_from)
        if not days:
            return None
        if day > days[-1]:
            # дата в будущем, торгового календаря на неё ещё не существует —
            # честно говорим «не знаю», вызывающий код перейдёт на
            # календарный расчёт по рабочим дням
            return None
        # индекс последнего торгового дня <= day
        lo, hi = 0, len(days) - 1
        pos = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if days[mid] <= day:
                pos = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if pos < 0:
            return None
        # если day сам торговый — отсчитываем от него
        target = pos - n if days[pos] == day else pos - n + 1
        if 0 <= target < len(days):
            return days[target]
        return None


# Режим расчётов на Мосбирже: до 31.07.2023 был T+2, после — T+1.
# Дата вынесена в константу, чтобы при следующей реформе правился один литерал.
T1_MODE_SINCE = dt.date(2023, 7, 31)


def settlement_lag(record_date: dt.date) -> int:
    """Сколько торговых дней до отсечки нужно купить акцию."""
    return 1 if record_date >= T1_MODE_SINCE else 2
