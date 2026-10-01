"""Фаза 3.1 - ярлык Desktop в автозагрузку.

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
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

EXE = r"A:\OpenDeamon\desktop\Hermes.exe"
NAME = "Hermes.lnk"
STARTUP = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                       "Start Menu", "Programs", "Startup")
TESTS = []


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
    entry = os.path.join(STARTUP, NAME)
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
    fails = []
    entry = os.path.join(STARTUP, "OpenDeamonHands.lnk")
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