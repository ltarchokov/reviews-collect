"""
Ютека — тир B: серверный HTML с микроразметкой schema.org.

Особенности площадки, найденные разведкой:

**Страница отзывов живёт по пути категории, а не товара.** Товар лежит на
`/product/<slug>/`, а его отзывы — на `<путь-категории>/<slug>/reviews/`.
Запрос `/product/<slug>/reviews/` молча редиректит на страницу категории
с чужими отзывами. Поэтому ссылка на отзывы берётся со страницы товара,
а не собирается из slug.

**На карточке товара видно только три отзыва**, остальные — на странице
отзывов с пагинацией `?page=N`.

**У отзывов нет идентификатора.** Ни в разметке, ни в data-атрибутах.
Поэтому `review_id` считается хешем от автора, даты и начала текста:
устойчив между прогонами, ловит дубли, ничего не раскрывает.

**Заявленное число отзывов расходится с отданным.** На АЦЦ Лонг карточка
показывает 25, страница отдаёт 15. Расхождение попадает в products.csv
как замер потолка — см. reviews_declared против reviews_collected.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser

from core.http import Client, log
from core.schema import row

NAME = "uteka"
TITLE = "Ютека"
TIER = "B"

SITE = "https://uteka.ru"
HEADERS = {"Referer": SITE + "/"}

# Блоки внутри текста отзыва размечены префиксами.
BODY_PARTS = {
    "Достоинства:": "pros",
    "Недостатки:": "cons",
    "Комментарий:": "text",
}


def add_args(ap) -> None:
    ap.add_argument("--url", nargs="+", help="прямые ссылки на товары вместо поиска")
    ap.add_argument("--products", type=int, default=10, help="сколько товаров обработать (по умолчанию 10)")
    ap.add_argument("--max-pages", type=int, default=30, help="предохранитель: страниц отзывов на товар")


def _txt(node, sel: str) -> str:
    n = node.css_first(sel)
    if n is None:
        return ""
    return (n.attributes.get("content") or n.text(strip=True) or "").strip()


def _review_id(product: str, author: str, date: str, text: str) -> str:
    """Устойчивый идентификатор: площадка своего не даёт."""
    raw = f"{product}|{author}|{date}|{text[:60]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class Uteka:
    def __init__(self):
        self.c = Client(headers=HEADERS, delay=(0.8, 1.5))

    def search(self, query: str, limit: int) -> list[str]:
        """Ссылки на товары. Параметр именно `query`: с `q` площадка молча
        отдаёт главную страницу, и в выдачу попадает случайный ассортимент."""
        html = self.c.text(f"{SITE}/search/?query={quote(query)}")
        if not html:
            return []
        urls, seen = [], set()
        for m in re.findall(r'href="(/product/[a-zA-Z0-9\-_/]+)"', html):
            if m not in seen:
                seen.add(m)
                urls.append(urljoin(SITE, m))
        log(f"поиск «{query}»: найдено товаров {len(urls)}")
        return urls[:limit]

    def product(self, url: str) -> dict | None:
        """Карточка товара плюс адрес её страницы отзывов."""
        html = self.c.text(url)
        if not html:
            return None
        p = HTMLParser(html)

        m = re.search(r'href="(/[^"]*?/reviews/)"', html)
        reviews_url = urljoin(SITE, m.group(1)) if m else None

        name = _txt(p, "h1") or _txt(p, 'meta[property="og:title"]')
        declared = _txt(p, '[itemprop="reviewCount"]')
        rating = _txt(p, '[itemprop="ratingValue"]')

        return {
            "platform": NAME,
            "query": "",
            "product_id": url.rstrip("/").rsplit("/", 1)[-1],
            "brand": _txt(p, '[itemprop="brand"]'),
            "product_name": re.sub(r"\s+", " ", name)[:200],
            "supplier": "",
            "product_url": url,
            "rating": rating,
            "reviews_declared": int(declared) if declared.isdigit() else 0,
            "reviews_collected": 0,
            "price_rub": "",
            "extra": "",
            "_reviews_url": reviews_url,
        }

    def reviews(self, product: dict, query: str, max_pages: int) -> list[dict]:
        url = product.get("_reviews_url")
        if not url:
            log(f"  {product['product_name'][:44]} — страницы отзывов нет")
            return []

        out: list[dict] = []
        for page in range(1, max_pages + 1):
            page_url = url if page == 1 else f"{url}?page={page}"
            html = self.c.text(page_url, tries=3)
            if not html:
                break
            p = HTMLParser(html)

            # Счётчик берём со страницы отзывов, а не с карточки товара.
            # На карточке он относится к одной фасовке, а страница общая
            # на всё семейство: у АЦЦ карточка показывает 4, страница 25.
            if page == 1:
                declared = _txt(p, '[itemprop="reviewCount"]')
                if declared.isdigit():
                    product["reviews_declared"] = int(declared)

            nodes = p.css("[itemprop=review]")
            if not nodes:
                break
            for n in nodes:
                r = self._parse(n, product, query)
                if r:
                    out.append(r)
            # Последняя страница короче полной — дальше идти незачем.
            if len(nodes) < 10:
                break
            self.c.sleep()
        return out

    def _parse(self, n, product: dict, query: str) -> dict | None:
        author = _txt(n, '[itemprop="author"]')
        date = _txt(n, '[itemprop="datePublished"]')
        rating = _txt(n, '[itemprop="ratingValue"]')

        # Текст разложен на блоки с префиксами. Разбираем по ним, а не
        # берём reviewBody целиком: иначе достоинства и недостатки
        # слипаются в одну строку и теряют смысл.
        parts = {"pros": "", "cons": "", "text": ""}
        body = n.css_first('[itemprop="reviewBody"]')
        if body is not None:
            for div in body.css("div"):
                t = div.text(strip=True)
                for prefix, key in BODY_PARTS.items():
                    if t.startswith(prefix):
                        parts[key] = t[len(prefix):].strip()
                        break
                else:
                    if not parts["text"] and t:
                        parts["text"] = t

        if not any(parts.values()) and not rating:
            return None

        whole = n.text()
        return row(
            NAME,
            query=query,
            brand=product["brand"],
            product_name=product["product_name"],
            product_id=product["product_id"],
            product_url=product["product_url"],
            # Идентификатор считается от страницы отзывов, а не от товара:
            # страница общая на семейство фасовок, и ключ от slug дал бы
            # одному отзыву разные id при разных точках входа.
            review_id=_review_id(
                product.get("_reviews_url") or product["product_id"],
                author,
                date,
                parts["text"] or parts["pros"],
            ),
            author_hash=hashlib.sha256(author.encode()).hexdigest()[:12] if author else "",
            rating=rating,
            date=date,
            text=parts["text"],
            pros=parts["pros"],
            cons=parts["cons"],
            has_photo=bool(n.css("[class*=review-gallery]")),
            # «Куплен на Ютеке» — метка площадки о подтверждённой покупке.
            verified_purchase="Куплен на Ютеке" in whole,
            # Отзывы на карточке товара — о товаре. Отзывы о самом сервисе
            # Ютеки (поиск, бронирование, выдача в аптеке) живут отдельно
            # и сюда не попадают: это отдельная задача.
            about="product",
            source_url=product["product_url"],
        )


def collect(args) -> tuple[list[dict], list[dict], list[str]]:
    u = Uteka()
    label = args.query or "по ссылкам"

    urls = args.url if args.url else u.search(args.query, args.products)
    if not urls:
        return [], [], ["Поиск не дал товаров."]

    products: list[dict] = []
    reviews: list[dict] = []
    seen_pages: set[str] = set()
    for i, url in enumerate(urls[: args.products], 1):
        p = u.product(url)
        if not p:
            log(f"[{i}/{len(urls)}] {url} — страница не открылась")
            continue
        p["query"] = label

        # Фасовки одного препарата делят одну страницу отзывов: у АЦЦ
        # 100, 200 и 600 мг ведут на общую. Не схлопнув их, соберём одни
        # и те же отзывы по разу на фасовку — на пробном прогоне это
        # дало 114 строк вместо 36 настоящих.
        page = p.get("_reviews_url")
        if page and page in seen_pages:
            log(f"[{i}/{min(len(urls), args.products)}] {p['product_name'][:44]} "
                "— та же страница отзывов, пропускаю")
            continue
        if page:
            seen_pages.add(page)

        rows = u.reviews(p, label, args.max_pages)
        p["reviews_collected"] = len(rows)
        products.append(p)
        reviews.extend(rows)
        log(
            f"[{i}/{min(len(urls), args.products)}] {p['product_name'][:44]} "
            f"— {len(rows)} из {p['reviews_declared']}"
        )
        u.c.sleep()

    declared = sum(p["reviews_declared"] for p in products)
    notes = [
        "Страница отзывов Ютеки лежит по пути категории, а не товара; ссылка "
        "берётся со страницы товара. Прямой адрес `/product/<slug>/reviews/` "
        "молча уводит на категорию с чужими отзывами.",
        f"Сеть: {u.c.stats()}.",
    ]
    if declared and len(reviews) < declared:
        notes.append(
            f"Карточки заявляют {declared} отзывов, страницы отдали {len(reviews)}. "
            "Расхождение стабильное — часть отзывов площадка не показывает."
        )
    return products, reviews, notes
