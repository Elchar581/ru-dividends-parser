# -*- coding: utf-8 -*-
"""Источники дивидендных данных.

Каждый источник независим и возвращает список DividendEvent. Если один
источник изменит вёрстку или ляжет — прогон продолжится на остальных,
а в лист «Журнал» попадёт предупреждение.

Добавить источник = написать класс с методом fetch(ticker, cfg) и
зарегистрировать его в SOURCES.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from .html_tables import Columns, iter_tables
from .model import (
    STATUS_FORECAST,
    DividendEvent,
    is_forecast,
    parse_date,
    parse_number,
)

log = logging.getLogger(__name__)


class SourceError(Exception):
    """Источник не отдал данные или изменил разметку."""


class BaseSource:
    name = "base"

    def __init__(self, http) -> None:
        self.http = http

    def fetch(self, ticker: str, cfg: dict) -> list:
        raise NotImplementedError


# --------------------------------------------------------------------------
class SmartLabSource(BaseSource):
    """smart-lab.ru — история выплат плюс отдельная таблица ожидаемых выплат.

    Даёт: последний день покупки (T-1), дату отсечки, период, дивиденд,
    цену акции и доходность. Именно отсюда приходят БУДУЩИЕ выплаты,
    как только их объявит совет директоров.
    """

    name = "smart-lab"
    URL = "https://smart-lab.ru/q/{page}/dividend/"

    def fetch(self, ticker: str, cfg: dict) -> list:
        # у привилегированных акций своей страницы нет — они идут строками
        # на странице обыкновенных (SBERP -> SBER, BSPBP -> BSPB)
        page = cfg.get("smartlab_page") or ticker
        html = self.http.get(self.URL.format(page=page))
        if html is None:
            raise SourceError("страница smart-lab недоступна: %s" % page)

        events = []
        seen_table = False
        for table, header, rows in iter_tables(html):
            cols = Columns(header)
            c_record = cols.find(r"отсечк", r"закрыти\w* реестр")
            c_div = cols.find(r"^дивиденд", r"дивиденд руб")
            if c_record is None or c_div is None:
                continue
            seen_table = True
            c_ticker = cols.find(r"тикер", r"бумаг", r"^код")
            c_t1 = cols.find(r"дата t.?1", r"последн\w* день", r"куп\w* до")
            c_period = cols.find(r"период")
            c_price = cols.find(r"цена")
            c_yield = cols.find(r"доходност")

            caption = self._table_caption(table)
            forecast_table = bool(re.search(r"прогноз", caption, re.I))

            for row in rows:
                if c_ticker is not None:
                    row_ticker = cols.get(row, c_ticker).strip().upper()
                    if row_ticker and row_ticker != ticker.upper():
                        continue
                record = parse_date(cols.get(row, c_record))
                div = parse_number(cols.get(row, c_div))
                if record is None and div is None:
                    continue
                raw_period = cols.get(row, c_period)
                ev = DividendEvent(
                    ticker=ticker,
                    record_date=record,
                    last_buy_date=parse_date(cols.get(row, c_t1)),
                    period=raw_period,
                    dividend=div,
                    price_close=parse_number(cols.get(row, c_price)),
                    yield_pct=parse_number(cols.get(row, c_yield)),
                    sources={self.name},
                )
                if forecast_table or is_forecast(raw_period, cols.get(row, c_record)):
                    ev.status = STATUS_FORECAST
                events.append(ev)

        if not seen_table:
            raise SourceError(
                "на странице smart-lab/%s не найдена таблица дивидендов "
                "(вероятно, изменилась вёрстка сайта)" % page
            )
        return events

    @staticmethod
    def _table_caption(table) -> str:
        """Подпись таблицы: атрибут выгрузки в CSV, caption или первая строка."""
        for attr in ("financials-param-csv-download-data", "data-title", "summary"):
            value = table.get(attr)
            if value:
                return str(value)
        cap = table.find("caption")
        if cap:
            return cap.get_text(" ", strip=True)
        first = table.find("tr")
        return first.get_text(" ", strip=True) if first else ""


# --------------------------------------------------------------------------
class DohodSource(BaseSource):
    """dohod.ru — единственный из доступных источников, который публикует
    ДАТУ ОБЪЯВЛЕНИЯ дивиденда (в ТЗ — «дата появления информации на ресурсах»).
    Служит также перекрёстной проверкой размера дивиденда и даты отсечки.
    """

    name = "dohod.ru"
    URL = "https://www.dohod.ru/ik/analytics/dividend/{slug}"

    def fetch(self, ticker: str, cfg: dict) -> list:
        slug = cfg.get("dohod_slug") or ticker.lower()
        html = self.http.get(self.URL.format(slug=slug))
        if html is None:
            raise SourceError("страница dohod.ru недоступна: %s" % slug)

        events = []
        found = False
        for _table, header, rows in iter_tables(html):
            cols = Columns(header)
            c_announce = cols.find(r"дата объявлени", r"объявлен")
            c_record = cols.find(r"закрыти\w* реестр", r"отсечк")
            if c_announce is None or c_record is None:
                continue
            found = True
            c_div = cols.find(r"^дивиденд")
            c_year = cols.find(r"год для учета", r"^год")
            c_yield = cols.find(r"доходност")

            for row in rows:
                raw_record = cols.get(row, c_record)
                record = parse_date(raw_record)
                announce = parse_date(cols.get(row, c_announce))
                div = parse_number(cols.get(row, c_div))
                if record is None and announce is None:
                    continue
                year = cols.get(row, c_year)
                if year.strip().lower() in ("n/a", "-", "—"):
                    year = ""
                ev = DividendEvent(
                    ticker=ticker,
                    record_date=record,
                    announce_date=announce,
                    period=year,
                    dividend=div,
                    yield_pct=parse_number(cols.get(row, c_yield)),
                    sources={self.name},
                )
                # Прогнозом считаем только то, что источник сам пометил
                # («(прогноз)» в дате). Отсутствие даты объявления — не повод:
                # у dohod.ru её просто может не быть, а выплата при этом
                # официально объявлена и придёт из smart-lab.
                if is_forecast(raw_record):
                    ev.status = STATUS_FORECAST
                events.append(ev)

        if not found:
            raise SourceError(
                "на странице dohod.ru/%s не найдена таблица с датой объявления "
                "(вероятно, изменилась вёрстка сайта)" % slug
            )
        return events


# --------------------------------------------------------------------------
class ZakrytiyaSource(BaseSource):
    """закрытияреестров.рф — резервный источник дат отсечек и размеров.

    Подключается флагом --sources, если основные разошлись в данных.
    """

    name = "закрытияреестров.рф"
    URL = "https://xn--80aeaogecdcfbcpiabkfdchi5aql5j0a.xn--p1ai/{ticker}/"

    def fetch(self, ticker: str, cfg: dict) -> list:
        html = self.http.get(self.URL.format(ticker=ticker.upper()))
        if html is None:
            raise SourceError("страница недоступна")
        events = []
        for _t, header, rows in iter_tables(html):
            cols = Columns(header)
            c_record = cols.find(r"закрыти\w* реестр", r"отсечк")
            c_div = cols.find(r"дивиденд", r"размер")
            if c_record is None or c_div is None:
                continue
            for row in rows:
                record = parse_date(cols.get(row, c_record))
                if record is None:
                    continue
                events.append(
                    DividendEvent(
                        ticker=ticker,
                        record_date=record,
                        dividend=parse_number(cols.get(row, c_div)),
                        sources={self.name},
                    )
                )
        if not events:
            raise SourceError("таблица не распознана")
        return events


SOURCES = {
    SmartLabSource.name: SmartLabSource,
    DohodSource.name: DohodSource,
    ZakrytiyaSource.name: ZakrytiyaSource,
}

DEFAULT_SOURCES = [SmartLabSource.name, DohodSource.name]
