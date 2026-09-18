#!/usr/bin/env python3
"""
Единая точка входа: сбор отзывов с любой подключённой площадки.

    python3 collect.py wb "<запрос>" --explore
    python3 collect.py wb "<запрос>" --brand "<бренд>" --subject "<категория>"
    python3 collect.py wb "<запрос>" --stars 1-3 --period 2025-01..2025-08
    python3 collect.py wb --nm <артикул> <артикул> --media photo

Площадка задаёт свои аргументы (что искать), фильтры общие для всех
(что оставить из собранного). Результат — в out/<площадка>/<запрос>/
относительно каталога, из которого запущен инструмент:

    <площадка>--<запрос>.xlsx  книга для коллег: Сводка, Отзывы, Негатив, Карточки
    reviews.csv   отзывы, строка = отзыв, колонки канонической схемы
    products.csv  карточки в выборке, покрытие по каждой
    summary.md    сводка: оценки, темы негатива, динамика
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from core import facets, filters, report, schema
from core.output import slugify, write_csv, write_reviews
from core.xlsx import write_workbook

# Результат кладётся рядом с рабочим проектом пользователя, а не внутрь
# скилла: скилл живёт в общем каталоге и обновляется из репозитория,
# данные в нём потерялись бы при обновлении.
OUT_DIR = Path.cwd() / "out"

# Подключённые площадки. В скилле только Wildberries: остальные либо
# не проверены на живых данных, либо закрыты антиботом. Их код лежит
# в репозитории разработки, каталог spare/, и возвращается сюда одной
# строкой, когда площадка пройдёт приёмку.
REGISTRY = {
    "wb": "platforms.wb",
}


def load(name: str):
    return importlib.import_module(REGISTRY[name])


def show_list() -> int:
    print("Готовы:")
    for name in REGISTRY:
        try:
            m = load(name)
            print(f"  {name:<12} тир {m.TIER}  {m.TITLE}")
        except Exception as e:  # модуль сломан — это не должно валить остальное
            print(f"  {name:<12} ошибка импорта: {e}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="collect.py",
        description="Сбор отзывов с площадок в единую схему",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--list", action="store_true", help="показать площадки и их состояние")

    sub = ap.add_subparsers(dest="platform", metavar="площадка")
    for name in REGISTRY:
        try:
            m = load(name)
        except Exception as e:
            print(f"внимание: {name} не импортируется ({e})", file=sys.stderr)
            continue
        p = sub.add_parser(name, help=f"{m.TITLE} (тир {m.TIER})")
        p.add_argument("query", nargs="?", help="запрос: бренд или продукт")
        # Разведка структуры вместо сбора. Нужна, чтобы уточняющий вопрос
        # задавался по фактам источника, а не по догадке о том, что там есть.
        p.add_argument(
            "--explore",
            action="store_true",
            help="показать структуру по запросу (подкатегории, бренды, продавцы) и ничего не собирать",
        )
        m.add_args(p)
        filters.add_args(p)
        p.add_argument("--out", help="каталог результата")
        p.add_argument("--keep-raw", action="store_true", help="сохранять сырые ответы")
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.list or not args.platform:
        return show_list()

    m = load(args.platform)
    label = args.query or getattr(args, "nm", None) and f"nm-{args.nm[0]}" or ""
    if not label:
        ap.error("нужен запрос или идентификаторы товаров")

    if getattr(args, "explore", False):
        if not hasattr(m, "explore"):
            print(
                f"{m.TITLE} не умеет показывать структуру: в выдаче нет разрезов, "
                "по которым можно сузить. Собирайте запросом целиком.",
                file=sys.stderr,
            )
            return 1
        print(facets.render(m.explore(args)))
        return 0

    # Каталог результата отражает сужение, а не только запрос: иначе
    # два разреза одного запроса («свитер» по водолазкам и по джемперам)
    # молча затирают друг друга, и пользователь получает не свои данные.
    parts = [slugify(label)]
    for flag in ("brand", "subject", "supplier"):
        val = getattr(args, flag, None)
        slug = slugify(str(val)) if val else ""
        # Бренд часто совпадает с запросом («твое» + --brand твое):
        # повтор в имени каталога только мешает читать.
        if slug and slug not in parts:
            parts.append(slug)
    out = Path(args.out) if args.out else OUT_DIR / args.platform / "--".join(parts)
    out.mkdir(parents=True, exist_ok=True)
    # Имя книги повторяет площадку и сужение: файл уходит коллегам
    # по почте и должен читаться сам, без каталога вокруг.
    xlsx = out / f"{args.platform}--{'--'.join(parts)}.xlsx"

    print(f"площадка: {m.TITLE} (тир {m.TIER})", file=sys.stderr)
    print(f"запрос: {label}", file=sys.stderr)

    products, reviews, notes = m.collect(args)
    collected = len(reviews)

    # Один и тот же отзыв площадка показывает на нескольких товарах
    # семейства. Схлопываем до фильтров, иначе все доли считаются
    # от завышенного числа.
    reviews, dropped = schema.dedup(reviews)
    if dropped:
        print(f"  дублей убрано: {dropped} (тот же отзыв на разных товарах)", file=sys.stderr)
        notes = list(notes or []) + [
            f"Убрано {dropped} дублей: площадка показывает один отзыв на "
            f"нескольких товарах семейства. Настоящих отзывов {len(reviews)} "
            f"из {collected} собранных строк."
        ]

    # Фильтры применяются после сбора: площадки не умеют фильтровать
    # на своей стороне, а если бы умели, срез всё равно надо считать
    # от полного набора. Несфильтрованный набор нужен сводке для замера
    # смещения — иначе фильтр по оценкам показывает сам себя как смещение.
    unfiltered = list(reviews)
    reviews, filter_report = filters.apply(reviews, args)
    if filter_report:
        for line in filter_report:
            print(f"  фильтр — {line}", file=sys.stderr)

    # Критерий приёмки №3: датасет обязан пройти валидацию схемы.
    problems = schema.validate(reviews) if reviews else []
    if problems:
        print("", file=sys.stderr)
        print("ПРОВЕРКА СХЕМЫ — проблемы:", file=sys.stderr)
        for p in problems:
            print(f"  ! {p}", file=sys.stderr)

    write_reviews(out / "reviews.csv", reviews)
    write_csv(out / "products.csv", products, fieldnames=schema.PRODUCT_FIELDS)

    if problems:
        notes = list(notes or []) + [
            "Валидация схемы нашла проблемы: " + "; ".join(problems)
        ]
    doc = report.build(
        m.TITLE, label, products, reviews, notes, filter_report,
        # Число до фильтров: покрытие площадки считается от него,
        # иначе запрос за период читается как «площадка скрыла всё».
        collected=collected - dropped,
        unfiltered=unfiltered,
    )
    (out / "summary.md").write_text(doc.markdown(), encoding="utf-8")
    # Книга пишется и при пустом результате: пустые листы с оговоркой
    # в сводке честнее, чем отсутствующий файл, который выглядит как сбой.
    write_workbook(xlsx, doc, reviews, products)

    if args.keep_raw:
        (out / "raw").mkdir(exist_ok=True)
        (out / "raw" / "products.json").write_text(
            json.dumps(products, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    print("", file=sys.stderr)
    stages = [f"собрано {collected}"]
    if dropped:
        stages.append(f"без дублей {collected - dropped}")
    if filter_report:
        stages.append(f"после фильтров {len(reviews)}")
    if len(stages) > 1:
        print(" → ".join(stages) + f", карточек {len(products)}", file=sys.stderr)
    else:
        print(f"готово: {len(reviews)} отзывов по {len(products)} карточкам", file=sys.stderr)
    print(f"→ {xlsx}", file=sys.stderr)
    return 0 if reviews or not products else 1


if __name__ == "__main__":
    sys.exit(main())
