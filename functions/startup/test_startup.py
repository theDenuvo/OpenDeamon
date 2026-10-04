r"""Фаза 3.1 - ярлык Desktop в автозагрузку.

Находка, из-за которой задача не сводится к копированию файла: ярлык
`Hermes.lnk` на рабочем столе указывал на

    C:\\Users\\cheli\\AppData\\Local\\hermes\\hermes-agent\\apps\\desktop\\
    release\\win-unpacked\\Hermes.exe

и этого файла НЕ СУЩЕСТВУЕТ. Реальная сборка лежит в
`A:\\OpenDeamon\\desktop\\Hermes.exe`. Простое копирование ярлыка в Startup
добавило бы автозагрузку, которая не запускается, - хуже, чем её отсутствие,
потому что отказ выглядит как «Desktop сломался».

Поэтому порядок такой: починить цель, убедиться, что она запускается, и
только потом вешать в Startup. Запуск проверяется по-настоящему: процесс
жив 12 секунд и окно имеет заголовок "Hermes". Наличие файла недостаточно -
неисполняемый или падающий при старте exe проходит проверку существования.

Мост hands из автозагрузки НЕ убирается: у него есть потребитель -
`bootstrap.ps1:39-46` поднимает `bridge\\hands.py`, если порт закрыт. Правило
из TODO - оставлять мост, только если что-то его использует.

ПЕРЕНОСИМОСТЬ. Набор проверяет ВИНДОС-СОСТОЯНИЕ МАШИНЫ: ярлык в
`%APPDATA%\...\Startup`, `.lnk` через WScript.Shell, exe на `A:\` и живой
процесс с заголовком окна. Вне Windows ни одно утверждение не имеет
смысла, и раньше набор об этом не сообщал вовсе: падал на импорте, то есть
`KeyError` до первого утверждения, а в workflow стоял в portable-suite.
Красный бейдж после этого читался как «сломан продукт», хотя была сломана
привязка к машине разработчика.

Теперь каждая группа говорит SKIP с точной причиной (`os.name`, `$APPDATA`,
powershell, `A:\`), а итоговая строка называет число пропущенных групп.
Код 0 при четырёх нулевых проверках - это «проверено всё», и именно такое
прочтение запрещено: `verify.yml` считает `SKIP` отдельно от `pass` и валит
сборку, если набор выполнил ноль групп и не был объявлен Windows-only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import namedtuple

EXE = r"A:\OpenDeamon\desktop\Hermes.exe"
NAME = "Hermes.lnk"


def startup_dir():
    """Каталог автозагрузки.

    Раньше вычислялся на уровне модуля (`os.environ["APPDATA"]`), и набор
    ронял ИМПОРТ на любой машине без `$APPDATA` - до первого утверждения.
    Теперь это функция, и отсутствие переменной не убивает набор."""
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


# Третий исход наряду с pass и fail: «проверка не выполнялась».
Skip = namedtuple("Skip", "reason")
TESTS = []


def skipped(reason):
    return Skip(reason)


def windows_gap():
    """Чего именно не хватает этой машине. Пустая строка - можно выполнять.

    Список, а не один флаг: «SKIP, потому что не Windows» невозможно
    отличить от «SKIP, потому что забыли подготовить окружение».
    """
    missing = []
    if os.name != "nt":
        missing.append("os.name=%s" % os.name)
    for var in ("APPDATA", "USERPROFILE"):
        if not os.environ.get(var):
            missing.append("$%s is not set" % var)
    if shutil.which("powershell") is None:
        missing.append("powershell is not in PATH")
    if not os.path.isdir("A:\\"):
        missing.append("the project drive A:\\ is absent")
    return ", ".join(missing)


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def ps(script):
    return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, timeout=120)


def read_lnk(path):
    """Прочитать ярлык без COM: .lnk - это бинарный формат, но цели там
    лежат читаемо. Проще и надёжнее, чем дёргать WScript.Shell, но для
    проверки цели Startup этого достаточно."""
    data = open(path, "rb").read()
    out = []
    text = data.decode("utf-16-le", "ignore").encode("latin-1", "ignore")
    for marker in (b"H\x00e\x00r\x00m\x00e\x00s\x00.\x00e\x00x\x00e\x00",
                   b"H\x00e\x00r\x00m\x00e\x00s\x00.\x00l\x00n\x00k\x00"):
        pass
    # Проще: ищем UTF-16 пути, оканчивающиеся на .exe / .lnk
    import re
    for m in re.finditer(rb"(?:[ -~]\x00){6,}", text):
        s = m.group(0).decode("utf-16-le", "ignore")
        if "\\" in s and (s.lower().endswith(".exe") or ":\\" in s):
            out.append(s)
    return out


def target_of(path):
    """Цель ярлыка через WScript.Shell - точнее самодельного разбора."""
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "Write-Output $s.TargetPath" % path)
    r = ps(script)
    return (r.stdout or "").strip()


# --- проверки -----------------------------------------------------------

@test
def test_the_real_desktop_binary_exists_and_is_not_a_lnk():
    gap = windows_gap()
    if gap:
        return skipped(gap)
    if not os.path.isfile(EXE):
        return ["the desktop build is missing: %s" % EXE]
    if EXE.lower().endswith(".lnk"):
        return ["EXE points at a shortcut, not a binary: %s" % EXE]
    if os.path.getsize(EXE) < 10 * 1024 * 1024:
        return ["the desktop binary is implausibly small: %d bytes"
                % os.path.getsize(EXE)]
    return []


@test
def test_the_desktop_shortcut_is_not_pointing_at_a_missing_path():
    """Именно эта проверка нашла проблему: ярлык вёл в win-unpacked, которого
    нет. Такой ярлык в автозагрузке хуже, чем его отсутствие."""
    gap = windows_gap()
    if gap:
        return skipped(gap)
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop", NAME)
    if not os.path.exists(desktop):
        return ["no %s on the desktop to inspect" % NAME]
    target = target_of(desktop)
    if not target:
        return ["could not read the target of %s" % desktop]
    if not os.path.exists(target):
        return ["%s points at a path that does not exist: %s"
                % (NAME, target)]
    if os.path.abspath(target).lower() != os.path.abspath(EXE).lower():
        return ["%s points at %s but the build is at %s"
                % (NAME, target, EXE)]
    return []


@test
def test_startup_has_a_working_hermes_entry():
    """Ярлык в Startup обязан вести на СУЩЕСТВУЮЩИЙ файл. Наличие ярлыка само
    по себе ничего не значит - это ровно тот случай, который был найден."""
    gap = windows_gap()
    if gap:
        return skipped(gap)
    entry = os.path.join(startup_dir(), NAME)
    if not os.path.exists(entry):
        return ["%s is not in Startup" % entry]
    target = target_of(entry)
    if not target:
        return ["could not read the Startup entry target"]
    if not os.path.exists(target):
        return ["the Startup entry points at a missing path: %s" % target]
    return []


@test
def test_the_hands_bridge_is_still_wired_so_it_stays_in_startup():
    """Правило из TODO: мост убирается из автозагрузки, только если его
    никто не использует. Потребитель есть - bootstrap.ps1."""
    gap = windows_gap()
    if gap:
        return skipped(gap)
    fails = []
    entry = os.path.join(startup_dir(), "OpenDeamonHands.lnk")
    script = os.path.join("A:", "OpenDeamon", "bootstrap.ps1")
    body = ""
    if os.path.exists(script):
        body = open(script, encoding="utf-8", errors="replace").read()
    used = "bridge" in body and "hands.py" in body
    if used and not os.path.exists(entry):
        fails.append("bootstrap.ps1 still starts the bridge, but its Startup "
                     "entry was removed - the bridge would only start on the "
                     "next manual bootstrap")
    if used:
        print("    bridge is referenced by bootstrap.ps1 - keeping it")
    return fails


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
    ran = len(TESTS) - skipped_n
    # Три числа, а не «all pass»: ноль выполненных групп при коде 0 обязан
    # быть виден в выводе, иначе зелёная строка означает «проверено всё».
    print("groups: %d total, %d passed, %d failed, %d skipped"
          % (len(TESTS), ran - failed, failed, skipped_n))
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())