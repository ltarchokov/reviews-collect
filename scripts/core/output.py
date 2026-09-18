"""Запись результата на диск."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .schema import FIELDS


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(text), flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "query"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    """CSV в utf-8-sig — иначе Excel открывает кириллицу кракозябрами,
    а датасеты уходят коллегам именно в Excel.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=names, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_reviews(path: Path, rows: list[dict]) -> None:
    """Отзывы всегда пишутся в порядке канонической схемы, независимо от
    того, в каком порядке их сложил парсер: датасеты должны мержиться."""
    write_csv(path, rows, fieldnames=FIELDS)
