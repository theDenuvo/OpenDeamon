"""ЗАКОН РЕПОЗИТОРИЯ: ноль проверок — не успех.

До этого правило жило в трёх местах сразу: в каждом из трёх наборов был свой
`Skip` и своя логика кода возврата, в манифесте покрытия было поле
`allow_zero_checks`, а список объявленных пропусков был константой в shell
шага CI. Пока правило живёт в трёх местах, оно не является правилом: следующий
набор напишет своё четвёртое.

Поэтому здесь - ОДНО место. Всё, что нужно знать про «ноль проверок»,
определено здесь и им же пользуются наборы и workflow.

КОДЫ ВОЗВРАТА
--------------

    0  PASS   проверки выполнялись, ни одна не провалилась
    1  FAIL   проверка провалилась: продукт или код ведут себя неверно
    2  SKIP   проверок не было, и это ОБЪЯВЛЕННЫЙ пропуск: среда не дала
              проверить (нет Windows, нет GPU, нет локальной модели)
    3  ENV    проверок не было, и это НЕ объявлено: окружение сломало прогон
              так, что перечислить проверки не удалось

Разделение 1 и 3 - это требование задачи буквально: «код возврата "среда не
дала проверить" отличается от "продукт сломан"». Набор, который не смог
разобрать свой собственный вход, не должен выглядеть как набор, у которого
сломалась проверка: первое чинят окружением, второе - кодом.

Почему SKIP не 0. Требование приёмки (3) без исключений: НИ ОДИН набор
репозитория не имеет права вернуть 0, выполнив ноль проверок. Раньше
`test_startup.py` именно так и делал: печатал четыре `SKIP` с причиной и
выходил с кодом 0, а workflow называл это «объявленным пропуском» по
списку в shell. Теперь такой набор возвращает 2, и это перестаёт быть
условным договором: код 0 при нуле выполненных групп теперь означает ошибку
самого набора, и workflow проверяет это буквально.

МАШИНОЧИТАЕМАЯ СТРОКА
----------------------

Каждый набор, прошедший через `finish()`, печатает ровно одну строку:

    VERIFY-RESULT total=7 passed=6 failed=0 skipped=1

Она существует, чтобы workflow считывал числа, а не вылавливал их из
проки. Число `pass` в тексте - это проза, которую можно случайно испортить
правкой формулировки; эта строка - контракт.

ОДИН СПИСОК ОБЪЯВЛЕННЫХ ПРОПУСКОВ
--------------------------------

`declared_skips()` читает манифест покрытия, а не shell. Раньше список был
константой в шаге CI - и это был ровно тот же класс дыры, который закрыт для
списка наборов: удалить строку молча и потерять проверку, не получив ошибки.

ОДИН ВЕРДИКТ
------------

`verdict(rc, ran, declared)` решает, что набор заслужил. Шаг CI не пишет это
условие сам - он спрашивает закон и печатает ответ. Иначе получилось бы ровно
то, что запрещает эта задача: правило, написанное в двух местах, из которых
одно молча разъезжается с другим. Здесь проверяется и код возврата, и число
выполненных проверок, и объявление в манифесте, - поэтому демонстрация
«пустой набор роняет сборку, объявленный пропуск - нет» воспроизводится
тестом, а не руками.
"""
from __future__ import annotations

import json
import os
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "coverage-manifest.json")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2
EXIT_ENV = 3

EXIT_NAMES = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_SKIP: "SKIP",
              EXIT_ENV: "ENV"}
LEGAL_EXITS = frozenset(EXIT_NAMES)

RESULT_PREFIX = "VERIFY-RESULT"

# Третий исход наряду с pass и fail: проверка не выполнялась, и это сказано.
# Определено здесь, а не в каждом наборе: см. докстринг модуля.
Skip = namedtuple("Skip", "reason")


def skipped(reason):
    """Объявленный пропуск группы. Причина обязательна: молчаливый пропуск -
    это ошибка, которую потом ищут в чужом месте."""
    return Skip(reason)


def env_error(reason):
    """Окружение не дало даже перечислить проверки. Код 3, а не 1."""
    return EXIT_ENV, reason


def summarise(total, passed, failed, skipped_n, reason="") -> str:
    """Строка контракта для workflow. Одна на набор, всегда последняя."""
    return "%s total=%d passed=%d passed_ran=%d failed=%d skipped=%d%s" % (
        RESULT_PREFIX, total, passed, passed + failed, failed, skipped_n,
        (" reason=%s" % reason.replace(" ", "_")) if reason else "")


def finish(total, passed, failed, skipped_n, reason="", stream=None) -> int:
    """Единственная точка, где набор решает свой код возврата.

    Параметры - числа, а не списки: их считает вызывающий, потому что у каждого
    набора своя форма регистрации групп. Закон проверяется здесь, а не в
    каждом наборе, поэтому обойти его нельзя случайно.

    Правило ровно одно и оно жёсткое: `EXIT_PASS` возвращается только если
    выполнена хотя бы одна проверка И числа сходятся. Ноль выполненных - это
    `EXIT_SKIP`, и объявлять его должен манифест покрытия, который проверяет
    `functions/meta/test_coverage_manifest.py`."""
    out = stream or sys.stdout
    ran = passed + failed
    # Арифметика самого набора проверяется прежде, чем из неё делается вывод.
    # Дыру нашёл этот же набор: `finish(total=4, skipped=9, failed=0)` даёт
    # passed = -5 и ran = -5, а `ran == 0` ложно - набор, не сумевший
    # посчитать свои группы, возвращал УСПЕХ. Проверять нечего, если из чисел
    # не сходится арифметика, поэтому несходящееся - не успех, а поломка
    # самого набора (EXIT_ENV).
    if ran < 0 or passed < 0 or total != passed + failed + skipped_n:
        code = EXIT_ENV
    elif ran == 0:
        code = EXIT_SKIP
    elif failed:
        code = EXIT_FAIL
    else:
        code = EXIT_PASS
    print(summarise(total, passed, failed, skipped_n, reason or
                    EXIT_NAMES[code]), file=out)
    if code == EXIT_ENV:
        print("ENV: %d passed + %d failed + %d skipped != %d declared groups "
              "- the suite miscounts itself, so this run concludes nothing"
              % (passed, failed, skipped_n, total), file=out)
    if code == EXIT_SKIP and not reason:
        print("note: zero checks ran and no reason was given; the manifest "
              "must declare this suite with allow_zero_checks", file=out)
    return code


def windows_gap() -> str:
    """Чего не хватает этой машине. Пустая строка - можно выполнять.

    Живёт в законе, а не в наборе, потому что это и есть «причина пропуска»,
    а причина обязана быть машиночитаемой: её читает манифест покрытия и она
    же попадает в отчёт CI. Список, а не один флаг: «SKIP, потому что не
    Windows» невозможно отличить от «SKIP, потому что забыли окружение»."""
    import shutil as _shutil
    missing = []
    if os.name != "nt":
        missing.append("os.name=%s" % os.name)
    for var in ("APPDATA", "USERPROFILE"):
        if not os.environ.get(var):
            missing.append("$%s is not set" % var)
    if _shutil.which("powershell") is None:
        missing.append("powershell is not in PATH")
    if not os.path.isdir("A:\\"):
        missing.append("the project drive A:\\ is absent")
    return ", ".join(missing)


def env_finish(reason, stream=None) -> int:
    """Прогон не состоялся по вине окружения. Ноль проверок, код 3."""
    out = stream or sys.stderr
    print("ENV: %s" % reason, file=out)
    print(summarise(0, 0, 0, 0, reason or "environment"), file=sys.stdout)
    return EXIT_ENV


def parse_result(text):
    """Разбор машинночитаемой строки. `None`, если её нет.

    Терпимость к мусору нужна, потому что строка может прийти из stdout
    набора вместе с его обычным выводом."""
    for line in (text or "").splitlines():
        if not line.startswith(RESULT_PREFIX):
            continue
        fields = {}
        for token in line[len(RESULT_PREFIX):].split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key] = value
        try:
            return {"total": int(fields.get("total", 0)),
                    "passed": int(fields.get("passed", 0)),
                    "ran": int(fields.get("passed_ran", 0)),
                    "failed": int(fields.get("failed", 0)),
                    "skipped": int(fields.get("skipped", 0)),
                    "reason": fields.get("reason", "")}
        except ValueError:
            continue
    return None


def declared_skips(manifest_path=None):
    """Наборы, которым манифест РАЗРЕШИЛ выполнить ноль проверок.

    Список читается из данных. Раньше он был константой в shell-шаге, и это
    был тот же класс дыры, что закрыт для списка наборов: удалить строку -
    и потерять проверку, не получив ошибки."""
    path = manifest_path or MANIFEST
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [e["path"] for e in data.get("suites") or []
            if e.get("allow_zero_checks")]


def verdict(rc: int, ran: int, declared: bool):
    """Что набор заслужил: `PASS`, `SKIP` или `FAIL` с причиной.

    Требуемое демонстрационное свойство живёт здесь, а не в условии `if`
    шага CI, по той же причине, по какой список пропусков переехал из shell
    в данные: правило, написанное дважды, рано или поздно разъезжается, и
    разъехавшаяся копия молчит. Теперь shell спрашивает закон и печатает
    ответ, поэтому проверить закон можно обычным тестом.

        verdict(0, 0, False) -> FAIL  # пустой набор врёт: роняет сборку
        verdict(2, 0, True)  -> SKIP  # объявленный пропуск: не роняет
        verdict(2, 0, False) -> FAIL  # пропуск без объявления - тоже ложь
        verdict(3, 0, True)  -> FAIL  # поломка окружения не объявляется

    Порядок проверок неслучаен: сначала «ноль проверок», потому что именно
    этот случай запрещён законом без исключений, и только потом «код 0».
    """
    if ran == 0:
        if rc == EXIT_SKIP and declared:
            return "SKIP", "declared skip: zero checks is not a pass"
        if rc == EXIT_SKIP:
            return "FAIL", ("exited %d (skip) with zero checks but the "
                            "manifest does not declare allow_zero_checks for "
                            "it" % rc)
        if rc == EXIT_ENV:
            return "FAIL", ("exited %d: the environment broke the run, and an "
                            "environment break is not a declared skip" % rc)
        return "FAIL", ("exited %d having executed ZERO checks - no suite may "
                        "be green without checking anything" % rc)
    if rc == EXIT_PASS:
        return "PASS", "%d checks ran" % ran
    if rc == EXIT_ENV:
        return "FAIL", ("exited %d: the environment broke the run, and an "
                        "environment break is not a declared skip" % rc)
    if rc not in LEGAL_EXITS:
        return "FAIL", ("exited %d, which is not one of the law's codes (%s)"
                        % (rc, ", ".join("%d=%s" % (c, EXIT_NAMES[c])
                                         for c in sorted(LEGAL_EXITS))))
    return "FAIL", "%d checks ran and the suite reported a failure (rc=%d)" % (
        ran, rc)


def main() -> int:
    """Точка входа для workflow.

    `--declared-skips` печатает список по одному на строку: шаг CI берёт его
    отсюда и не знает ни одного имени набора собственным текстом.

    `--verdict` читает из stdin `rc<TAB>ran<TAB>declared` и печатает
    `PASS`, `SKIP` или `FAIL <причина>`, возвращая код, пригодный для шага.
    """
    if "--declared-skips" in sys.argv[1:]:
        for path in declared_skips():
            print(path)
        return EXIT_PASS
    if "--verdict" in sys.argv[1:]:
        raw = sys.stdin.read().strip().split()
        if len(raw) != 3:
            print("FAIL: --verdict expects `rc ran declared` on stdin, got %r"
                  % raw, file=sys.stderr)
            return EXIT_FAIL
        try:
            rc, ran = int(raw[0]), int(raw[1])
        except ValueError:
            print("FAIL: --verdict got non-numeric rc/ran: %r" % raw,
                  file=sys.stderr)
            return EXIT_FAIL
        label, why = verdict(rc, ran, raw[2] in ("1", "true", "yes"))
        print(label if label == "PASS" else "%s %s" % (label, why))
        return EXIT_PASS if label != "FAIL" else EXIT_FAIL
    if "--codes" in sys.argv[1:]:
        for code in sorted(LEGAL_EXITS):
            print("%d %s" % (code, EXIT_NAMES[code]))
        return EXIT_PASS
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    print("usage: zero_checks_law.py [--declared-skips] [--verdict] [--codes]",
          file=sys.stderr)
    return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())