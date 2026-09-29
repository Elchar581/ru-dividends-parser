# -*- coding: utf-8 -*-
"""Офлайн-тесты разбора. Сеть не нужны — запускаются за секунду:

    python -m pytest tests -q
    (или без pytest:  python tests/test_parsing.py)

Смысл этих тестов — быстро проверить парсер после того, как источник
изменит вёрстку: если упал тест на HTML — правится разбор, если всё
зелено, а данных нет — проблема на стороне сайта.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# консоль Windows по умолчанию не в UTF-8 — иначе вывод падает на кириллице
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # перенаправленный поток без reconfigure
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from divparser.html_tables import Columns, iter_tables
from divparser.model import (
    STATUS_FORECAST,
    STATUS_PAID,
    STATUS_UPCOMING,
    DividendEvent,
    merge_events,
    parse_date,
    parse_number,
)
from divparser.moex import settlement_lag
from divparser.pipeline import _shift_weekdays


def test_parse_date_iso_before_russian():
    # MOEX отдаёт ISO; без приоритета ISO строка читалась как 25.07.2017
    assert parse_date("2025-07-17") == dt.date(2025, 7, 17)
    assert parse_date("17.07.2025") == dt.date(2025, 7, 17)
    assert parse_date("13.06.19") == dt.date(2019, 6, 13)
    assert parse_date("20.07.2026 (прогноз)") == dt.date(2026, 7, 20)
    assert parse_date("n/a") is None
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date(dt.date(2024, 1, 2)) == dt.date(2024, 1, 2)
    assert parse_date("31.02.2025") is None  # несуществующая дата


def test_parse_number_russian_formats():
    assert parse_number("37,64 ₽") == 37.64
    assert parse_number("1 234,5") == 1234.5
    assert parse_number("13,6%") == 13.6
    assert parse_number("255 16 руб".replace(" ", "")) == 25516.0
    assert parse_number("0,2913") == 0.2913
    assert parse_number("n/a") is None
    assert parse_number("-") is None
    assert parse_number(42) == 42.0


def test_settlement_mode_switch():
    # до 31.07.2023 режим T+2, после — T+1
    assert settlement_lag(dt.date(2022, 5, 10)) == 2
    assert settlement_lag(dt.date(2023, 7, 30)) == 2
    assert settlement_lag(dt.date(2023, 7, 31)) == 1
    assert settlement_lag(dt.date(2026, 10, 13)) == 1


def test_shift_weekdays_skips_weekend():
    # понедельник 21.09.2026 -> предыдущий рабочий день пятница 18.09
    assert _shift_weekdays(dt.date(2026, 9, 21), 1) == dt.date(2026, 9, 18)


def test_status_depends_on_today_not_on_hardcode():
    """Статус пересчитывается от текущей даты — это и делает выгрузку
    воспроизводимой через год и через два."""
    past = DividendEvent(ticker="X", record_date=dt.date(2024, 5, 1))
    future = DividendEvent(ticker="X", record_date=dt.date(2099, 5, 1))
    today = dt.date(2026, 9, 4)
    assert past.resolve_status(today) == STATUS_PAID
    assert future.resolve_status(today) == STATUS_UPCOMING


def test_merge_joins_sources_by_record_date():
    a = DividendEvent(ticker="SBER", record_date=dt.date(2026, 7, 20),
                      dividend=37.64, last_buy_date=dt.date(2026, 7, 17),
                      sources={"smart-lab"})
    b = DividendEvent(ticker="SBER", record_date=dt.date(2026, 7, 20),
                      dividend=37.64, announce_date=dt.date(2026, 4, 21),
                      sources={"dohod.ru"})
    merged = merge_events([[a], [b]])
    assert len(merged) == 1
    ev = merged[0]
    assert ev.announce_date == dt.date(2026, 4, 21)
    assert ev.last_buy_date == dt.date(2026, 7, 17)
    assert ev.sources == {"smart-lab", "dohod.ru"}


def test_merge_flags_disagreement_instead_of_hiding_it():
    a = DividendEvent(ticker="X", record_date=dt.date(2026, 7, 20),
                      dividend=37.64, sources={"smart-lab"})
    b = DividendEvent(ticker="X", record_date=dt.date(2026, 7, 20),
                      dividend=30.00, sources={"dohod.ru"})
    ev = merge_events([[a], [b]])[0]
    assert any("расхождение" in n for n in ev.notes)


def test_announced_payout_beats_forecast_from_other_source():
    """Если один источник знает об объявленной выплате, а второй считает её
    прогнозом — доверяем объявлению."""
    announced = DividendEvent(ticker="X", record_date=dt.date(2099, 10, 13),
                              dividend=42.51, sources={"smart-lab"})
    forecast = DividendEvent(ticker="X", record_date=dt.date(2099, 10, 13),
                             dividend=42.51, status=STATUS_FORECAST,
                             sources={"dohod.ru"})
    ev = merge_events([[announced], [forecast]])[0]
    assert ev.resolve_status(dt.date(2026, 9, 4)) == STATUS_UPCOMING


def test_columns_found_by_name_not_by_position():
    """Источник вправе переставить и добавить колонки — разбор не должен
    сломаться, потому что ищем по заголовку."""
    html = """
    <table><tr><td colspan=3>Выплаченные дивиденды</td></tr>
    <tr><th>Период</th><th>Тикер</th><th>дата отсечки</th>
        <th>Новая колонка</th><th>дивиденд</th></tr>
    <tr><td>2025 год</td><td>SBER</td><td>20.07.2026</td>
        <td>x</td><td>37,64 &#8381;</td></tr>
    </table>"""
    tables = list(iter_tables(html))
    assert len(tables) == 1
    _tag, header, rows = tables[0]
    cols = Columns(header)
    assert cols.find(r"отсечк") == 2
    assert cols.find(r"^дивиденд") == 4
    assert parse_number(cols.get(rows[0], cols.find(r"^дивиденд"))) == 37.64
    assert parse_date(cols.get(rows[0], cols.find(r"отсечк"))) == dt.date(2026, 7, 20)


def test_missing_column_returns_empty_not_crash():
    cols = Columns(["Период", "дивиденд"])
    assert cols.find(r"отсечк") is None
    assert cols.get(["2025 год", "10"], None) == ""


def _run_all():
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("OK   %s" % name)
        except AssertionError as exc:
            failed += 1
            print("ПАДЁТ %s: %s" % (name, exc or "проверка не прошла"))
        except Exception as exc:
            failed += 1
            print("ОШИБКА %s: %r" % (name, exc))
    print("\nПровалено тестов: %d" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
