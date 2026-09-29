# -*- coding: utf-8 -*-
"""Точка входа: параметры командной строки и оркестрация прогона."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from .excel import ExcelReport
from .net import Http
from .pipeline import Pipeline
from .registry import Registry
from .sources import DEFAULT_SOURCES, SOURCES

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("divparser")


def setup_logging(verbose: bool, log_file: Path | None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers: list[logging.Handler] = []
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    handlers.append(stream)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)
    logging.basicConfig(level=logging.DEBUG, format=fmt, handlers=handlers, datefmt="%H:%M:%S")
    for noisy in ("urllib3", "charset_normalizer", "chardet"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    # на Windows консоль бывает в cp866 — не даём логу уронить прогон
    try:
        stream.stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="div-parser",
        description="Парсер истории дивидендов российских эмитентов -> Excel. "
                    "Собирает и уже состоявшиеся, и объявленные будущие выплаты.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", type=Path, default=ROOT / "config" / "issuers.json",
                   help="справочник эмитентов")
    p.add_argument("--out", type=Path, default=None,
                   help="путь к файлу Excel (по умолчанию output/Дивиденды_ДАТА.xlsx)")
    p.add_argument("--start", default="2021-01-01",
                   help="начало периода истории, ГГГГ-ММ-ДД")
    p.add_argument("--tickers", default="",
                   help="ограничить прогон списком тикеров через запятую (для отладки)")
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                   help="источники через запятую; доступны: " + ", ".join(SOURCES))
    p.add_argument("--cache-hours", type=float, default=12.0,
                   help="сколько часов считать закэшированный ответ свежим; 0 — всегда качать")
    p.add_argument("--offline", action="store_true",
                   help="работать только на кэше, в сеть не ходить")
    p.add_argument("--no-recalc-yield", action="store_true",
                   help="не пересчитывать доходность, брать значение источника")
    p.add_argument("--delay", type=float, default=0.7,
                   help="пауза между запросами к одному сайту, сек")
    p.add_argument("--registry", type=Path, default=ROOT / "output" / "first_seen.json",
                   help="журнал первого обнаружения выплат; накапливается между прогонами")
    p.add_argument("--no-registry", action="store_true",
                   help="не вести и не использовать журнал первого обнаружения")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def load_config(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    issuers = data["issuers"] if isinstance(data, dict) else data
    seen = set()
    for row in issuers:
        if not row.get("ticker"):
            raise ValueError("в справочнике есть строка без поля 'ticker'")
        if row["ticker"] in seen:
            raise ValueError("тикер %s встречается дважды" % row["ticker"])
        seen.add(row["ticker"])
        row.setdefault("issuer_key", row["ticker"])
        row.setdefault("name", row["ticker"])
        row.setdefault("share_type", "Обыкновенная")
    return issuers


def group_by_issuer(results: list) -> list:
    """Схлопывает ао+ап одного эмитента в одну группу: 30 тикеров -> 28 листов."""
    order: list = []
    index: dict = {}
    for res in results:
        key = res.config.get("issuer_key", res.ticker)
        if key not in index:
            name = res.config.get("name", key)
            # для листа берём имя без пометки типа акции
            base = name.replace("-п", "").replace("-ап", "").strip()
            index[key] = {"key": key, "name": base, "rows": []}
            order.append(index[key])
        index[key]["rows"].append(res)
    return order


def main(argv=None) -> int:
    args = parse_args(argv)
    stamp = dt.datetime.now()
    out_path = args.out or (
        ROOT / "output" / ("Дивиденды_РФ_%s.xlsx" % stamp.strftime("%Y-%m-%d"))
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(args.verbose, ROOT / "output" / "run.log")

    try:
        start_date = dt.date.fromisoformat(args.start)
    except ValueError:
        log.error("--start должен быть в формате ГГГГ-ММ-ДД, получено %r", args.start)
        return 2

    try:
        issuers = load_config(args.config)
    except (OSError, ValueError, KeyError) as exc:
        log.error("не читается справочник %s: %s", args.config, exc)
        return 2

    only = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
    if only:
        issuers = [i for i in issuers if i["ticker"].upper() in only]
        if not issuers:
            log.error("в справочнике нет ни одного из тикеров: %s", ", ".join(sorted(only)))
            return 2

    http = Http(
        cache_dir=ROOT / ".cache",
        cache_ttl_hours=args.cache_hours,
        delay=args.delay,
        offline=args.offline,
    )
    registry = None if args.no_registry else Registry(args.registry)
    pipeline = Pipeline(
        http,
        source_names=[s.strip() for s in args.sources.split(",") if s.strip()],
        start_date=start_date,
        recalc_yield=not args.no_recalc_yield,
        registry=registry,
    )

    log.info("Старт. Эмитентов в работе: %d, период с %s", len(issuers), start_date)
    results = []
    for n, cfg in enumerate(issuers, start=1):
        log.info("[%d/%d] %s (%s)", n, len(issuers), cfg["ticker"], cfg.get("name", ""))
        try:
            results.append(pipeline.run_ticker(cfg))
        except Exception as exc:  # один тикер не должен ронять весь прогон
            log.exception("%s: прогон прерван: %s", cfg["ticker"], exc)
            from .pipeline import TickerResult

            results.append(
                TickerResult(
                    ticker=cfg["ticker"], secid=cfg["ticker"], config=cfg,
                    warnings=["прогон по тикеру прерван: %s" % exc],
                )
            )

    if registry is not None:
        registry.save()
        if registry.changes:
            log.info("Изменения с прошлого прогона (%d):", len(registry.changes))
            for change in registry.changes:
                log.info("  * %s", change)

    groups = group_by_issuer(results)
    report = ExcelReport(start_date=start_date, generated_at=stamp)
    report.build(groups)
    report.save(out_path)

    total_events = sum(len(r.events) for r in results)
    problem = [r for r in results if r.warnings]
    log.info("Готово: %s", out_path)
    log.info(
        "Листов: 1 общий + %d по эмитентам + журнал. Выплат собрано: %d.",
        len(groups), total_events,
    )
    if problem:
        log.warning(
            "С замечаниями %d тикеров: %s — подробности на листе «Журнал сбора».",
            len(problem), ", ".join(r.ticker for r in problem),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
