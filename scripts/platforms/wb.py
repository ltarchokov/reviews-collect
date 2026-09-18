"""
Wildberries — тир A: открытый JSON API, работает без браузера.

Ограничение платформы: WB отдаёт максимум ~1000 последних отзывов
на карточку, сколько бы их там ни было. Параметры пагинации эндпоинт
игнорирует. Следствия:

  1. Объём набирается вширь — числом карточек, а не глубиной по одной.
  2. Выборка смещена к свежим отзывам. По ней нельзя строить динамику
     «как менялось отношение за два года» — в срезе будут последние месяцы.

Карточка ≠ артикул. Один `root` объединяет все варианты — объёмы, цвета,
фасовки. Отзывы приходят на карточку целиком, поле variant показывает,
к какому варианту относится отзыв. Дубли по root схлопываются, иначе одни
и те же отзывы качались бы по нескольку раз.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from core.facets import Facet, Facets, aggregate
from core.http import Client, log
from core.schema import anon, row

# Дерево предметов WB: id → название. Без него разведка показывала бы
# «subjectId 160» вместо «Свитеры», то есть была бы бесполезна.
SUBJECT_TREE_URLS = [
    "https://static-basket-01.wbbasket.ru/vol0/data/subject-base.json",
    "https://static-basket-01.wb.ru/vol0/data/subject-base.json",
]
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "wb-subjects.json"

NAME = "wb"
TITLE = "Wildberries"
TIER = "A"

# 429 на поиске вероятностный, а не привязанный к пути: одна и та же пара
# (путь, TLS-профиль) проходит в 1–3 случаях из 10 — замерено. Поэтому пути
# не «ищутся рабочие», а перебираются как билеты в лотерее: путей семь,
# профилей шесть, попыток на путь три — вероятность не пробиться вообще
# около одного процента.
#
# Если однажды перестанут отвечать все — дописать новый путь, подсмотрев
# в DevTools на wildberries.ru: вкладка Network, фильтр `search`.
SEARCH_PATHS = [
    "https://search.wb.ru/exactmatch/ru/male/v5/search",
    "https://search.wb.ru/exactmatch/ru/female/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v8/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
]
SEARCH_TRIES_PER_PATH = 3
# Эндпоинты отзывов и карточек защиты не имеют и стабильны.
FEEDBACK_HOSTS = ["feedbacks1.wb.ru", "feedbacks2.wb.ru"]
CARD_URL = "https://card.wb.ru/cards/v4/detail"

DEST = "-1257786"  # регион выдачи: Москва

HEADERS = {
    "Accept": "*/*",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


def subjects(client: Client) -> dict[int, str]:
    """id предмета → название. Дерево меняется редко, кешируется на диск."""
    if CACHE.exists():
        try:
            return {int(k): v for k, v in json.loads(CACHE.read_text()).items()}
        except (ValueError, OSError):
            pass

    tree = None
    for url in SUBJECT_TREE_URLS:
        tree = client.json(url, tries=2)
        if tree:
            break
    if not tree:
        log("дерево предметов недоступно — разрез по подкатегориям будет по id")
        return {}

    flat: dict[int, str] = {}

    def walk(nodes):
        for n in nodes or []:
            if isinstance(n, dict) and n.get("id") and n.get("name"):
                flat[int(n["id"])] = str(n["name"])
                walk(n.get("childs"))

    walk(tree)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(
        json.dumps({str(k): v for k, v in flat.items()}, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"дерево предметов загружено: {len(flat)} значений")
    return flat


def add_args(ap) -> None:
    ap.add_argument("--subject", help="сузить до подкатегории по названию (см. explore)")
    ap.add_argument("--supplier", help="сузить до продавца по названию")
    ap.add_argument("--nm", nargs="+", type=int, help="конкретные артикулы вместо поиска")
    ap.add_argument("--products", type=int, default=25, help="сколько карточек обработать (по умолчанию 25)")
    ap.add_argument("--pages", type=int, default=1, help="страниц выдачи по 100 товаров (по умолчанию 1)")
    ap.add_argument("--brand", help="оставить только этот бренд (без учёта регистра)")
    ap.add_argument("--min-feedbacks", type=int, default=1, help="пропускать карточки с числом отзывов меньше указанного")


class WB:
    def __init__(self):
        # Ниже 0.4с WB начинает отдавать 429 ещё и по частоте.
        self.c = Client(headers=HEADERS, delay=(0.6, 1.2))
        self.total = 0  # сколько карточек по запросу заявляет площадка

    def _search_page(self, query: str, page: int) -> list[dict]:
        """Одна страница выдачи. Перебирает пути, внутри пути — профили."""
        for path in SEARCH_PATHS:
            url = (
                f"{path}?appType=1&curr=rub&dest={DEST}&page={page}"
                f"&query={quote(query)}&resultset=catalog&sort=popular"
                f"&spp=30&suppressSpellcheck=false"
            )
            data = self.c.json(url, tries=SEARCH_TRIES_PER_PATH)
            if not data:
                continue
            items = (
                data.get("products")
                or (data.get("data") or {}).get("products")
                or []
            )
            if items:
                self.total = self.total or int(data.get("total") or 0)
                log(f"страница {page}: {len(items)} товаров ({path.rsplit('/ru/', 1)[1]})")
                return items
        return []

    def search(self, query: str, pages: int = 1) -> list[dict]:
        found: list[dict] = []
        for page in range(1, pages + 1):
            items = self._search_page(query, page)
            if not items:
                log(f"страница {page}: выдача не отдалась, останавливаюсь")
                break
            found.extend(items)
            self.c.sleep()
        return found

    def cards(self, nm_ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(nm_ids), 50):
            chunk = ",".join(str(n) for n in nm_ids[i : i + 50])
            data = self.c.json(f"{CARD_URL}?appType=1&curr=rub&dest={DEST}&nm={chunk}")
            if data:
                out.extend(data.get("products") or [])
            self.c.sleep()
        return out

    def feedbacks(self, root: int) -> dict | None:
        """Отзывы по imtId (поле root карточки). Отдаёт до ~1000 последних."""
        for host in FEEDBACK_HOSTS:
            data = self.c.json(f"https://{host}/feedbacks/v2/{root}", tries=2)
            if data and data.get("feedbacks"):
                return data
        return None


def _product(p: dict, query: str) -> dict:
    sizes = p.get("sizes") or [{}]
    price = ((sizes[0].get("price") or {}).get("product") or 0) / 100
    return {
        "platform": NAME,
        "query": query,
        "product_id": p.get("root"),
        "brand": p.get("brand") or "",
        "product_name": p.get("name") or "",
        "supplier": p.get("supplier") or "",
        "product_url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx",
        "rating": p.get("reviewRating") or p.get("rating") or "",
        "reviews_declared": p.get("feedbacks") or 0,
        "reviews_collected": 0,
        "price_rub": round(price, 2),
        "extra": "",
        # не уезжают в CSV, нужны только внутри сбора
        "_nm_id": p.get("id"),
        "_subject_id": p.get("subjectId"),
    }


def _reviews(data: dict, product: dict, query: str) -> list[dict]:
    out = []
    for f in data.get("feedbacks") or []:
        votes = f.get("votes") or {}
        # Ссылка строится из артикула самого отзыва, а не карточки. Одна
        # карточка (root) держит все варианты, и отзывы на ней приходят
        # общей пачкой: ссылка на артикул из выдачи повела бы на другой
        # объём или цвет, чем тот, о котором отзыв.
        nm = f.get("nmId") or product.get("_nm_id")
        out.append(
            row(
                NAME,
                query=query,
                brand=product["brand"],
                product_name=product["product_name"],
                product_id=product["product_id"],
                product_url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
                variant=f.get("color") or "",
                review_id=f.get("id") or "",
                author_hash=anon(f.get("globalUserId")),
                rating=f.get("productValuation") or "",
                date=f.get("createdDate") or "",
                text=f.get("text") or "",
                pros=f.get("pros") or "",
                cons=f.get("cons") or "",
                has_photo=bool(f.get("photo") or f.get("photos")),
                has_video=bool(f.get("video")),
                votes_up=votes.get("pluses") or 0,
                votes_down=votes.get("minuses") or 0,
                seller_answered=bool(f.get("answer")),
                tags="|".join(f.get("bables") or []),
                about="product",
                # WB исключает часть отзывов из расчёта рейтинга — признак
                # накрутки или нарушения правил. В канонической схеме
                # такого поля нет, уезжает в extra.
                excluded_from_rating=bool(
                    (f.get("excludedFromRating") or {}).get("isExcluded")
                ),
                nm_id=f.get("nmId") or product.get("_nm_id"),
            )
        )
    return out


def explore(args) -> Facets:
    """Что есть в источнике по запросу: подкатегории, бренды, продавцы.

    Ничего не собирает — только структуру, чтобы уточняющий вопрос
    задавался по фактам, а не по догадке.
    """
    wb = WB()
    pages = max(args.pages, 2)  # одной страницы мало, чтобы увидеть разброс
    raw = wb.search(args.query, pages)
    if not raw:
        return Facets(
            platform=TITLE,
            query=args.query,
            notes=["Не пробились к выдаче. Повторить или проверить SEARCH_PATHS."],
        )

    names = subjects(wb.c)
    # Схлопываем варианты одной карточки: иначе брендовый разрез
    # считает объёмы по числу цветов, а не товаров.
    seen, cards = set(), []
    for p in raw:
        if p.get("root") and p["root"] not in seen:
            seen.add(p["root"])
            cards.append(p)

    subj = aggregate(
        cards, "subjectId", "Подкатегория", "--subject «название»",
        name_of=lambda i: names.get(int(i), f"предмет {i}"),
    )
    brand = aggregate(cards, "brand", "Бренд", "--brand «название»")
    supp = aggregate(cards, "supplier", "Продавец", "--supplier «название»")

    # Порядок разрезов и вопрос зависят от того, чего в запросе не хватает.
    # Сбор без бренда и категории — это смесь чужих товаров, а не анализ,
    # поэтому «собрать всё» вариантом не предлагается никогда.
    total_rev = sum(v[2] for v in brand.values) or 1
    top = sorted(brand.values, key=lambda v: -v[2])[:1]
    brand_share = (top[0][2] / total_rev) if top else 0
    brand_named = bool(getattr(args, "brand", None))
    subject_named = bool(getattr(args, "subject", None))

    if brand_named or brand_share > 0.7:
        # Бренд уже определён — запросом или тем, что он один в выдаче.
        facets = [subj, brand, supp]
        owner = top[0][0] if top else "бренд"
        ask = (
            f"Бренд определён: {owner}. Выбери продуктовую категорию из списка "
            f"выше — их {len(subj.values)}. Сбор идёт по одной категории за "
            "прогон, чтобы датасет был про один товар, а не про весь бренд."
        ) if not subject_named else ""
    else:
        # Брендов много — сначала выбор бренда, категории потом.
        facets = [brand, subj, supp]
        ask = (
            f"В выдаче {len(brand.values)} брендов — выбери, чей ассортимент "
            "нужен. Сбор без бренда смешает чужие товары в один датасет, "
            "поэтому этот шаг не пропускается. После бренда выберем категорию."
        )

    return Facets(
        platform=TITLE,
        query=args.query,
        total_cards=wb.total,
        scanned_cards=len(cards),
        facets=facets,
        ask=ask,
        notes=[
            f"Разведка смотрит начало выдачи — {pages} стр., {len(cards)} карточек "
            "после схлопывания вариантов, — а не весь каталог: разрезы показывают "
            "структуру запроса, а не полные объёмы площадки.",
            "Столбец «отзывов» — сколько их заявлено на карточках. Забрать "
            "получится не больше тысячи с каждой.",
        ],
    )


def _narrow(products: list[dict], args, names: dict[int, str]) -> list[dict]:
    """Сужение по подкатегории и продавцу — по названию, без учёта регистра."""
    if getattr(args, "subject", None):
        needle = args.subject.lower()
        products = [
            p for p in products
            if needle in names.get(int(p.get("_subject_id") or 0), "").lower()
        ]
        log(f"фильтр по подкатегории «{args.subject}»: осталось {len(products)}")
    if getattr(args, "supplier", None):
        needle = args.supplier.lower()
        products = [p for p in products if needle in (p.get("supplier") or "").lower()]
        log(f"фильтр по продавцу «{args.supplier}»: осталось {len(products)}")
    return products


def collect(args) -> tuple[list[dict], list[dict], list[str]]:
    label = args.query or (f"nm-{args.nm[0]}" if args.nm else "")
    wb = WB()

    # Сбор без бренда даёт смесь ассортимента разных производителей:
    # по запросу «свитер» в выдаче 112 брендов. Такой датасет не про
    # продукт и не про бренд — предупреждаем до траты времени.
    no_brand = not getattr(args, "brand", None) and not args.nm
    if no_brand:
        log("ВНИМАНИЕ: бренд не задан — в датасет попадут товары разных брендов.")
        log("           Сначала посмотри `--explore`, затем сузь `--brand`.")

    raw = wb.cards(args.nm) if args.nm else wb.search(args.query, args.pages)
    if not raw:
        log(f"ничего не найдено. {wb.c.stats()}")
        return [], [], [
            "Не пробились к выдаче ни одним путём. Если повторный запуск "
            "не помогает — вероятно, WB сменил пути поиска, см. SEARCH_PATHS."
        ]

    products = [_product(p, label) for p in raw]

    if args.brand:
        needle = args.brand.lower()
        products = [p for p in products if needle in p["brand"].lower()]
        log(f"фильтр по бренду «{args.brand}»: осталось {len(products)}")

    if getattr(args, "subject", None) or getattr(args, "supplier", None):
        products = _narrow(products, args, subjects(wb.c))

    # Схлопываем варианты одной карточки — иначе тянем одни и те же отзывы.
    seen: set = set()
    unique = []
    for p in products:
        if p["product_id"] and p["product_id"] not in seen:
            seen.add(p["product_id"])
            unique.append(p)
    products = [p for p in unique if p["reviews_declared"] >= args.min_feedbacks]
    products.sort(key=lambda p: -p["reviews_declared"])
    products = products[: args.products]

    log(f"карточек к обработке: {len(products)}")
    if not products:
        # Пустой результат после сужения почти всегда значит, что выбранная
        # пара «бренд + категория» в этой выдаче не существует. Молчаливый
        # ноль тут бесполезен — говорим, что реально есть, чтобы человек
        # мог поправить выбор, а не гадать.
        names = subjects(wb.c)
        avail = Counter()
        for p in raw:
            b = (p.get("brand") or "").lower()
            if getattr(args, "brand", None) and args.brand.lower() not in b:
                continue
            nm = names.get(int(p.get("subjectId") or 0), "")
            if nm:
                avail[nm] += 1
        note = "После сужения не осталось ни одной карточки."
        if avail:
            have = ", ".join(f"{k} ({v})" for k, v in avail.most_common(12))
            note += (
                f" У бренда «{args.brand}» по запросу «{label}» есть другие "
                f"категории: {have}. Выбери из них или смени запрос."
            )
        elif getattr(args, "brand", None):
            note += (
                f" Бренда «{args.brand}» в выдаче по запросу «{label}» нет "
                "вообще — проверь написание или зайди с другого запроса."
            )
        log(note)
        return [], [], [note]

    reviews: list[dict] = []
    for i, p in enumerate(products, 1):
        data = wb.feedbacks(p["product_id"])
        if not data:
            log(f"[{i}/{len(products)}] {p['brand']} · {p['product_name'][:40]} — отзывов нет")
            wb.c.sleep()
            continue

        rows = _reviews(data, p, label)
        reviews.extend(rows)
        p["reviews_collected"] = len(rows)
        # Число с карточки отзывов точнее, чем из выдачи поиска.
        p["reviews_declared"] = data.get("feedbackCount") or p["reviews_declared"]
        p["rating"] = data.get("valuation") or p["rating"]
        # Распределение оценок по всей карточке. Нужно, чтобы посчитать,
        # насколько отданная нам тысяча смещена относительно целого:
        # замерено, что негатив в срезе представлен втрое сильнее.
        dist = data.get("valuationDistribution") or {}
        if dist:
            p["extra"] = json.dumps(
                {"valuation_distribution": dist}, ensure_ascii=False
            )

        log(
            f"[{i}/{len(products)}] {p['brand']} · {p['product_name'][:40]} "
            f"— {len(rows)} из {p['reviews_declared']}"
        )
        wb.c.sleep()

    log(wb.c.stats())
    notes = [
        "WB отдаёт максимум ~1000 последних отзывов на карточку. Объём набирается "
        "числом карточек (`--products`), а не глубиной по одной. Выборка смещена "
        "к свежим отзывам — динамику за длинный период по ней строить нельзя.",
        f"Сеть: {wb.c.stats()}. Высокая доля отбитых запросов — норма для "
        "поиска WB, он вероятностно сбрасывает нагрузку.",
    ]
    return products, reviews, notes
