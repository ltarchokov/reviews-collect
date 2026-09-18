"""
Сетевой слой: сессия, ретраи, троттлинг, ротация TLS-отпечатков.

Почему curl_cffi, а не requests. У requests свой TLS-отпечаток (порядок
шифров, набор расширений), и площадки его узнают. Замерено на WB: при
одинаковых заголовках curl получает 200, requests — 403. Заголовки тут
ни при чём, проверены все комбинации. curl_cffi подставляет отпечаток
настоящего браузера, и запрос перестаёт отличаться от браузерного.

Почему Chrome исключён из профилей по умолчанию. На card.wb.ru
chrome131 и chrome136 стабильно отдают 403, а firefox, safari, edge и tor
— 200. Скраперы поголовно прикидываются Chrome, его отпечаток и внесли
в список. Прикидываться Safari сейчас надёжнее.

Почему ретраи агрессивные, а не с длинным откатом. На search.wb.ru 429
вероятностный: один и тот же путь с одним и тем же профилем проходит
в 1–3 случаях из 10. Это сброс нагрузки, а не блокировка, и лечится
он упорством, а не паузами. Долгий откат тут только теряет время.
"""

from __future__ import annotations

import gzip
import json
import random
import sys
import time

from curl_cffi import requests as cr

# Порядок важен: первым идёт то, что на практике проходит чаще.
PROFILES = [
    "firefox135",
    "safari184",
    "edge101",
    "safari260",
    "firefox133",
    "tor145",
]


def log(msg: str) -> None:
    """Прогресс идёт в stderr, чтобы stdout можно было пайпить."""
    print(f"  {msg}", file=sys.stderr, flush=True)


class Client:
    """Сессия с ротацией TLS-отпечатков и ретраями.

    delay — пауза между успешными запросами. Ниже 0.4с большинство
    площадок начинает отдавать 429; значение подбирается на площадку
    и живёт в её модуле.
    """

    def __init__(
        self,
        headers: dict | None = None,
        delay: tuple[float, float] = (0.6, 1.2),
        profiles: list[str] | None = None,
        tries: int = 8,
    ):
        self.headers = {
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            **(headers or {}),
        }
        self.delay = delay
        self.profiles = list(profiles or PROFILES)
        self.tries = tries
        self.blocked = False   # взведён после исчерпания ретраев
        self.attempts = 0      # статистика: сколько запросов реально ушло
        self.rejected = 0      # сколько из них отбито площадкой

    # --- низкий уровень -------------------------------------------------

    def _fetch(self, url: str, tries: int | None = None, **kw):
        """Перебирает профили, пока не получит 200 или не кончатся попытки."""
        budget = tries if tries is not None else self.tries
        last = None

        for attempt in range(budget):
            profile = self.profiles[attempt % len(self.profiles)]
            self.attempts += 1
            try:
                r = cr.get(
                    url,
                    impersonate=profile,
                    headers=self.headers,
                    timeout=kw.pop("timeout", 30),
                    **kw,
                )
            except Exception as e:
                log(f"сеть: {type(e).__name__} ({profile}), попытка {attempt + 1}/{budget}")
                time.sleep(1.5)
                continue

            if r.status_code == 200:
                return r

            last = r.status_code
            self.rejected += 1

            # 403 — отпечаток не понравился: смысла ждать нет, сразу
            # следующий профиль. 429/503 — вероятностный сброс нагрузки:
            # короткая пауза и снова.
            if r.status_code == 403:
                continue
            if r.status_code in (429, 503):
                time.sleep(min(0.8 + 0.4 * attempt, 4.0))
                continue

            return None  # 404 и прочее ретраить бессмысленно

        self.blocked = True
        if last:
            log(f"не пробились за {budget} попыток, последний код {last}")
        return None

    def sleep(self) -> None:
        time.sleep(random.uniform(*self.delay))

    # --- то, чем пользуются парсеры -------------------------------------

    def json(self, url: str, tries: int | None = None, **kw) -> dict | list | None:
        """JSON по адресу. Разжимает gzip, приходящий без заголовка."""
        r = self._fetch(url, tries=tries, **kw)
        if r is None:
            return None

        raw = r.content
        if raw[:2] == b"\x1f\x8b":  # часть площадок отдаёт gzip без Content-Encoding
            try:
                raw = gzip.decompress(raw)
            except OSError:
                return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def text(self, url: str, tries: int | None = None, **kw) -> str | None:
        """HTML по адресу — для площадок тира B."""
        r = self._fetch(url, tries=tries, **kw)
        return None if r is None else r.text

    def stats(self) -> str:
        if not self.attempts:
            return "запросов не было"
        share = self.rejected / self.attempts * 100
        return (
            f"запросов {self.attempts}, отбито площадкой {self.rejected} "
            f"({share:.0f}%)"
        )
