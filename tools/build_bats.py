"""Сборка .bat файлов в кодировке cp866.

cmd.exe читает пакетные файлы в OEM-кодировке (cp866 для русского Windows).
Если сохранить .bat в UTF-8, кириллица ломает разбор строк, и команды
превращаются в ошибки вида "'' is not recognized".

Поэтому исходники лежат рядом с расширением .utf8 (их удобно хранить в git,
где UTF-8 — норма), а этот скрипт собирает из них рабочие .bat в cp866.

Запуск:  python tools/build_bats.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Символы, которых нет в cp866, но которые легко случайно набрать.
REPLACEMENTS = (
    ("\u2014", "--"),   # длинное тире
    ("\u2013", "-"),    # короткое тире
    ("\u00ab", '"'),    # кавычка-ёлочка
    ("\u00bb", '"'),
    ("\u2026", "..."),  # многоточие
    ("\u2192", "->"),   # стрелка
    ("\u25b2", "^"),
    ("\u25bc", "v"),
)

ROOT = Path(__file__).resolve().parents[1]


def sanitize(text: str) -> str:
    for source, target in REPLACEMENTS:
        text = text.replace(source, target)
    return text


def build(src: Path) -> Path:
    text = sanitize(src.read_text(encoding="utf-8"))
    dst = src.with_suffix("")  # foo.bat.utf8 -> foo.bat
    dst.write_bytes(text.replace("\n", "\r\n").encode("cp866"))
    return dst


def main() -> int:
    # Проверяем, что каждый символ представим в cp866: иначе файл не соберётся
    # и пользователь получит непонятную ошибку уже при запуске.
    sources = sorted(ROOT.glob("*.bat.utf8"))
    if not sources:
        print("Не найдено ни одного файла *.bat.utf8")
        return 1

    failed = False
    for src in sources:
        text = sanitize(src.read_text(encoding="utf-8"))
        try:
            text.encode("cp866")
        except UnicodeEncodeError as exc:
            bad = text[exc.start]
            print(f"ОШИБКА: {src.name} содержит символ {bad!r} (U+{ord(bad):04X}), которого нет в cp866")
            failed = True
            continue
        dst = build(src)
        print(f"{dst.name}: {dst.stat().st_size} байт")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
