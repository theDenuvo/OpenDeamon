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
import shutil
_HERE = os.path.dirname(os.path.abspath(__file__))
import os
import sys

# ЗАКОН «ноль проверок — не успех» определён в одном месте; здесь набор только
# им пользуется. См. functions/meta/zero_checks_law.py.
sys.path.insert(0, os.path.join(_HERE, os.pardir, "meta"))
import zero_checks_law as law

_ROOT = os.path.dirname(os.path.dirname(_HERE))
# TODO.md - файл планировщика. Править его отсюда нельзя: правка
# чужого документа замаскировала бы повреждение. Поэтому он
# исключён из проверки и докладывается отдельно.
PLANNER_FILES = {"TODO.md"}
SKIP_DIRS = {".git", ".worktrees", "node_modules", "__pycache__", "cache",
             "venvs", "site-packages", ".mechanical", "searxng", "logs",
             "generated", "assets", "_setup_tmp"}
TESTS = []

# Третий исход наряду с pass и fail берётся из закона, а не определяется
# здесь: см. `zero_checks_law`. Пока закон не существовал, это определение
# было в каждом наборе отдельно, и потому правило можно было обойти, просто
# не прочитав чужой вариант.
Skip = law.Skip
skipped = law.skipped





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


def _volume_roots():
    """Тома, которые имеет смысл проверять на этой машине.

    На Windows: `A:` - проект, кэши и Hermes; `C:` - установленный Hermes,
    opencode и node (общий риск, не зона проекта); `B:` - кандидат на перенос
    кэшей, решение владельца, которого пока нет.

    На любой другой системе букв томов нет. Единственный носитель, который
    имеет смысл мерить - каталог проекта: на Windows это всё, что лежит на
    `A:` (Hermes-home, функции, кэши, и `cache/tmp`, куда `bootstrap.ps1`
    перенаправляет TEMP/TMP), а `C:` и `B:` - это уже про установленный
    Hermes/opencode на чужой машине и про нереализованное решение владельца
    о переносе кэшей. Системный каталог `/tmp` сюда НЕ относится: проект
    его не использует, и мерить его - значит мерить чужое место, где
    низкое свободное место ничего не говорит о проекте."""
    if os.name == "nt":
        return {"A": "A:\\", "C": "C:\\", "B": "B:\\"}
    return {"project": _ROOT}


_VOLUMES = _volume_roots()


def mem_status_gb(drive):
    """Свободное место на томе, помеченном буквой/именем `drive`.

    Раньше источником был PowerShell
    `[math]::Round((Get-PSDrive A).Free/1GB,2)`, то есть Windows и конкретные
    буквы томов. На любой другой системе вызов падал, функция возвращала
    -1.0, и обе дисковые группы ПРОХОДИЛИ, ничего не измерив: -1 это не
    «много места», но проверка читала его как «не ноль — значит, порядок».

    Теперь измерение переносимое (`shutil.disk_usage` по пути), буквы томов
    остались ярлыками, потому что проект действительно разложен по
    `A:`/`C:`/`B:` и пороги для них разные.
    """
    path = _VOLUMES.get(drive)
    if path is None or not os.path.exists(path):
        return -1.0
    try:
        return round(shutil.disk_usage(path).free / (1024 ** 3), 2)
    except OSError:
        return -1.0


# --- кодировка ---------------------------------------------------------

@test
def test_the_two_files_the_plan_called_corrupt_are_clean_utf8():
    """Проверяется ИМЕННО то, что план назвал сломанным.

    `memories/MEMORY.md` лежит в git - он проверяется как есть.

    `bridge/hands.log` - РUNTIME-файл: его нет в свежем клооне by design
    (`.gitignore` содержит `*.log`, журнал моста создаётся при первом
    запуске). Требовать его существование было не проверкой свойства файла,
    а проверкой того, что мост уже поработали. Поэтому файл проверяется,
    если он есть, а когда его нет - проверяется ПИСАТЕЛЬ: `hands.py`
    обязан открывать журнал с явным `encoding="utf-8"`. Именно писатель
    может испортить файл заново, и в отличие от журнала он лежит в
    репозитории и проверяется на любой машине.
    """
    fails = []
    for rel in (os.path.join("hermes-home", "memories", "MEMORY.md"),
                os.path.join("bridge", "hands.log")):
        path = os.path.join(_ROOT, rel)
        if not os.path.exists(path):
            if rel.endswith("hands.log"):
                continue          # runtime-артефакт, см. ниже
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

    writer = os.path.join(_ROOT, "bridge", "hands.py")
    if not os.path.isfile(writer):
        fails.append("bridge/hands.py is missing, so the log has no writer "
                     "to check")
        return fails
    body = open(writer, encoding="utf-8").read()
    log_open = [l.strip() for l in body.splitlines()
                if "open(" in l and "LOG" in l]
    if not log_open:
        fails.append("bridge/hands.py no longer opens LOG in one place - the "
                     "log writer moved and needs to be re-checked")
    for line in log_open:
        if "encoding=" not in line:
            fails.append("the log writer does not set an explicit encoding, "
                         "so the log inherits the locale: %s" % line)
    return fails


@test
def test_the_memory_writer_uses_an_explicit_encoding():
    """Правка кодировки бессмысленна, если писатель перезапишет файл. Проверяем
    не файл, а писателя - как и просил план.

    Переносимость: писатель живёт не в этом репозитории, а в установленном
    Hermes (`%LOCALAPPDATA%\\hermes\\hermes-agent`). Путь искался ТОЛЬКО там,
    поэтому на любой другой машине группа падала с «could not locate», то
    есть проверка, ради которой она написана, не выполнялась ни разу.

    Теперь пути переносимые: `$HERMES_SRC`, `tools/` внутри репозитория и
    прежний install-путь (на не-Windows `expandvars` его просто не найдёт).
    Если писателя на этой машине нет - группа объявляет SKIP с причиной, а
    не рапортует успех. Молчаливый успех здесь означал бы «мы проверили
    кодировку памяти», что было бы неправдой.
    """
    fails = []
    candidates = []
    base = os.environ.get("HERMES_SRC")
    if base:
        candidates.append(os.path.join(base, "tools"))
    candidates.append(os.path.join(_ROOT, "tools"))
    candidates.append(os.path.expandvars(
        r"%LOCALAPPDATA%\hermes\hermes-agent\tools"))

    store = ""
    for tools_dir in candidates:
        path = os.path.join(tools_dir, "memory_tool_store.py")
        if os.path.isfile(path):
            store = path
            break
    if not store:
        return skipped("the Hermes memory store is not installed on this host "
                       "(looked in %s); the writer cannot be inspected here"
                       % ", ".join(candidates))

    body = open(store, encoding="utf-8", errors="replace").read()
    if "atomic_write_text" not in body:
        fails.append("memory store no longer uses atomic_write_text")
    utils = os.path.join(os.path.dirname(store), "utils.py")
    if os.path.exists(utils):
        u = open(utils, encoding="utf-8", errors="replace").read()
        if 'encoding: str = "utf-8"' not in u:
            fails.append("atomic_write_text no longer defaults to utf-8")
    for rel in (os.path.join("functions", "NOTES.md"),):
        if not os.path.exists(os.path.join(_ROOT, rel)):
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

    Порог 5 ГБ взят не с потолка: сегодня A: падал до 0.29 ГБ из-за
    песочницы чистого прогона, и это был реальный инцидент.

    Переносимость: список томов задаёт `_volume_roots()` для этой системы,
    а не жёсткие буквы `A:`/`C:`. На Windows проверяются те же тома с теми
    же порогами; на других системах - каталог проекта. Число печатается
    всегда, поэтому «измерили и места мало» отличается от «измерить не
    удалось»."""
    fails = []
    for label in _VOLUMES:
        free = mem_status_gb(label)
        print("    %s: %.2f GB free" % (label, free))
        if free <= 0:
            fails.append("%s: free space could not be measured, and a check "
                         "that measured nothing must not report success"
                         % label)
        elif free < 5.0:
            fails.append("%s: has only %.2f GB free - this already happened "
                         "once" % (label, free))
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
    # Раньше здесь требовалось прочитать свободное место на `B:` через
    # PowerShell, то есть на любой не-Windows машине группа падала. Но
    # решение принимается по ЦИФРАМ, а цифры не зависят от буквы диска.
    measured = [(label, mem_status_gb(label)) for label in _VOLUMES]
    for label, free in measured:
        print("    %s: %.2f GB free" % (label, free))
    if not any(free > 0 for _label, free in measured):
        fails.append("no volume reported free space, so there is nothing to "
                     "decide the move on: %r" % (measured,))
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
    failed = skipped_n = 0
    for label, fn in TESTS:
        try:
            outcome = fn()
        except Exception as exc:  # noqa: BLE001
            outcome = ["%s: %s" % (type(exc).__name__, exc)]
        if isinstance(outcome, Skip):
            skipped_n += 1
            print("SKIP  %s" % label)
            print("        - %s" % outcome.reason)
            continue
        fails = list(outcome or [])
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)
    print()
    passed = len(TESTS) - skipped_n - failed
    # Код возврата и машинночитаемая строка - из закона. Шесть групп читают
    # только репозиторий, поэтому ноль выполненных здесь означает поломку
    # самого набора; пропуском это объявляться не может.
    return law.finish(len(TESTS), passed, failed, skipped_n)


if __name__ == "__main__":
    raise SystemExit(main())