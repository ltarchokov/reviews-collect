"""
Сводка как структура, а не как текст.

Раньше сводка сразу писалась в Markdown. Когда результат стал уезжать
коллегам в Excel, понадобился второй вид той же сводки — и делать его
разбором собственного Markdown было бы хрупко. Поэтому сводка теперь
собирается из блоков (заголовок, абзац, пункт, таблица), а Markdown
и лист Excel — два рендера одной структуры. Цифры в них совпадают
по построению, а не по совпадению.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`(.+?)`")


def plain(text: str) -> str:
    """Убирает разметку Markdown: в ячейке Excel звёздочки — мусор."""
    return _CODE.sub(r"\1", _BOLD.sub(r"\1", str(text)))


@dataclass
class Block:
    kind: str                      # h1 | h2 | h3 | p | li | quote | table | bars
    text: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    align: str = ""                # для таблиц: буква на колонку, «lrr»


class Doc:
    def __init__(self) -> None:
        self.blocks: list[Block] = []

    # --- построение --------------------------------------------------
    def h1(self, text: str) -> None:
        self.blocks.append(Block("h1", text))

    def h2(self, text: str) -> None:
        self.blocks.append(Block("h2", text))

    def h3(self, text: str) -> None:
        self.blocks.append(Block("h3", text))

    def p(self, text: str) -> None:
        self.blocks.append(Block("p", text))

    def li(self, text: str) -> None:
        self.blocks.append(Block("li", text))

    def quote(self, text: str) -> None:
        self.blocks.append(Block("quote", text))

    def table(self, headers: list[str], rows: list[list], align: str = "") -> None:
        self.blocks.append(Block("table", headers=headers, rows=rows, align=align))

    def bars(self, rows: list[tuple[str, int]]) -> None:
        """Ряд «метка — число». В Markdown рисуется полосками, в Excel
        ложится таблицей: там полоски не нужны, сортировка есть."""
        self.blocks.append(Block("bars", rows=[list(r) for r in rows]))

    # --- рендер ------------------------------------------------------
    def markdown(self) -> str:
        L: list[str] = []
        prev = ""
        for b in self.blocks:
            # Пустая строка между блоками; подряд идущие пункты списка
            # не разрываются. Цитаты разрываются: каждая — отдельный
            # отзыв, слитые в один блок они читаются как один текст.
            if L and not (b.kind == prev and b.kind == "li"):
                L.append("")
            if b.kind == "h1":
                L.append(f"# {b.text}")
            elif b.kind == "h2":
                L.append(f"## {b.text}")
            elif b.kind == "h3":
                L.append(f"### {b.text}")
            elif b.kind == "p":
                L.append(b.text)
            elif b.kind == "li":
                L.append(f"- {b.text}")
            elif b.kind == "quote":
                L.extend(f"> {line}" for line in str(b.text).split("\n"))
            elif b.kind == "table":
                L.append("| " + " | ".join(b.headers) + " |")
                # align — строка из букв по колонкам, «lrr»: l слева, r справа.
                marks = list(b.align) if len(b.align) == len(b.headers) else ["l"] * len(b.headers)
                L.append("|" + "|".join("---:" if m == "r" else "---" for m in marks) + "|")
                for row in b.rows:
                    L.append("| " + " | ".join(str(c) for c in row) + " |")
            elif b.kind == "bars":
                top = max((int(r[1]) for r in b.rows), default=1) or 1
                for label, n in b.rows:
                    bar = "█" * max(1, round(int(n) / top * 40))
                    L.append(f"`{label}` {bar} {n}")
            prev = b.kind
        return "\n".join(L) + "\n"
