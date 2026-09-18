"""
Шесть фильтров, которые закрывают всю колонку «Дополнительно» из задания.

В задании двадцать два запроса, но это не двадцать два скрипта: одиннадцать
сборщиков и один набор фильтров, одинаковый на всех площадках. Поэтому
фильтры живут здесь, а не в парсерах, и подключаются к любому из них
одной строкой add_args(parser).

Соответствие запросам:
    --stars 1-3       негативные отзывы, классификация причин негатива
    --media photo     отзывы с фото и видео
    --contains        «маломерит», «протекает», «до и после», «не рекомендую»,
                      упоминания цены, оригинальности, доставки, гарантии
    --period          «за [период]» — во всех одиннадцати строках задания
    --min-len         развёрнутые отзывы от N символов
    --about seller    отзывы о продавце / о сервисе аптеки, отдельно от товара
"""

from __future__ import annotations

import calendar
import re

from .schema import ABOUT


def add_args(ap) -> None:
    """Общая группа аргументов. Подключается в каждом парсере."""
    g = ap.add_argument_group("фильтры отзывов")
    g.add_argument(
        "--stars",
        help="оценки: 1-3 (диапазон), 1,2 (перечень) или 5",
    )
    g.add_argument(
        "--media",
        choices=("photo", "video", "any"),
        help="только отзывы с фото / с видео / с любым медиа",
    )
    g.add_argument(
        "--contains",
        help="ключевые слова через запятую, без учёта регистра",
    )
    g.add_argument(
        "--period",
        help="период: 2024-01..2025-06, 2024-01-15..2025-06-30, "
             "2024-01.. (открытый справа) или ..2025-06",
    )
    g.add_argument(
        "--min-len",
        type=int,
        help="только отзывы, где текст длиннее N символов",
    )
    g.add_argument(
        "--about",
        choices=ABOUT,
        help="о чём отзыв: о товаре, о продавце или о сервисе площадки",
    )


def parse_stars(spec: str) -> set[int]:
    """«1-3» → {1,2,3}; «1,2» → {1,2}; «5» → {5}."""
    out: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return {s for s in out if 1 <= s <= 5}


def parse_period(spec: str) -> tuple[str, str]:
    """Период → пара границ YYYY-MM-DD включительно.

    Месяц без дня доводится до реальных границ месяца, а не до «-01»
    с обеих сторон: иначе `--period 2024-01..2024-03` молча отрезал бы
    февраль и март.
    """
    raw = str(spec).strip()
    left, _, right = raw.partition("..")
    if not _:
        left = right = raw  # одна дата — период в один день/месяц

    def edge(value: str, end: bool) -> str:
        value = value.strip()
        if not value:
            return "9999-12-31" if end else "0000-01-01"
        m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$", value)
        if not m:
            raise ValueError(f"не понимаю дату {value!r} в --period")
        y = int(m.group(1))
        if m.group(2) is None:
            return f"{y}-12-31" if end else f"{y}-01-01"
        mo = int(m.group(2))
        if m.group(3) is None:
            d = calendar.monthrange(y, mo)[1] if end else 1
        else:
            d = int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"

    return edge(left, False), edge(right, True)


def _haystack(r: dict) -> str:
    return " ".join(
        str(r.get(f) or "") for f in ("title", "text", "pros", "cons", "tags")
    ).lower()


def apply(rows: list[dict], args) -> tuple[list[dict], list[str]]:
    """Прогоняет датасет через все заданные фильтры.

    Возвращает отфильтрованные строки и отчёт: какой фильтр сколько отрезал.
    Отчёт обязателен — фильтр, срезавший датасет до нуля молча, выглядит
    как «на площадке нет отзывов», а это разные вещи.
    """
    report: list[str] = []
    kept = list(rows)

    def step(name: str, predicate) -> None:
        nonlocal kept
        before = len(kept)
        kept = [r for r in kept if predicate(r)]
        report.append(f"{name}: {before} → {len(kept)}")

    if getattr(args, "stars", None):
        allowed = parse_stars(args.stars)
        if not allowed:
            raise ValueError(f"не понимаю --stars {args.stars!r}")
        step(
            f"оценки {args.stars}",
            lambda r: str(r.get("rating") or "").isdigit()
            and int(r["rating"]) in allowed,
        )

    if getattr(args, "period", None):
        start, end = parse_period(args.period)
        # Отзывы без даты выбрасываются: оставить их — значит соврать
        # про период, а датой их не наделишь.
        step(
            f"период {start}..{end}",
            lambda r: bool(r.get("date")) and start <= r["date"] <= end,
        )

    if getattr(args, "media", None):
        if args.media == "photo":
            step("с фото", lambda r: bool(r.get("has_photo")))
        elif args.media == "video":
            step("с видео", lambda r: bool(r.get("has_video")))
        else:
            step(
                "с медиа",
                lambda r: bool(r.get("has_photo")) or bool(r.get("has_video")),
            )

    if getattr(args, "contains", None):
        words = [w.strip().lower() for w in args.contains.split(",") if w.strip()]
        if words:
            step(
                f"упоминания ({len(words)} слов)",
                lambda r: any(w in _haystack(r) for w in words),
            )

    if getattr(args, "min_len", None):
        step(
            f"текст длиннее {args.min_len}",
            lambda r: len(str(r.get("text") or "")) >= args.min_len,
        )

    if getattr(args, "about", None):
        step(f"о {args.about}", lambda r: r.get("about") == args.about)

    return kept, report
