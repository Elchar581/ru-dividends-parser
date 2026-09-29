# -*- coding: utf-8 -*-
"""HTTP-клиент с кэшем на диске, повторами и мягкой деградацией по SSL.

Вынесен отдельно, чтобы при смене сайтов-источников не трогать сетевой слой.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 >= 2 / < 2
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Http:
    """Тонкая обёртка над requests.

    * повторы с экспоненциальной паузой;
    * пауза между запросами к одному хосту (вежливость к источникам);
    * кэш ответов на диске с TTL — повторный прогон не бомбит сайты;
    * если корпоративный прокси подменяет сертификат, один раз падаем
      на verify=False и пишем предупреждение (не молча).
    """

    def __init__(
        self,
        cache_dir: Path,
        cache_ttl_hours: float = 12.0,
        timeout: int = 30,
        delay: float = 0.7,
        user_agent: str = DEFAULT_UA,
        verify_ssl: bool = True,
        offline: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = cache_ttl_hours * 3600
        self.timeout = timeout
        self.delay = delay
        self.offline = offline
        self.verify_ssl = verify_ssl
        self._ssl_warned = False
        self._last_call: dict[str, float] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        retry = Retry(
            total=3,
            backoff_factor=1.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{h}.cache"

    def _read_cache(self, url: str) -> Optional[str]:
        p = self._cache_path(url)
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if not self.offline and age > self.cache_ttl:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_cache(self, url: str, text: str) -> None:
        try:
            self._cache_path(url).write_text(text, encoding="utf-8")
        except OSError as exc:  # кэш не критичен
            log.debug("не удалось записать кэш %s: %s", url, exc)

    # ------------------------------------------------------------------- http
    # Официальному API биржи частые запросы не мешают, в отличие от сайтов,
    # которые мы парсим как обычный браузер.
    FAST_HOSTS = ("iss.moex.com",)

    def _throttle(self, url: str) -> None:
        host = url.split("/")[2] if "://" in url else url
        delay = 0.05 if host in self.FAST_HOSTS else self.delay
        jitter = 0.0 if host in self.FAST_HOSTS else random.uniform(0, 0.4)
        last = self._last_call.get(host, 0.0)
        wait = delay + jitter - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.time()

    def get(self, url: str, use_cache: bool = True) -> Optional[str]:
        """Вернуть тело ответа или None. Исключения наружу не выпускаем:
        падение одного источника не должно ронять весь прогон."""
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                log.debug("кэш: %s", url)
                return cached
        if self.offline:
            log.warning("offline-режим, в кэше нет: %s", url)
            return None

        for verify in ([True, False] if self.verify_ssl else [False]):
            self._throttle(url)
            try:
                resp = self.session.get(url, timeout=self.timeout, verify=verify)
            except requests.exceptions.SSLError as exc:
                if verify:
                    if not self._ssl_warned:
                        log.warning(
                            "Проверка TLS-сертификата не прошла (%s). Похоже на "
                            "корпоративный прокси с подменой сертификата — "
                            "продолжаю без проверки. Чтобы убрать это предупреждение, "
                            "укажите корневой сертификат в переменной REQUESTS_CA_BUNDLE.",
                            str(exc)[:120],
                        )
                        self._ssl_warned = True
                    continue
                log.warning("SSL-ошибка %s: %s", url, exc)
                return None
            except requests.RequestException as exc:
                log.warning("сеть недоступна %s: %s", url, exc)
                return None

            if resp.status_code == 404:
                log.info("404: %s", url)
                return None
            if resp.status_code != 200:
                log.warning("HTTP %s: %s", resp.status_code, url)
                return None

            # requests при отсутствии charset в заголовке подставляет
            # ISO-8859-1 — на русских страницах это мусор. Автоопределение
            # запускаем только в этом случае: оно дорогое.
            declared = resp.encoding
            if not declared or declared.lower() in ("iso-8859-1", "ascii"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            text = resp.text
            if use_cache:
                self._write_cache(url, text)
            return text
        return None

    def get_json(self, url: str, use_cache: bool = True):
        import json

        text = self.get(url, use_cache=use_cache)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            log.warning("не JSON (%s): %s", exc, url)
            return None
