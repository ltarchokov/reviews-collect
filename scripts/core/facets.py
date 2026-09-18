"""
Разведка структуры источника до сбора.

Зачем. Раньше запрос «собери отзывы по свитерам» превращался в команду
моей догадкой: какое слово искать, добавлять ли фильтр по бренду, сколько
карточек брать. Догадка каждый раз своя, поэтому один и тот же вопрос
давал разный результат — это не сервис.

Теперь порядок другой: сначала инструмент показывает, что в источнике
реально есть — какие подкатегории, бренды, продавцы и сколько в них
отзывов, — и уточняющий вопрос задаётся по фактам. Пользователь
выбирает, и сбор идёт с точными параметрами. Одинаковый выбор даёт
одинаковую команду у всех.

Площадка реализует `explore(args) -> Facets` необязательно: у кого
структуры в выдаче нет, тот её и не покажет.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Facet:
    """Один разрез: имя разреза и его значения с объёмами."""

    title: str                       # «Предмет», «Бренд», «Продавец»
    values: list[tuple[str, int, int]]  # (значение, карточек, отзывов)
    flag: str = ""                   # аргумент CLI для сужения по нему

    def top(self, n: int) -> list[tuple[str, int, int]]:
        return sorted(self.values, key=lambda v: -v[2])[:n]


@dataclass
class Facets:
    """Результат разведки: сколько всего и какие разрезы доступны."""

    platform: str
    query: str
    total_cards: int = 0        # сколько карточек по запросу заявляет площадка
    scanned_cards: int = 0      # сколько мы реально разобрали
    facets: list[Facet] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Что спросить у пользователя дальше. Считается площадкой по данным
    # разведки, а не выбирается на глаз: сбор без бренда и категории даёт
    # смесь чужих товаров, а не анализ.
    ask: str = ""
    # Уточнять нечего — бренд и категория определились сами. Отдельное
    # поле, а не пустой ask: агент должен видеть «можно собирать», а не
    # гадать, почему вопроса нет.
    ready: str = ""


def aggregate(
    rows: list[dict],
    key: str,
    title: str,
    flag: str = "",
    name_of=None,
) -> Facet:
    """Сворачивает карточки по полю: сколько карточек и отзывов в каждом
    значении. `name_of` переводит числовой id в человеческое имя."""
    acc: dict = {}
    for r in rows:
        raw = r.get(key)
        if raw in (None, "", 0):
            continue
        label = name_of(raw) if name_of else str(raw)
        if not label:
            continue
        cards, reviews = acc.get(label, (0, 0))
        acc[label] = (cards + 1, reviews + int(r.get("feedbacks") or 0))
    return Facet(
        title=title,
        flag=flag,
        values=[(k, v[0], v[1]) for k, v in acc.items()],
    )


def render(f: Facets, limit: int = 12) -> str:
    """Разведка в текст. Формат один на все площадки, чтобы ответ
    не зависел от того, кто его печатает."""
    L = [f"Запрос «{f.query}» на {f.platform}", ""]
    if f.total_cards:
        L.append(f"Карточек по запросу: {f.total_cards:,}".replace(",", " "))
    L.append(f"Разобрано для разведки: {f.scanned_cards}")
    L.append("")

    for facet in f.facets:
        vals = facet.top(limit)
        if not vals:
            continue
        L.append(f"{facet.title} — {len(facet.values)} значений")
        if facet.flag:
            L.append(f"  сузить: {facet.flag}")
        L.append("")
        w = max(len(v[0]) for v in vals)
        w = min(max(w, 12), 46)
        L.append(f"  {'значение'.ljust(w)}  карточек   отзывов")
        for name, cards, reviews in vals:
            # Разделители тысяч: агент копирует цифры в варианты ответа
            # как напечатано, «91 761» читается, «91761» — нет.
            L.append(f"  {name[:w].ljust(w)}  {cards:>8}  {reviews:>9,}".replace(",", " "))
        if len(facet.values) > limit:
            hidden = len(facet.values) - limit
            L.append(f"  … и ещё {hidden}")
        L.append("")

    if f.ask:
        L.append("СПРОСИТЬ У ПОЛЬЗОВАТЕЛЯ")
        L.append("")
        L.append(f"  {f.ask}")
        L.append("")
    if f.ready:
        L.append("ГОТОВО К СБОРУ")
        L.append("")
        L.append(f"  {f.ready}")
        L.append("")

    for n in f.notes:
        L.append(f"> {n}")
    return "\n".join(L) + "\n"
