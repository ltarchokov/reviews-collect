# reviews-collect

Скилл для агентов (Cursor, Kilo, Claude Code): собирает отзывы покупателей
с Wildberries и отдаёт книгу Excel со сводкой, отзывами, негативом
и карточками. Другие площадки не собирает. Инструкция агенту — в [SKILL.md](SKILL.md), код — в `scripts/`.

## Установка

Склонировать в каталог скиллов своего агента. Имя каталога должно быть
`reviews-collect`.

| Агент | Для одного проекта | Для всех проектов |
|---|---|---|
| Cursor | `.cursor/skills/reviews-collect` | `~/.cursor/skills/reviews-collect` |
| Kilo | `.kilo/skills/reviews-collect` | `~/.kilo/skills/reviews-collect` |
| Claude Code | `.claude/skills/reviews-collect` | `~/.claude/skills/reviews-collect` |

Cursor и Kilo читают также `.claude/skills/`, так что одной копии в нём
хватает на всех трёх.

```bash
git clone <адрес репозитория> ~/.cursor/skills/reviews-collect
cd ~/.cursor/skills/reviews-collect/scripts
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Нужен Python 3.10 или новее. На Windows вместо `.venv/bin/pip` —
`.venv\Scripts\pip`.

Обновление: `git pull` в каталоге скилла. Окружение пересобирать
не нужно, пока не изменился `requirements.txt`.

## Проверка

Напишите агенту «что ты умеешь?» — скилл представится и задаст два
вопроса: что ищем и какие отзывы нужны. Или сразу задачу со своим
брендом или категорией — агент покажет, что есть в выдаче WB, спросит,
какой срез собирать, и отдаст книгу Excel. Сам по себе, без вашего
запроса, агент ничего собирать не должен.

## Что внутри

```
SKILL.md      ход работы агента: знакомство, разведка, уточняющий вопрос, сбор, ответ
references/   ограничения Wildberries и как читать сводку
scripts/      collect.py и код парсеров; сюда же ставится .venv
```

Результат каждого сбора — в `out/<площадка>/<срез>/` относительно
каталога, в котором открыта сессия агента.
