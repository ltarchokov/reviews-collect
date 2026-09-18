"""
Каноническая схема отзыва — одна на все площадки.

Смысл модуля: датасеты с разных площадок должны сходиться в один без ручной
склейки. Поэтому колонки и их порядок задаются здесь, а не в каждом парсере.
Площадка заполняет то, что реально отдала; остальное остаётся пустым, но
колонка всё равно на месте — иначе CSV не мержатся.

Категорийная специфика (размер покупателя на Lamoda, тип кожи на Золотом
Яблоке, просмотры на Отзовике) не выносится в отдельные колонки, иначе их
станет сорок и тридцать на любой конкретной площадке будут пустыми. Она
кладётся в `extra` как JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

# Порядок колонок в CSV. Менять только добавлением в конец —
# иначе поедут уже собранные датасеты.
FIELDS = [
    "platform",           # wb, ozon, lamoda, ...
    "query",              # запрос/задача, по которой собрано
    "brand",
    "product_name",
    "product_id",         # идентификатор карточки на площадке
    "product_url",
    "variant",            # цвет, объём, фасовка — к чему относится отзыв
    "review_id",
    "author_hash",        # обезличенный автор, см. anon()
    "rating",             # 1..5, пусто если площадка не даёт
    "date",               # YYYY-MM-DD
    "title",
    "text",
    "pros",               # достоинства (Отзовик, iRecommend, Ozon)
    "cons",               # недостатки
    "has_photo",
    "has_video",
    "media_urls",         # через |
    "votes_up",
    "votes_down",
    "views",
    "seller_answered",
    "verified_purchase",
    "tags",               # через |, авторазметка площадки
    "about",              # product | seller | service
    "source_url",         # прямая ссылка на отзыв, если есть
    "collected_at",
    "extra",              # JSON: категорийные поля площадки
]

REQUIRED = ["platform", "review_id"]

# Карточки, попавшие в выборку. Тоже общие колонки: по products.csv
# сверяют покрытие и решают, добирать ли объём числом карточек.
PRODUCT_FIELDS = [
    "platform",
    "query",
    "product_id",
    "brand",
    "product_name",
    "supplier",          # продавец, если площадка его отдаёт
    "product_url",
    "rating",
    "reviews_declared",  # сколько заявлено площадкой
    "reviews_collected", # сколько реально удалось взять — замер потолка
    "price_rub",
    "extra",
]

ABOUT = ("product", "seller", "service")

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
    # именительный падеж — встречается в группировках вида «Март 2024»
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DOTTED = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")
_RU = re.compile(r"(\d{1,2})\s+([а-яё]+)\s*(\d{4})?", re.IGNORECASE)


def anon(user_id: str | int | None) -> str:
    """Стабильный обезличенный id автора.

    По нему видно накрутку одним автором, но нельзя опознать человека.
    Правило действует на всех площадках без исключений: имя автора
    не сохраняется нигде.
    """
    if not user_id:
        return ""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


def normalize_date(raw, today: date | None = None) -> str:
    """Дату площадки — в YYYY-MM-DD. Не разобралось — пустая строка.

    Площадки отдают дату шестью разными способами, и это одно из мест,
    где парсеры молча врут: неразобранная дата превращается в пустоту,
    и период в выборке оказывается не тем, что просили. Поэтому здесь
    перебираются все известные форматы, а не только ISO.

    `today` — точка отсчёта для «сегодня»/«вчера». Такие даты делают
    повторный прогон невоспроизводимым, поэтому площадки, которые их
    отдают, лучше собирать сразу с абсолютной датой, если она есть в вёрстке.
    """
    if raw is None or raw == "":
        return ""

    today = today or date.today()

    # unix-таймстамп: числом или строкой из цифр
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        n = int(float(raw))
        if n > 10_000_000_000:  # миллисекунды
            n //= 1000
        if 946_684_800 < n < 4_102_444_800:  # 2000..2100, отсекаем мусор
            return datetime.fromtimestamp(n, timezone.utc).strftime("%Y-%m-%d")
        return ""

    s = str(raw).strip().lower()

    m = _ISO.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = _DOTTED.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""

    if s.startswith("сегодня"):
        return today.strftime("%Y-%m-%d")
    if s.startswith("вчера"):
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    m = _RU.search(s)
    if m and m.group(2) in RU_MONTHS:
        d = int(m.group(1))
        mo = RU_MONTHS[m.group(2)]
        # Год часто опущен — значит текущий, но если такая дата
        # оказывается в будущем, это прошлый год.
        y = int(m.group(3)) if m.group(3) else today.year
        if not m.group(3) and (mo, d) > (today.month, today.day):
            y -= 1
        if 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return ""


def row(platform: str, **kw) -> dict:
    """Строка отзыва со всеми колонками канонической схемы.

    Всё, что не передано, остаётся пустым. Всё, что передано сверх схемы,
    уезжает в `extra` — так площадка не может случайно расширить набор
    колонок и рассинхронизировать датасеты.
    """
    extra = dict(kw.pop("extra", None) or {})
    for key in list(kw):
        if key not in FIELDS:
            extra[key] = kw.pop(key)

    out = {f: "" for f in FIELDS}
    out["platform"] = platform
    out["about"] = "product"
    out["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    out.update({k: v for k, v in kw.items() if v is not None})

    out["date"] = normalize_date(out["date"])
    out["text"] = clean(out["text"])
    out["pros"] = clean(out["pros"])
    out["cons"] = clean(out["cons"])
    out["title"] = clean(out["title"])
    out["extra"] = json.dumps(extra, ensure_ascii=False) if extra else ""
    return out


def clean(text) -> str:
    """Схлопывает переводы строк и пробелы: в CSV многострочный текст
    читается плохо, а в анализе перевод строки ничего не значит."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def dedup(rows: list[dict]) -> tuple[list[dict], int]:
    """Убирает один и тот же отзыв, показанный на нескольких товарах.

    Площадки переиспользуют отзывы внутри семейства товаров: на Ютеке
    один отзыв висит на страницах разных фасовок препарата, и по
    `review_id` он не ловится — у каждой точки входа свой id. Замерено:
    48 собранных строк на АЦЦ оказались 36 настоящими отзывами.

    Ключ — автор, дата, оценка и начала текстов. Схлопнуть два разных
    отзыва он может только если один автор в один день поставил ту же
    оценку и написал то же самое; такой случай нам и не нужен.
    """
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        key = (
            r.get("platform"),
            r.get("author_hash"),
            r.get("date"),
            str(r.get("rating")),
            str(r.get("text"))[:80],
            str(r.get("pros"))[:80],
            str(r.get("cons"))[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, len(rows) - len(out)


def validate(rows: list[dict]) -> list[str]:
    """Проверки канонической схемы. Возвращает список проблем.

    Это критерий приёмки №3 из плана. Пустой список — схема сошлась.
    """
    problems: list[str] = []
    if not rows:
        return ["датасет пустой"]

    keys = set(rows[0].keys())
    if keys != set(FIELDS):
        missing = set(FIELDS) - keys
        extra = keys - set(FIELDS)
        if missing:
            problems.append(f"нет колонок: {', '.join(sorted(missing))}")
        if extra:
            problems.append(f"лишние колонки: {', '.join(sorted(extra))}")

    seen: set = set()
    dupes = 0
    no_required = 0
    bad_rating = 0
    bad_date = 0
    no_date = 0
    empty_content = 0

    for r in rows:
        if any(not r.get(f) for f in REQUIRED):
            no_required += 1

        rid = (r.get("platform"), r.get("review_id"))
        if rid in seen:
            dupes += 1
        seen.add(rid)

        rating = str(r.get("rating") or "")
        if rating and not (rating.isdigit() and 1 <= int(rating) <= 5):
            bad_rating += 1

        d = str(r.get("date") or "")
        if not d:
            no_date += 1
        elif not _ISO.match(d):
            bad_date += 1

        if not any(r.get(f) for f in ("text", "pros", "cons", "title")):
            empty_content += 1

        if r.get("about") and r["about"] not in ABOUT:
            problems.append(f"about={r['about']!r} — допустимы {ABOUT}")

    n = len(rows)
    if no_required:
        problems.append(f"без обязательных полей: {no_required} из {n}")
    if dupes:
        problems.append(f"дубли review_id: {dupes}")
    if bad_rating:
        problems.append(f"оценка вне 1..5: {bad_rating}")
    if bad_date:
        problems.append(f"дата не в формате YYYY-MM-DD: {bad_date}")

    # Пустая дата и пустой текст — не ошибка схемы, но если их много,
    # почти наверняка перепутано поле при разборе. Это критерий №4.
    if no_date > n * 0.1:
        problems.append(f"без даты: {no_date} из {n} — проверь разбор даты")
    if empty_content > n * 0.5:
        problems.append(
            f"без текста, достоинств и недостатков: {empty_content} из {n} "
            "— вероятно, перепутаны поля при разборе"
        )

    return problems
