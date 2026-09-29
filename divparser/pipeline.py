# -*- coding: utf-8 -*-
"""Сборка данных: источники -> слияние -> обогащение с MOEX -> готовые строки.

Тут нет ни одной зашитой даты выплаты. Всё, что делает модуль, — тянет
актуальное состояние источников на момент запуска. Поэтому один и тот же
код одинаково отработает и сегодня, и через два года: появится новая
объявленная выплата — она приедет со статусом «Объявлен (предстоящий)».
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from .model import (
    STATUS_FORECAST,
    DividendEvent,
    merge_events,
)
from .moex import Moex, settlement_lag
from .sources import SOURCES, SourceError

log = logging.getLogger(__name__)


@dataclass
class TickerResult:
    """Результат по одному тикеру."""

    ticker: str
    secid: str
    config: dict
    info: dict = field(default_factory=dict)          # карточка бумаги с MOEX
    events: list = field(default_factory=list)        # список DividendEvent
    warnings: list = field(default_factory=list)
    last_price: float | None = None
    last_price_date: dt.date | None = None

    @property
    def next_event(self):
        """Ближайшая будущая выплата.

        Приоритет — официально объявленная. Если объявленных нет, отдаём
        ближайший прогноз ресурса: в общей таблице он будет помечен статусом,
        чтобы прогноз не выдавали за решение совета директоров.
        """
        today = dt.date.today()
        future = [e for e in self.events if e.record_date and e.record_date >= today]
        announced = [e for e in future if e.status != STATUS_FORECAST]
        pool = announced or future
        return min(pool, key=lambda e: e.record_date) if pool else None


class Pipeline:
    def __init__(
        self,
        http,
        source_names: list,
        start_date: dt.date,
        recalc_yield: bool = True,
        registry=None,
    ) -> None:
        self.http = http
        self.moex = Moex(http)
        self.start_date = start_date
        self.recalc_yield = recalc_yield
        self.registry = registry
        self.sources = []
        for name in source_names:
            cls = SOURCES.get(name)
            if cls is None:
                log.warning("неизвестный источник %r, пропускаю", name)
                continue
            self.sources.append(cls(http))

    # ---------------------------------------------------------------- публич.
    def run_ticker(self, cfg: dict) -> TickerResult:
        ticker = cfg["ticker"]
        secid = self.moex.resolve_secid(ticker, cfg.get("moex_aliases"))
        result = TickerResult(ticker=ticker, secid=secid, config=cfg)

        result.info = self.moex.security_info(secid) or {}
        if not result.info:
            result.warnings.append("MOEX не отдал карточку бумаги %s" % secid)

        groups = []
        for source in self.sources:
            try:
                events = source.fetch(ticker, cfg)
            except SourceError as exc:
                msg = "[%s] %s" % (source.name, exc)
                log.warning("%s: %s", ticker, msg)
                result.warnings.append(msg)
                continue
            except Exception as exc:  # источник сломался неожиданным образом
                msg = "[%s] непредвиденная ошибка разбора: %s" % (source.name, exc)
                log.exception("%s: %s", ticker, msg)
                result.warnings.append(msg)
                continue
            log.info("%s: %s -> %d записей", ticker, source.name, len(events))
            groups.append(events)

        if not groups:
            result.warnings.append(
                "ни один источник не отдал данные — строка останется пустой"
            )
            return result

        merged = merge_events(groups)
        merged = [e for e in merged if self._in_range(e) and self._is_meaningful(e)]

        result.last_price, result.last_price_date = self.moex.last_close(
            secid, self.start_date.year
        )
        for ev in merged:
            self._enrich(ev, secid, result)
        result.events = merged
        return result

    # --------------------------------------------------------------- приватн.
    def _in_range(self, ev: DividendEvent) -> bool:
        """Оставляем всё от start_date и дальше в будущее — верхней границы нет."""
        anchor = ev.record_date or ev.announce_date or ev.last_buy_date
        return anchor is None or anchor >= self.start_date

    @staticmethod
    def _is_meaningful(ev: DividendEvent) -> bool:
        """Отсекает пустые прогнозные строки.

        Агрегаторы держат в таблицах заготовки будущих отсечек с нулевым или
        неизвестным размером — по компаниям, которые дивиденды не платят.
        Такая строка ничего не сообщает, а таблицу засоряет. Реальные выплаты
        с нулевым размером эмитенты не объявляют: они просто не назначают
        дату закрытия реестра.
        """
        if ev.status == STATUS_FORECAST and not ev.dividend:
            return False
        return True

    def _enrich(self, ev: DividendEvent, secid: str, result: TickerResult) -> None:
        today = dt.date.today()

        # 1. Последний день покупки: если источник не дал — считаем по
        #    торговому календарю MOEX с учётом режима расчётов (T+1 / T+2).
        if ev.last_buy_date is None and ev.record_date:
            lag = settlement_lag(ev.record_date)
            calc = self.moex.shift_trading_days(
                secid, ev.record_date, lag, self.start_date.year
            )
            if calc is None:
                calc = _shift_weekdays(ev.record_date, lag)
                ev.notes.append("последний день покупки рассчитан по календарю (без биржевого)")
            else:
                ev.notes.append("последний день покупки рассчитан по торговому календарю MOEX")
            ev.last_buy_date = calc

        # 2. Цена закрытия. Для состоявшихся выплат — закрытие последнего дня
        #    покупки; для будущих — последнее доступное закрытие.
        moex_price = None
        price_date = None
        if ev.last_buy_date and ev.last_buy_date <= today:
            moex_price, price_date = self.moex.close_on(
                secid, ev.last_buy_date, self.start_date.year
            )
        if moex_price is None:
            moex_price, price_date = result.last_price, result.last_price_date
            if moex_price is not None:
                ev.notes.append("цена — последнее закрытие на MOEX")

        if moex_price is not None:
            if ev.price_close and abs(ev.price_close - moex_price) / moex_price > 0.02:
                ev.notes.append(
                    "цена источника %.2f отличается от MOEX %.2f" % (ev.price_close, moex_price)
                )
            ev.price_close = moex_price
            ev.price_date = price_date
        elif ev.price_close is None:
            ev.notes.append("цена закрытия не найдена")

        # 3. Доходность считаем сами — так она всегда согласована с
        #    показанными в таблице дивидендом и ценой.
        if self.recalc_yield and ev.dividend and ev.price_close:
            ev.yield_pct = ev.dividend / ev.price_close * 100.0

        ev.status = ev.resolve_status(today)

        # 4. Дата появления информации. Официальную дату объявления даёт
        #    dohod.ru, но по только что объявленным выплатам она приходит с
        #    задержкой. Тогда подставляем дату, когда выплату впервые увидел
        #    сам парсер, и явно это помечаем.
        if self.registry is not None:
            first_seen = self.registry.observe(ev.ticker, ev, today)
            # прогнозу ресурса дату «появления информации» не приписываем:
            # никакого решения по нему объявлено не было
            if (
                ev.announce_date is None
                and first_seen is not None
                and ev.status != STATUS_FORECAST
            ):
                ev.announce_date = first_seen
                ev.notes.append(
                    "дата объявления источником не указана; показана дата "
                    "первого обнаружения выплаты парсером"
                )
        if ev.announce_date is None and ev.status != STATUS_FORECAST:
            ev.notes.append("дата появления информации не найдена")


def _shift_weekdays(day: dt.date, n: int) -> dt.date:
    """Резервный сдвиг на n рабочих дней назад (без учёта праздников).
    Используется только когда биржевого календаря на эту дату ещё нет."""
    d = day
    left = n
    while left > 0:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d
