"""Фаза 10: гигиена текста и диска.

План требовал чинить две вещи, которые сломаны не были. Диагноз П.7 утверждал,
что `memories/MEMORY.md` имеет «битую кодировку из-за двойной перекодировки
при записи», и что `bridge/hands.log` растёт «в cp1251-мусоре». Оба файла в
реальности валидный UTF-8 без единого битого символа:

  - `tools/memory_tool_store.py:465` пишет через `atomic_write_text`, у которого
    кодировка ПО УМОЛЧАНИЮ utf-8 - явная, а не унаследованная от локали;
  - `bridge/hands.py:58` открывает лог как `open(LOG, "a", encoding="utf-8")`.

Обе «поломки» были видны с консоли cp1251, где русский текст выглядит мусором
независимо от того, что файл в порядке. Это тот же ложный диагноз, который уже
один раз стоил времени в этом проекте.

Этот набор фиксирует правило, а не разовую проверку: любой текстовый файл
проекта должен быть валидным UTF-8, иначе его нельзя читать ни в одном
инструменте сразу. Плюс - измерение места, потому что «уборка» без цифр
невозможно ни проверить, ни оспорить.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
# TODO.md - файл планировщика. Править его отсюда нельзя: правка
# чужого документа замаскировала бы повреждение. Поэтому он
# исключён из проверки и докладывается отдельно.
PLANNER_FILES = {"TODO.md"}
SKIP_DIRS = {".git", ".worktrees", "node_modules", "__pycache__", "cache",
             "venvs", "site-packages", ".mechanical", "searxng", "logs",
             "generated", "assets", "_setup_tmp"}
TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def text_files(limit=4000):
    out = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml",
                                      ".py", ".ps1", ".bat", ".toml", ".cfg")):
                out.append(os.path.join(dirpath, name))
                if len(out) >= limit:
                    return out
    return out


_REPLACEMENT = chr(0xFFFD)


def mem_status_gb(drive):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[math]::Round((Get-PSDrive %s).Free/1GB,2)" % drive],
            capture_output=True, text=True, timeout=40).stdout.strip()
        # Русская локаль отдаёт запятую как десятичный разделитель - ровно та
        # причина, по которой одна из проб памяти молча возвращала -1.
        return float(out.replace(",", ".")) if out else -1.0
    except Exception:  # noqa: BLE001
        return -1.0


# --- кодировка ---------------------------------------------------------

@test
def test_the_two_files_the_plan_called_corrupt_are_clean_utf8():
    """Проверяется ИМЕННО то, что план назвал сломанным."""
    fails = []
    for rel in (os.path.join("hermes-home", "memories", "MEMORY.md"),
                os.path.join("bridge", "hands.log")):
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            fails.append("%s is missing" % rel)
            continue
        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            fails.append("%s is not valid UTF-8: %s" % (rel, exc))
            continue
        bad = text.count(_REPLACEMENT)
        if bad:
            fails.append("%s has %d replacement char(s)" % (rel, bad))
    return fails


@test
def test_the_memory_writer_uses_an_explicit_encoding():
    """Правка кодировки бессмысленна, если писатель перезапишет файл. Проверяем
    не файл, а писателя - как и просил план."""
    fails = []
    base = os.environ.get("HERMES_SRC")
    candidates = [
        os.path.join(_ROOT, "functions", "NOTES.md"),  # документация
    ]
    # Ищем реальный источник там, где он установлен.
    roots = [os.path.expandvars(r"%LOCALAPPDATA%\hermes\hermes-agent")]
    found_writer = False
    for root in roots:
        store = os.path.join(root, "tools", "memory_tool_store.py")
        if not os.path.exists(store):
            continue
        found_writer = True
        body = open(store, encoding="utf-8", errors="replace").read()
        if "atomic_write_text" not in body:
            fails.append("memory store no longer uses atomic_write_text")
        else:
            utils = os.path.join(root, "utils.py")
            if os.path.exists(utils):
                u = open(utils, encoding="utf-8", errors="replace").read()
                if 'encoding: str = "utf-8"' not in u:
                    fails.append("atomic_write_text no longer defaults to utf-8")
    if not found_writer:
        fails.append("could not locate the memory store writer to check it")
    for rel in candidates:
        if not os.path.exists(rel):
            fails.append("%s is missing" % rel)
    return fails


@test
def test_project_text_files_are_valid_utf8():
    """Правило вместо разовой проверки: проект читается инструментами с
    РАЗНЫМИ кодировками, и файл должен открываться везде."""
    fails = []
    bad = []
    for path in text_files():
        raw = open(path, "rb").read()
        if raw.startswith(b"\xef\xbb\xbf"):
            # BOM допустим, но помечаем: он ломает чтение через .splitlines()
            # в некоторых инструментах и мешает точному сравнению хэшей.
            bad.append("BOM: " + os.path.relpath(path, _ROOT))
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append("not UTF-8: " + os.path.relpath(path, _ROOT))
    if bad:
        fails.append("%d file(s): %s" % (len(bad), "; ".join(bad[:6])))
    return fails


@test
def test_no_replacement_characters_anywhere_in_the_project_text():
    fails = []
    # Файлы планировщика проверяет отдельная группа: здесь они были бы
    # задвоены, и падение выглядело бы двумя разными проблемами.
    planner = {os.path.join(_ROOT, n) for n in PLANNER_FILES}
    for path in text_files(limit=1500):
        if path in planner:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if _REPLACEMENT in text:
            fails.append("U+FFFD in " + os.path.relpath(path, _ROOT))
    return fails


# --- диск --------------------------------------------------------------

@test
def test_free_space_on_the_drives_that_matter():
    """Цифры, потому что «уборка» без измерения не проверяема.

    Порог 5 ГБ на A: взят не с потолка: сегодня A: падал до 0.29 ГБ из-за
    песочницы чистого прогона, и это был реальный инцидент."""
    fails = []
    a = mem_status_gb("A")
    print("    A: %.2f GB free" % a)
    if 0 < a < 5.0:
        fails.append("A: has only %.2f GB free - this already happened once"
                     % a)
    c = mem_status_gb("C")
    print("    C: %.2f GB free" % c)
    if 0 < c < 5.0:
        fails.append("C: has only %.2f GB free; Hermes and node live there"
                     % c)
    return fails


@test
def test_the_cache_is_reported_so_the_move_can_be_decided():
    """Перенос кэша на B: - решение владельца, но оно принимается по цифрам.
    Тест печатает размеры и проверяет лишь, что цифры вообще считываются."""
    import math
    fails = []
    total = 0.0
    for name in ("hf", "uv", "npm"):
        path = os.path.join(_ROOT, "cache", name)
        if not os.path.isdir(path):
            continue
        size = 0.0
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        gb = round(size / (1024 ** 3), 1)
        print("    cache/%s: %.1f GB" % (name, gb))
        total += gb
    print("    cache total: %.1f GB" % round(total, 1))
    b = mem_status_gb("B")
    print("    B: %.2f GB free" % b)
    if b <= 0:
        fails.append("could not read free space on B:")
    return fails


@test
def test_planner_files_are_reported_not_silently_fixed():
    """TODO.md принадлежит планировщику, и мы его НЕ правим.

    Но повреждение в нём должно быть названо, а не спрятано исключением из
    проверки. На 2026-10-02 в TODO.md два символа U+FFFD в строке статуса
    Фазы 5 - следы обрезки текста планировщика."""
    out = []
    for name in PLANNER_FILES:
        path = os.path.join(_ROOT, name)
        if not os.path.exists(path):
            continue
        raw = open(path, "rb").read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            out.append("%s is not valid UTF-8: %s" % (name, exc))
            continue
        n = text.count(_REPLACEMENT)
        if n:
            lines = [i for i, l in enumerate(text.splitlines(), 1)
                     if _REPLACEMENT in l]
            print("    NOTE %s has %d replacement char(s) at line(s) %s"
                  % (name, n, lines))
            out.append("%s carries %d replacement char(s) at %s - report it, "
                       "do not edit the planner's file"
                       % (name, n, lines))
    return out


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = list(fn() or [])
        except Exception as exc:  # noqa: BLE001
            fails = ["%s: %s" % (type(exc).__name__, exc)]
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)
    print()
    if failed:
        print("%d/%d groups failed" % (failed, len(TESTS)))
        return 1
    print("all pass (%d groups)" % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())