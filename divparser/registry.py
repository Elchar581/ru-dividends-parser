# -*- coding: utf-8 -*-
"""Реестр первого обнаружения выплат.

Зачем. «Дата появления информации на ресурсах» из ТЗ есть у dohod.ru, но
только по уже отработанным выплатам: свежеобъявленную выплату dohod
проставляет с задержкой. Поэтому парсер ведёт собственный журнал: при
каждом прогоне он запоминает, какие выплаты видел, и для новой выплаты
фиксирует дату, когда она впервые появилась в источниках.

Отсюда и режим работы «механизма»: гоняем парсер по расписанию (например,
раз в сутки) — и по каждой новой рекомендации совета директоров в таблице
автоматически появляется дата её обнаружения, без ручного труда. Файл
реестра нужно хранить вместе с проектом: он накапливается прогон за прогоном.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict = {}
        self.changes: list = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("реестр обнаружений создаётся заново: %s", self.path)
            return
        try:
            with self.path.open(encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("реестр %s повреждён (%s), начинаю с чистого", self.path, exc)
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_описание": "Журнал первого обнаружения дивидендных выплат парсером. "
                         "Не удалять: на нём строится колонка «дата появления информации» "
                         "для выплат, по которым источники ещё не проставили дату объявления.",
            "_обновлён": dt.datetime.now().isoformat(timespec="seconds"),
            **{k: v for k, v in self.data.items() if not k.startswith("_")},
        }
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        tmp.replace(self.path)

    # ------------------------------------------------------------------ API
    def observe(self, ticker: str, event, today: dt.date) -> dt.date | None:
        """Регистрирует выплату и возвращает дату её первого обнаружения.

        Возвращает None, если выплата видна впервые в этом же прогоне и при
        этом уже состоялась — задним числом «обнаружение» не имеет смысла.
        """
        if event.record_date is None:
            return None
        key = event.record_date.isoformat()
        bucket = self.data.setdefault(ticker, {})
        entry = bucket.get(key)

        if entry is None:
            bucket[key] = {
                "first_seen": today.isoformat(),
                "dividend": event.dividend,
                "period": event.period,
                "status": event.status,
            }
            # выплата уже прошла к моменту первого прогона — дату обнаружения
            # не выдаём за дату публикации новости
            if event.record_date < today:
                return None
            self.changes.append("%s: новая выплата на %s, %s руб."
                                % (ticker, key, event.dividend))
            return today

        # выплата уже известна — проверяем, не поменялся ли размер
        old_div = entry.get("dividend")
        if old_div is not None and event.dividend is not None:
            base = max(abs(old_div), abs(event.dividend))
            if base and abs(old_div - event.dividend) / base > 0.001:
                self.changes.append(
                    "%s: по выплате на %s размер изменён с %s на %s руб."
                    % (ticker, key, old_div, event.dividend)
                )
                entry["dividend"] = event.dividend
        first = entry.get("first_seen")
        try:
            first_date = dt.date.fromisoformat(first) if first else None
        except ValueError:
            first_date = None
        # если выплата была впервые записана уже состоявшейся — даты нет
        if first_date and event.record_date < first_date:
            return None
        return first_date
