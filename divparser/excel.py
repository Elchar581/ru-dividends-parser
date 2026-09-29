# -*- coding: utf-8 -*-
"""Формирование книги Excel: Таблица №1 (общая) + 28 листов по эмитентам.

Даты и числа пишутся НАСТОЯЩИМИ типами Excel (не строками) — иначе по ним
нельзя ни сортировать, ни считать.
"""
from __future__ import annotations

import datetime as dt
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .model import STATUS_FORECAST, STATUS_PAID, STATUS_UPCOMING

# Оформление
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=13, color="1F4E79")
NOTE_FONT = Font(italic=True, size=9, color="808080")
UPCOMING_FILL = PatternFill("solid", fgColor="E2F0D9")   # объявленные будущие
FORECAST_FILL = PatternFill("solid", fgColor="FFF2CC")   # прогноз
WARN_FILL = PatternFill("solid", fgColor="FCE4E4")       # проблемы
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

DATE_FMT = "DD.MM.YYYY"
MONEY_FMT = "# ##0.00##"
PCT_FMT = "0.00"

SHEET1_COLUMNS = [
    ("№", 5),
    ("Тикер", 10),
    ("Компания", 26),
    ("Тип акции", 16),
    ("Полное наименование (MOEX)", 34),
    ("ISIN", 15),
    ("Уровень листинга", 9),
    ("Ссылка на сайт эмитента", 42),
    ("Председатель СД / крупнейший акционер", 62),
    ("Выплат в выборке", 9),
    ("Последняя цена, ₽", 12),
    ("Ближайшая отсечка", 13),
    ("Ближайший дивиденд, ₽", 12),
    ("Ожидаемая доходность, %", 12),
    ("Статус ближайшей выплаты", 24),
    ("Лист с историей", 20),
    ("Замечания сбора", 40),
]

DETAIL_COLUMNS = [
    ("№", 5),
    ("Тикер", 10),
    ("Тип акции", 16),
    ("Период / год учёта", 18),
    ("Дата появления информации", 15),
    ("Последний день покупки", 15),
    ("Дата закрытия реестра", 15),
    ("Размер дивиденда, ₽", 13),
    ("Цена акции на закрытие, ₽", 14),
    ("Дивидендная доходность, %", 13),
    ("Статус", 24),
    ("Дата цены", 12),
    ("Источники", 20),
    ("Примечания", 52),
]


def _safe_sheet_name(name: str) -> str:
    """Excel запрещает : \\ / ? * [ ] и ограничивает имя 31 символом."""
    cleaned = re.sub(r"[:\\/?*\[\]]", "-", name).strip()
    return cleaned[:31] or "Лист"


def _write_header(ws, columns, row=1):
    for col, (title, width) in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[row].height = 32


def _finish_table(ws, columns, header_row, last_row, name):
    """Оформляет диапазон как таблицу Excel — включает автофильтр и сортировку."""
    if last_row <= header_row:
        return
    ref = "A%d:%s%d" % (header_row, get_column_letter(len(columns)), last_row)
    table = Table(displayName=re.sub(r"\W", "_", name)[:30] or "T", ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleLight9", showRowStripes=True, showColumnStripes=False
    )
    try:
        ws.add_table(table)
    except ValueError:
        ws.auto_filter.ref = ref  # имя занято — хотя бы фильтр
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)


def _set(ws, row, col, value, fmt=None, fill=None):
    cell = ws.cell(row=row, column=col, value=value)
    if fmt:
        cell.number_format = fmt
    if fill:
        cell.fill = fill
    cell.border = BORDER
    cell.alignment = Alignment(vertical="center", wrap_text=isinstance(value, str) and len(str(value)) > 30)
    return cell


class ExcelReport:
    def __init__(self, start_date: dt.date, generated_at: dt.datetime | None = None) -> None:
        self.wb = Workbook()
        self.start_date = start_date
        self.generated_at = generated_at or dt.datetime.now()
        self.summary = self.wb.active
        self.summary.title = "Таблица №1 Общая"
        self._sheet_names: dict[str, str] = {}

    # ------------------------------------------------------------------ вывод
    def build(self, groups: list) -> None:
        """groups — список групп: [{'key','name','rows':[TickerResult,...]}, ...]"""
        for group in groups:
            self._sheet_names[group["key"]] = self._detail_sheet(group)
        self._summary_sheet(groups)
        self._log_sheet(groups)
        self.wb.move_sheet(self.summary, offset=-len(self.wb.sheetnames))

    def save(self, path) -> None:
        self.wb.save(path)

    # ------------------------------------------------------------- Таблица №1
    def _summary_sheet(self, groups) -> None:
        ws = self.summary
        ws.cell(row=1, column=1, value="Таблица №1. Общий список акций").font = TITLE_FONT
        ws.cell(
            row=2, column=1,
            value="Сформировано %s. Период истории выплат: с %s по настоящее время "
                  "(включая уже объявленные будущие выплаты)."
                  % (self.generated_at.strftime("%d.%m.%Y %H:%M"),
                     self.start_date.strftime("%d.%m.%Y")),
        ).font = NOTE_FONT
        header_row = 4
        _write_header(ws, SHEET1_COLUMNS, header_row)

        row = header_row
        for group in groups:
            for res in group["rows"]:
                row += 1
                cfg = res.config
                info = res.info
                nxt = res.next_event
                fill = WARN_FILL if res.warnings else None
                _set(ws, row, 1, cfg.get("no"), fill=fill)
                _set(ws, row, 2, res.ticker, fill=fill)
                _set(ws, row, 3, cfg.get("name", ""), fill=fill)
                _set(ws, row, 4, cfg.get("share_type", ""), fill=fill)
                _set(ws, row, 5, info.get("NAME") or info.get("SHORTNAME") or "", fill=fill)
                _set(ws, row, 6, info.get("ISIN", ""), fill=fill)
                _set(ws, row, 7, info.get("LISTLEVEL", ""), fill=fill)
                link = cfg.get("site", "")
                cell = _set(ws, row, 8, link, fill=fill)
                if link:
                    cell.hyperlink = link
                    cell.font = Font(color="0563C1", underline="single", size=10)
                _set(ws, row, 9, cfg.get("chairman", ""), fill=fill)
                _set(ws, row, 10, len(res.events), fill=fill)
                _set(ws, row, 11, res.last_price, MONEY_FMT, fill=fill)
                _set(ws, row, 12, nxt.record_date if nxt else None, DATE_FMT, fill=fill)
                _set(ws, row, 13, nxt.dividend if nxt else None, MONEY_FMT, fill=fill)
                _set(ws, row, 14, nxt.yield_pct if nxt else None, PCT_FMT, fill=fill)
                _set(ws, row, 15, nxt.status if nxt else "нет объявленных выплат",
                     fill=FORECAST_FILL if nxt and nxt.status == STATUS_FORECAST else fill)
                sheet = self._sheet_names.get(group["key"], "")
                cell = _set(ws, row, 16, sheet, fill=fill)
                if sheet:
                    cell.hyperlink = "#'%s'!A1" % sheet
                    cell.font = Font(color="0563C1", underline="single", size=10)
                _set(ws, row, 17, "; ".join(res.warnings), fill=fill)

        _finish_table(ws, SHEET1_COLUMNS, header_row, row, "Общая")
        ws.freeze_panes = ws.cell(row=header_row + 1, column=3)

    # ---------------------------------------------------------- листы Часть 2
    def _detail_sheet(self, group) -> str:
        rows = group["rows"]
        title = "%02d %s" % (rows[0].config.get("no", 0), group["name"])
        ws = self.wb.create_sheet(_safe_sheet_name(title))

        ws.cell(row=1, column=1, value="%s — история дивидендов" % group["name"]).font = TITLE_FONT
        tickers = " / ".join(r.ticker for r in rows)
        ws.cell(
            row=2, column=1,
            value="Тикеры: %s. Период: с %s по настоящее время. "
                  "Строки со статусом «Объявлен (предстоящий)» — будущие выплаты, "
                  "уже утверждённые эмитентом."
                  % (tickers, self.start_date.strftime("%d.%m.%Y")),
        ).font = NOTE_FONT

        header_row = 4
        _write_header(ws, DETAIL_COLUMNS, header_row)

        # объединяем события всех тикеров эмитента (ао + ап) и сортируем
        combined = []
        for res in rows:
            for ev in res.events:
                combined.append((res, ev))
        combined.sort(key=lambda pair: (pair[1].sort_key, pair[0].ticker), reverse=True)

        row = header_row
        num = 0
        for res, ev in combined:
            row += 1
            num += 1
            if ev.status == STATUS_UPCOMING:
                fill = UPCOMING_FILL
            elif ev.status == STATUS_FORECAST:
                fill = FORECAST_FILL
            else:
                fill = None
            _set(ws, row, 1, num, fill=fill)
            _set(ws, row, 2, ev.ticker, fill=fill)
            _set(ws, row, 3, res.config.get("share_type", ""), fill=fill)
            _set(ws, row, 4, ev.period, fill=fill)
            _set(ws, row, 5, ev.announce_date, DATE_FMT, fill=fill)
            _set(ws, row, 6, ev.last_buy_date, DATE_FMT, fill=fill)
            _set(ws, row, 7, ev.record_date, DATE_FMT, fill=fill)
            _set(ws, row, 8, ev.dividend, MONEY_FMT, fill=fill)
            _set(ws, row, 9, ev.price_close, MONEY_FMT, fill=fill)
            _set(ws, row, 10, ev.yield_pct, PCT_FMT, fill=fill)
            _set(ws, row, 11, ev.status, fill=fill)
            _set(ws, row, 12, ev.price_date, DATE_FMT, fill=fill)
            _set(ws, row, 13, ", ".join(sorted(ev.sources)), fill=fill)
            _set(ws, row, 14, "; ".join(ev.notes), fill=fill)

        if num == 0:
            row += 1
            _set(ws, row, 1, "Данные не получены", fill=WARN_FILL)

        _finish_table(ws, DETAIL_COLUMNS, header_row, row, "T" + group["key"])

        legend = row + 2
        ws.cell(row=legend, column=1, value="Обозначения:").font = Font(bold=True, size=9)
        for i, (text, fill) in enumerate(
            [
                ("%s — выплата состоялась, реестр закрыт" % STATUS_PAID, None),
                ("%s — совет директоров объявил, отсечка впереди" % STATUS_UPCOMING, UPCOMING_FILL),
                ("%s — оценка ресурса, официального решения нет" % STATUS_FORECAST, FORECAST_FILL),
            ],
            start=1,
        ):
            cell = ws.cell(row=legend + i, column=1, value=text)
            cell.font = Font(size=9)
            if fill:
                cell.fill = fill
        return ws.title

    # ------------------------------------------------------------------ лог
    def _log_sheet(self, groups) -> None:
        ws = self.wb.create_sheet("Журнал сбора")
        ws.cell(row=1, column=1, value="Журнал сбора данных").font = TITLE_FONT
        ws.cell(
            row=2, column=1,
            value="Пустой журнал — все источники отработали штатно. Строки здесь "
                  "означают, что источник изменил разметку или был недоступен: "
                  "данные по этим тикерам стоит перепроверить вручную.",
        ).font = NOTE_FONT
        header_row = 4
        cols = [("Тикер", 12), ("Компания", 28), ("Замечание", 110)]
        _write_header(ws, cols, header_row)
        row = header_row
        for group in groups:
            for res in group["rows"]:
                for warn in res.warnings:
                    row += 1
                    _set(ws, row, 1, res.ticker, fill=WARN_FILL)
                    _set(ws, row, 2, res.config.get("name", ""), fill=WARN_FILL)
                    _set(ws, row, 3, warn, fill=WARN_FILL)
        if row == header_row:
            row += 1
            _set(ws, row, 1, "—")
            _set(ws, row, 3, "Замечаний нет, все источники ответили.")
        _finish_table(ws, cols, header_row, row, "Журнал")
