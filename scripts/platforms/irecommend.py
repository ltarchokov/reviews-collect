"""
iRecommend — тир B: серверный HTML (Drupal) с микроразметкой schema.org.

⚠️ НЕ ПРОВЕРЕН НА ЖИВЫХ ДАННЫХ. Разведка структуры пройдена и надёжна,
но на момент написания сайт стоял на обслуживании с отключённой базой:
любой адрес, включая robots.txt, отдавал 521 с телом
`<body class="in-maintenance db-offline">`. Критерии приёмки 2–7
не пройдены. Перед использованием — прогнать и сверить пять отзывов
с сайтом глазами.

Структура, снятая разведкой:

    /srch?query=<запрос>        поиск: отдаёт ссылки на отзывы и товары
    /taxonomy/term/<id>         страница товара, пагинация ?page=N
    /content/<slug>             отдельный отзыв с полным текстом

**Главная ловушка разбора.** На странице отзыва класс `.created`
встречается 23 раза: это даты чужих отзывов из блока «похожие».
Взять их — получить случайные даты при заполненных остальных полях,
чего ни один автотест не заметит. Поэтому все поля берутся строго
внутри контейнера `div.reviewBlock[itemtype*="Review"]`, а дата —
из микроразметки `datePublished`, а не из вёрстки.

**Полный текст только на странице отзыва.** В выдаче и на карточке
товара лежат обрезанные тизеры, поэтому за каждым отзывом нужен
отдельный запрос: задание просит полный текст, достоинства
и недостатки. Отсюда `--max-reviews` как предохранитель.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from core.http import Client, log
from core.schema import row

NAME = "irecommend"
TITLE = "iRecommend"
TIER = "B"

SITE = "https://irecommend.ru"
HEADERS = {"Referer": SITE + "/"}

REVIEW_BOX = 'div.reviewBlock'


def add_args(ap) -> None:
    ap.add_argument("--term", nargs="+", help="id страниц товаров вместо поиска")
    ap.add_argument("--products", type=int, default=5, help="сколько товаров обработать")
    ap.add_argument("--max-reviews", type=int, default=100, help="предохранитель: отзывов на товар (по запросу на каждый)")
    ap.add_argument("--max-pages", type=int, default=20, help="предохранитель: страниц списка на товар")


def _prop(node, prop: str) -> str:
    n = node.css_first(f'[itemprop="{prop}"]')
    if n is None:
        return ""
    return (n.attributes.get("content") or n.text(strip=True) or "").strip()


def _strip_label(text: str, *labels: str) -> str:
    """Убирает подпись блока: разметка кладёт «Достоинства» внутрь значения."""
    t = text.strip()
    for lab in labels:
        if t.startswith(lab):
            return t[len(lab):].strip(" : ")
    return t


class IRecommend:
    def __init__(self):
        # Площадка небольшая, ходим заметно медленнее маркетплейсов.
        self.c = Client(headers=HEADERS, delay=(1.5, 2.5))

    def alive(self) -> str | None:
        """Проверка до сбора: сайт может стоять на обслуживании.

        Без неё обслуживание выглядит как «отзывов не найдено», и это
        худший вид ошибки — тихий и правдоподобный.
        """
        html = self.c.text(f"{SITE}/robots.txt", tries=2)
        if html is None:
            return "сайт не отвечает (проверь robots.txt вручную)"
        if "in-maintenance" in html or "db-offline" in html:
            return "сайт на обслуживании, база отключена"
        return None

    def search(self, query: str, limit: int) -> list[str]:
        """Страницы товаров по запросу."""
        html = self.c.text(f"{SITE}/srch?query={quote(query)}")
        if not html:
            return []
        terms, seen = [], set()
        for m in re.findall(r'/taxonomy/term/(\d+)', html):
            if m not in seen:
                seen.add(m)
                terms.append(urljoin(SITE, f"/taxonomy/term/{m}"))
        log(f"поиск «{query}»: страниц товаров {len(terms)}")
        return terms[:limit]

    def product(self, url: str) -> dict | None:
        html = self.c.text(url)
        if not html:
            return None
        p = HTMLParser(html)
        name = p.css_first("h1")
        return {
            "platform": NAME,
            "query": "",
            "product_id": url.rstrip("/").rsplit("/", 1)[-1],
            "brand": "",
            "product_name": re.sub(r"\s+", " ", name.text(strip=True))[:200] if name else "",
            "supplier": "",
            "product_url": url,
            "rating": _prop(p, "ratingValue"),
            "reviews_declared": 0,
            "reviews_collected": 0,
            "price_rub": "",
            "extra": "",
            "_first_page": html,
        }

    def review_links(self, product: dict, max_pages: int, cap: int) -> list[str]:
        """Ссылки на отзывы со страницы товара и её пагинации."""
        links, seen = [], set()
        for page in range(0, max_pages):
            if page == 0:
                html = product.pop("_first_page", None) or self.c.text(product["product_url"])
            else:
                html = self.c.text(f"{product['product_url']}?page={page}")
            if not html:
                break
            found = re.findall(r'/content/([a-z0-9\-]+)', html)
            fresh = 0
            for slug in found:
                if slug not in seen:
                    seen.add(slug)
                    links.append(urljoin(SITE, f"/content/{slug}"))
                    fresh += 1
            if not fresh or len(links) >= cap:
                break
            self.c.sleep()
        return links[:cap]

    def review(self, url: str, product: dict, query: str) -> dict | None:
        html = self.c.text(url, tries=3)
        if not html:
            return None
        page = HTMLParser(html)

        # Всё — только внутри контейнера отзыва. На странице лежат ещё
        # 20+ чужих отзывов из блока «похожие», и любой селектор
        # по классу без этой рамки хватает случайный.
        box = None
        for n in page.css(REVIEW_BOX):
            if "Review" in (n.attributes.get("itemtype") or ""):
                box = n
                break
        if box is None:
            return None

        title = _prop(box, "name") or (
            box.css_first(".reviewTitle").text(strip=True)
            if box.css_first(".reviewTitle") else ""
        )
        plus = box.css_first(".plus")
        minus = box.css_first(".minus")
        verdict = box.css_first(".verdict")
        author = _prop(box, "author")

        return row(
            NAME,
            query=query,
            brand=product.get("brand", ""),
            product_name=product["product_name"],
            product_id=product["product_id"],
            product_url=product["product_url"],
            review_id=url.rstrip("/").rsplit("/", 1)[-1],
            author_hash=hashlib.sha256(author.encode()).hexdigest()[:12] if author else "",
            rating=_prop(box, "ratingValue"),
            date=_prop(box, "datePublished"),
            title=title,
            text=_prop(box, "reviewBody"),
            pros=_strip_label(plus.text(strip=True), "Достоинства") if plus else "",
            cons=_strip_label(minus.text(strip=True), "Недостатки") if minus else "",
            has_photo=bool(box.css("img[itemprop=image], .reviewPhotos img")),
            about="product",
            source_url=url,
            # «рекомендует» / «не рекомендует» — вердикт автора, отдельный
            # от оценки. В канонической схеме такого поля нет.
            verdict=verdict.text(strip=True) if verdict else "",
        )


def collect(args) -> tuple[list[dict], list[dict], list[str]]:
    ir = IRecommend()
    label = args.query or "по id"

    down = ir.alive()
    if down:
        log(f"iRecommend недоступен: {down}")
        return [], [], [
            f"Сбор не выполнен: {down}. Это не ошибка парсера и не отсутствие "
            "отзывов — площадка сама закрыта. Повторить позже."
        ]

    urls = (
        [urljoin(SITE, f"/taxonomy/term/{t}") for t in args.term]
        if args.term
        else ir.search(args.query, args.products)
    )
    if not urls:
        return [], [], ["Поиск не дал страниц товаров."]

    products: list[dict] = []
    reviews: list[dict] = []
    for i, url in enumerate(urls[: args.products], 1):
        p = ir.product(url)
        if not p:
            log(f"[{i}/{len(urls)}] {url} — не открылась")
            continue
        p["query"] = label

        links = ir.review_links(p, args.max_pages, args.max_reviews)
        p["reviews_declared"] = len(links)
        log(f"[{i}/{min(len(urls), args.products)}] {p['product_name'][:44]} — отзывов в списке {len(links)}")

        got = 0
        for link in links:
            r = ir.review(link, p, label)
            if r:
                reviews.append(r)
                got += 1
            ir.c.sleep()
        p["reviews_collected"] = got
        p.pop("_first_page", None)
        products.append(p)
        log(f"     собрано {got} из {len(links)}")

    notes = [
        "Полный текст отзыва лежит только на его собственной странице, "
        "поэтому на каждый отзыв уходит отдельный запрос. Объём ограничен "
        "`--max-reviews`.",
        "Модуль написан по разведке структуры, но не проверен на живых "
        "данных: во время разработки сайт стоял на обслуживании. Сверьте "
        "пять отзывов с сайтом глазами перед тем, как доверять датасету.",
        f"Сеть: {ir.c.stats()}.",
    ]
    return products, reviews, notes
