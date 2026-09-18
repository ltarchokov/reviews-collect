"""
Сводка по собранному датасету — одна на все площадки.

Главное, что здесь есть помимо распределения оценок: разделение негатива
на претензии к продукту и претензии к логистике, подделкам и сервису.
Без этого разделения бренд получает претензии за работу склада — на WB
это заметная доля негатива, и она не про продукт.

Сводка собирается в `Doc` (см. core/doc.py) и оттуда рендерится
в summary.md и в лист «Сводка» книги Excel. Считать дважды нельзя:
две сводки с разными цифрами хуже одной.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

from .doc import Doc


def _declared_dist(products: list[dict]) -> Counter:
    """Распределение оценок по всем карточкам целиком, если площадка его
    отдаёт. Нужно, чтобы измерить смещение собранной выборки, а не
    ограничиваться оговоркой «выборка смещена»."""
    total: Counter = Counter()
    for p in products:
        try:
            extra = json.loads(p.get("extra") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for k, v in (extra.get("valuation_distribution") or {}).items():
            if str(k).isdigit():
                total[int(k)] += int(v)
    return total

# Грубая первичная тематизация негатива по ключевым словам.
# Это первый срез, а не классификатор: одно и то же слово в разных
# категориях значит разное («протекает» — брак у бытовой техники и
# нормальная жалоба на упаковку у косметики). Перед выводами о продукте
# срез нужно просмотреть глазами.
BUCKETS = {
    "логистика и упаковка": (
        "доставк", "курьер", "пункт выдачи", "пвз", "упаков", "коробк",
        "помят", "мят", "порван", "вскрыт", "разбит", "задерж", "не привез",
        "потеря", "пересорт", "не тот товар", "другой товар", "долго шл",
    ),
    "подделка и оригинальность": (
        "подделк", "не оригинал", "неоригинал", "фальшив", "копи", "реплик",
        "паль", "сомнева", "отлича от оригинал",
    ),
    "продавец и сервис": (
        "продавец", "магазин", "поддержк", "возврат", "отмен", "чек",
        "гаранти", "не отвеча", "обман",
    ),
    "цена": ("цена", "дорог", "переплат", "скидк", "акци", "стоимость"),
}


def _bucket(text: str) -> str:
    t = text.lower()
    for name, words in BUCKETS.items():
        if any(w in t for w in words):
            return name
    return "о самом продукте"


# Второй слой разметки: конкретные дефекты внутри претензий к продукту.
# BUCKETS выше отвечает на вопрос «к кому претензия», этот — «в чём она».
# Темы неперекрывающиеся по смыслу, но один отзыв может попасть в
# несколько: жалоба «тонкая ткань и вся в катышках» — это две проблемы.
#
# Набор общий на все категории: одежда, косметика, лекарства. Ненужные
# темы просто дают ноль и в отчёт не попадают.
DEFECTS = {
    "размер: большемерит": ("большемер", "велик", "огромн", "больше размер", "широк"),
    "размер: маломерит": ("маломер", "маловат", "меньше размер", "мала", "узк", "жмет", "жмёт"),
    "ткань и материал": ("ткань", "тонк", "просвечива", "материал", "дешев", "дешёв", "синтетик", "колюч", "рыхл"),
    "катышки и износ": ("катыш", "скатал", "заката", "пилинг", "сыпется", "облез", "вылез", "лезет"),
    "после стирки": ("после стир", "растян", "деформ", "линя", "полин", "покрасил", "сел после", "село после"),
    "вид не как на фото": ("не как на фото", "цвет не", "другой цвет", "оттенок", "не соответствует фото"),
    "швы, фурнитура, брак": ("шов", "швы", "нитк", "дыр", "распус", "затяжк", "молни", "замок", "брак"),
    "запах": ("запах", "пахнет", "вонь"),
    "не помогает / нет эффекта": ("не помог", "без эффект", "нет эффект", "не подейств", "бесполезн"),
    "побочная реакция": ("аллерг", "сыпь", "зуд", "раздражен", "покраснен", "жжение", "побочк"),
    "вкус и приём": ("вкус", "горьк", "противн", "тошнот", "глотать"),
    "упаковка и комплект": ("упаков", "флакон", "дозатор", "крышк", "не хватает", "неполн"),
}


def _defects(reviews: list[dict]) -> list[tuple[str, int]]:
    """Сколько отзывов упоминает каждый дефект. Один отзыв может попасть
    в несколько тем — это не доли одного целого, а частоты упоминаний."""
    counts: Counter = Counter()
    for r in reviews:
        hay = " ".join(
            str(r.get(f) or "") for f in ("title", "text", "pros", "cons", "tags")
        ).lower()
        for name, words in DEFECTS.items():
            if any(w in hay for w in words):
                counts[name] += 1
    return [(n, c) for n, c in counts.most_common() if c]


def _quotes(reviews: list[dict], words: tuple, limit: int = 2) -> list[dict]:
    """Показательные цитаты по теме.

    Слово темы должно найтись в самом тексте отзыва, а не в полях
    «достоинства»/«недостатки»: иначе под темой «ткань» оказывается
    цитата, где про ткань ни слова — совпало в другом поле, а читателю
    показывается текст. Ранжируем по числу разных попавших слов:
    отзыв, где тема названа несколько раз, иллюстрирует её лучше.
    """
    scored = []
    for r in reviews:
        text = str(r.get("text") or "")
        if not (60 < len(text) < 260):
            continue
        low = text.lower()
        hits = sum(1 for w in words if w in low)
        if hits:
            scored.append((hits, len(text), r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [r for _, _, r in scored[:limit]]


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "—"


def build(
    platform: str,
    label: str,
    products: list[dict],
    reviews: list[dict],
    notes: list[str] | None = None,
    filter_report: list[str] | None = None,
    collected: int | None = None,
    unfiltered: list[dict] | None = None,
) -> Doc:
    """`collected` и `unfiltered` — набор до применения фильтров.

    Нужны отдельно от `reviews` по двум причинам, и обе про честность.

    Покрытие площадки: считать его от остатка после фильтров значит
    выдавать запрос «за последний год» за «площадка скрыла 88% отзывов».

    Таблица смещения: считать её по отфильтрованному — тавтология.
    С `--stars 1-3` в срезе окажется 0% пятёрок и расхождение
    −93.7 п.п., то есть фильтр, показанный как катастрофа.
    """
    if collected is None:
        collected = len(reviews)
    base = unfiltered if unfiltered is not None else reviews

    d = Doc()
    d.h1(f"{platform}: «{label}»")
    d.p(f"Собрано {datetime.now():%Y-%m-%d %H:%M}")
    d.li(f"Карточек в выборке: **{len(products)}**")
    d.li(f"Отзывов собрано: **{collected}**")
    if len(reviews) != collected:
        d.li(f"Осталось после фильтров: **{len(reviews)}**")

    # Покрытие: сколько отзывов есть на площадке против того, сколько
    # удалось взять. Замер потолка, критерий приёмки №5.
    declared = sum(int(p.get("reviews_declared") or 0) for p in products)
    if declared:
        d.li(
            f"Заявлено площадкой на этих карточках: **{declared}** "
            f"(взято {collected / declared * 100:.0f}%)"
        )
        if collected < declared * 0.9:
            d.li(
                f"⚠️ Площадка отдала {collected} из {declared}: часть отзывов "
                "она не показывает. Доли по собранному срезу считать можно "
                "только с оговоркой — см. раздел о смещении ниже."
            )

    if filter_report:
        d.p("Фильтры:")
        for line in filter_report:
            d.li(line)

    for n in notes or []:
        d.quote(n)

    if not reviews:
        return d

    rated = [int(r["rating"]) for r in reviews if str(r.get("rating") or "").isdigit()]
    if rated:
        d.li(f"Средняя оценка по собранному: **{sum(rated) / len(rated):.2f}**")
    with_text = sum(1 for r in reviews if len(str(r.get("text") or "")) > 20)
    d.li(f"С содержательным текстом (>20 симв.): **{with_text}**")

    if rated:
        d.h2("Распределение оценок")
        dist = Counter(rated)
        d.table(
            ["Оценка", "Отзывов", "Доля"],
            [[star, dist.get(star, 0), _pct(dist.get(star, 0), len(rated))] for star in (5, 4, 3, 2, 1)],
            align="lrr",
        )
        d.p(
            "Оценки на маркетплейсах смещены вверх — 90% пятёрок это норма. "
            "Смысл почти весь в 1–3 звёздах и в текстах, а не в среднем балле."
        )

        # Замер смещения выборки. Площадка отдаёт не нейтральный срез:
        # на WB негатив в отданной тысяче представлен примерно втрое
        # сильнее, чем на карточке целиком. Без этой таблицы распределение
        # из нашего CSV выдают за распределение по карточке, и картина
        # получается втрое хуже реальной.
        decl = _declared_dist(products)
        # Сравниваем с несфильтрованным срезом: фильтр по оценкам иначе
        # сам себя показывает как чудовищное смещение.
        base_rated = [
            int(r["rating"]) for r in base if str(r.get("rating") or "").isdigit()
        ]
        base_dist = Counter(base_rated)
        if decl and base_rated:
            dtot = sum(decl.values())
            d.h2("Смещение выборки")
            d.p(
                f"Площадка заявляет **{dtot}** отзывов на этих карточках, "
                f"собрать удалось **{len(base_rated)}** с оценкой"
                + (" (до фильтров). Сравнение форм:" if len(base_rated) != len(rated)
                   else ". Сравнение форм:")
            )
            rows = []
            worst = 0.0
            for star in (5, 4, 3, 2, 1):
                w = decl.get(star, 0) / dtot * 100
                o = base_dist.get(star, 0) / len(base_rated) * 100
                worst = max(worst, abs(o - w))
                rows.append([star, f"{w:.1f}%", f"{o:.1f}%", f"{o - w:+.1f} п.п."])
            d.table(["Оценка", "Карточки целиком", "Собранный срез", "Расхождение"], rows, align="lrrr")
            neg_d = sum(decl.get(s, 0) for s in (1, 2, 3)) / dtot * 100
            neg_o = sum(base_dist.get(s, 0) for s in (1, 2, 3)) / len(base_rated) * 100
            ratio = neg_o / neg_d if neg_d else 0
            d.p(
                f"Негатив (1–3): **{neg_d:.1f}%** по карточкам целиком против "
                f"**{neg_o:.1f}%** в собранном срезе"
                + (f" — представлен в **{ratio:.1f}×** сильнее." if ratio > 1.2 else ".")
            )
            if worst > 2:
                d.p(
                    "⚠️ **Распределение оценок из этого датасета нельзя выдавать "
                    "за распределение по карточке.** Для доли негатива берите левую "
                    "колонку — она от площадки. Собранный срез годится для текстов "
                    "и тем, а не для процентов."
                )

    neg = [
        r for r in reviews
        if str(r.get("rating") or "").isdigit() and int(r["rating"]) <= 3
    ]
    if neg:
        d.h2(f"Негатив (1–3 звезды): {len(neg)} отзывов")

        by_bucket = Counter(
            _bucket(" ".join(str(r.get(f) or "") for f in ("text", "cons", "title")))
            for r in neg
        )
        d.p("Про что негатив (грубая разметка по словам, требует проверки глазами):")
        d.table(
            ["Тема", "Отзывов", "Доля негатива"],
            [[name, n, _pct(n, len(neg))] for name, n in by_bucket.most_common()],
            align="lrr",
        )
        about_product = by_bucket.get("о самом продукте", 0)
        d.p(
            f"К самому продукту относится **{about_product}** из {len(neg)} "
            "негативных отзывов. Остальное — логистика, подделки, сервис и цена: "
            "это претензии не к бренду, и в выводы о продукте их тащить нельзя."
        )

        tags_neg = Counter(
            t for r in neg for t in str(r.get("tags") or "").split("|") if t
        )
        if tags_neg:
            d.p("Теги площадки на негативе:")
            for t, n in tags_neg.most_common(15):
                d.li(f"{t} — {n}")

        # В чём именно претензия. BUCKETS выше говорит, к кому она
        # относится; здесь — конкретный дефект, по которому можно
        # что-то сделать на производстве.
        defects = _defects(neg)
        if defects:
            d.h3("В чём претензия")
            d.table(
                ["Дефект", "Отзывов", "Доля негатива"],
                [[name, n, _pct(n, len(neg))] for name, n in defects[:12]],
                align="lrr",
            )
            d.p(
                "Один отзыв попадает в несколько тем, если жалоб несколько — "
                "это частоты упоминаний, а не доли одного целого."
            )

            # Пара цитат по трём главным темам: без них таблица
            # не даёт понять, о чём люди говорят на самом деле.
            d.h3("Цитаты по главным темам")
            for name, n in defects[:3]:
                d.p(f"**{name}** — {n} отзывов")
                for q in _quotes(neg, DEFECTS[name]):
                    d.quote(
                        f"[{q.get('rating')}★] {str(q.get('product_name'))[:48]}\n"
                        f"{str(q.get('text'))}"
                    )

        # Разброс по карточкам. Внутри одного бренда и одной категории
        # он бывает трёхкратным, и это указывает на конкретный артикул,
        # а не на «качество вообще».
        per: dict = defaultdict(lambda: [0, 0, ""])
        for r in reviews:
            k = str(r.get("product_id"))
            per[k][0] += 1
            if str(r.get("rating") or "").isdigit() and int(r["rating"]) <= 3:
                per[k][1] += 1
            per[k][2] = str(r.get("product_name") or "")
        cards = [
            (v[1] / v[0] * 100, v[0], v[1], v[2])
            for v in per.values()
            if v[0] >= 100  # на меньших объёмах доля шумит
        ]
        if len(cards) >= 3:
            cards.sort(reverse=True)
            d.h2("Разброс по карточкам")
            d.p(
                "Доля негатива по карточкам от 100 собранных отзывов. "
                "Разрыв внутри одной категории указывает на конкретный "
                "артикул, а не на бренд целиком."
            )
            rows = [
                [f"{share:.1f}%", f"{n} из {tot}", name[:52]]
                for share, tot, n, name in cards[:8]
            ]
            if len(cards) > 11:
                rows.append(["…", "", ""])
            rows += [
                [f"{share:.1f}%", f"{n} из {tot}", name[:52]]
                for share, tot, n, name in cards[-3:]
            ]
            d.table(["Негатива", "Отзывов", "Карточка"], rows, align="rrl")
            spread = cards[0][0] / cards[-1][0] if cards[-1][0] else 0
            if spread >= 2:
                d.p(
                    f"Худшая карточка хуже лучшей в **{spread:.1f} раза**. "
                    "Искать причину стоит в их различиях, а не в бренде."
                )

    tags_all = Counter(
        t for r in reviews for t in str(r.get("tags") or "").split("|") if t
    )
    if tags_all:
        d.h2("Все темы (авторазметка площадки)")
        for t, n in tags_all.most_common(20):
            d.li(f"{t} — {n}")

    by_month = Counter(r["date"][:7] for r in reviews if r.get("date"))
    if by_month:
        d.h2("Динамика по месяцам")
        d.bars([(m, by_month[m]) for m in sorted(by_month)[-18:]])
        no_date = sum(1 for r in reviews if not r.get("date"))
        if no_date:
            d.p(f"Без даты: {no_date} отзывов — в динамику не попали.")

    by_brand = Counter(r.get("brand") or "—" for r in reviews)
    if len(by_brand) > 1:
        d.h2("Отзывы по брендам в выборке")
        for b, n in by_brand.most_common(15):
            d.li(f"{b} — {n}")

    per_product: dict = defaultdict(int)
    for r in reviews:
        per_product[str(r.get("product_id"))] += 1
    by_id = {str(p.get("product_id")): p for p in products}
    if per_product:
        d.h2("Топ карточек по объёму собранного")
        for pid, n in sorted(per_product.items(), key=lambda x: -x[1])[:15]:
            p = by_id.get(pid, {})
            name = str(p.get("product_name") or "")[:60]
            d.li(f"{n} — {p.get('brand', '')} · {name} · {p.get('product_url', '')}")

    return d
