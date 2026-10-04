"""Сам закон и его соблюдение. Набор-закон.

Пока «ноль проверок — не успех» был свойством трёх наборов, он был
соглашением: стоило написать четвёртый набор со своим `return 0`, и правило
перестало действовать, никто бы этого не заметил. Закон существует, потому
что (1) написан в одном месте, (2) это место проверяется гейтом, и (3)
демонстрация его силы воспроизводится автоматически, а не вручную.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ

1. **Код возврата выводится из числа выполненных проверок.** Ноль
   выполненных недостижим при коде 0 - не « discouraged», а недостижим:
   `finish()` не имеет пути, который вернул бы 0 после нуля проверок.

2. **Демонстрация из приёмки, исполняемая.** Два синтетических набора
   запускаются как настоящие дочерние процессы: один печатает «всё хорошо»
   и выходит с кодом 0, выполнив ноль проверок; второй возвращает код
   закона. Первый обязан быть признан провалом, второй — пропуском. Раньше
   это показывалось руками в терминале и через месяц никто не помнил,
   работает ли оно ещё.

3. **«Среда не дала проверить» отличается от «продукт сломан».** Коды 1 и 3
   не совпадают, и объявленный пропуск (2) отличается от поломки
   окружения (3), хотя обе означают «проверок не было». Иначе чинят не то.

4. **Вердикт живёт в законе, а не в shell.** Шаг CI спрашивает
   `verdict()`, и этот набор проверяет именно ту функцию, которой CI
   пользуется. Вторая копия условия в shell разъехалась бы с первой
   молча.

5. **Список объявленных пропусков — данные.** `declared_skips()` обязан
   совпадать с манифестом покрытия, и наборы, которым закон позволяет
   вернуть ноль проверок, обязаны импортировать закон.

Запуск:
    python functions/meta/test_zero_checks_law.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))

import zero_checks_law as law  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def suite_files():
    """Все наборы репозитория: и пропускающие, и нет.

    Обход всего `functions/`, а не только наборов из манифеста: набор, которого
    нет в манифесте, - это уже отдельная дыра, и ловить отсутствие закона
    нужно до того, как её заметят."""
    return sorted(p for p in (_ROOT / "functions").rglob("test_*.py"))


def run_child(body: str) -> subprocess.CompletedProcess:
    """Запустить синтетический набор как настоящий процесс.

    Именно процесс, а не вызов функции: закон проверяет код возврата, который
    видит CI, и подмена вызова проверила бы не то."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic_suite.py"
        # meta кладём в PYTHONPATH, чтобы синтетический набор видел закон
        # ровно так же, как настоящий.
        path.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n" % str(_HERE) + body,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_HERE)
        return subprocess.run([sys.executable, str(path)],
                              capture_output=True, text=True, timeout=120,
                              env=env, cwd=str(_ROOT))


# --------------------------------------------------------------------------
# 1. закон не может вернуть 0 после нуля проверок
# --------------------------------------------------------------------------

@test
def test_zero_checks_can_never_return_success():
    import io

    fails = []
    # Ни одна согласованная комбинация нулевого числа выполненных проверок не
    # даёт кода 0.
    for total, skipped_n in ((0, 0), (4, 4), (1, 1), (7, 7)):
        for failed in (0, 1):
            passed = total - skipped_n - failed
            code = law.finish(total, passed, failed, skipped_n,
                              stream=io.StringIO())
            if code == law.EXIT_PASS:
                fails.append("finish(total=%d, skipped=%d, failed=%d) returned "
                             "EXIT_PASS having executed zero checks - the law "
                             "forbids it" % (total, skipped_n, failed))
            elif code not in law.LEGAL_EXITS:
                fails.append("finish(... failed=%d) returned %d, which is not "
                             "one of the law's codes" % (failed, code))

    # Несходящаяся арифметика - тоже не успех. Дыру нашёл этот набор:
    # finish(total=4, skipped=9, failed=0) даёт passed=-5, ran=-5, и
    # `ran == 0` оказывается ложью - успех возвращал набор, который не сумел
    # посчитать собственные группы.
    for total, passed, failed, skipped_n in ((4, -5, 0, 9), (3, 0, -1, 0),
                                             (9, 8, 0, 0), (0, 1, 0, 0),
                                             (4, 4, 0, 2)):
        code = law.finish(total, passed, failed, skipped_n,
                          stream=io.StringIO())
        if code == law.EXIT_PASS:
            fails.append("finish(total=%d, passed=%d, failed=%d, skipped=%d) "
                         "returned EXIT_PASS although the numbers do not add "
                         "up - a suite that miscounts itself proves nothing"
                         % (total, passed, failed, skipped_n))
        elif code != law.EXIT_ENV:
            fails.append("finish(total=%d, passed=%d, failed=%d, skipped=%d) "
                         "returned %d, expected EXIT_ENV for inconsistent "
                         "numbers" % (total, passed, failed, skipped_n, code))

    # И наоборот: выполненная хотя бы одна проверка без провалов даёт 0.
    if law.finish(3, 3, 0, 0, stream=io.StringIO()) != law.EXIT_PASS:
        fails.append("three passing checks did not produce EXIT_PASS")
    if law.finish(3, 2, 1, 0, stream=io.StringIO()) != law.EXIT_FAIL:
        fails.append("one failing check did not produce EXIT_FAIL")
    return fails


@test
def test_the_result_line_is_a_contract_the_workflow_can_parse():
    """Машинночитаемая строка обязана быть разбираемой.

    Пока числа вылавливались из вывода регуляркой `^pass`, правка формулировки
    в наборе могла обнулить счётчик, и набор с 40 группами отчитался бы как
    выполнивший ноль - то есть тихо стал бы пропуском."""
    import io

    fails = []
    sink = io.StringIO()
    law.finish(5, 4, 0, 1, stream=sink)
    line = law.summarise(5, 4, 0, 1)
    parsed = law.parse_result(line)
    if parsed is None:
        fails.append("summarise() produced a line the workflow cannot parse: "
                     "%r" % line)
        return fails
    for field, want in (("total", 5), ("passed", 4), ("ran", 4), ("failed", 0),
                        ("skipped", 1)):
        if parsed[field] != want:
            fails.append("parsed %s=%r, expected %d from %r"
                         % (field, parsed[field], want, line))
    if law.parse_result("nothing here") is not None:
        fails.append("parse_result() invented a result out of ordinary output")
    if law.parse_result("VERIFY-RESULT total=x passed=y") is not None:
        fails.append("parse_result() accepted a malformed result line instead "
                     "of rejecting it")
    # число выполненных обязано совпадать с суммой, а не быть отдельной
    # величиной, которую можно забыть обновить
    if parsed["ran"] != parsed["passed"] + parsed["failed"]:
        fails.append("ran disagrees with passed+failed")
    return fails


# --------------------------------------------------------------------------
# 2. демонстрация из приёмки
# --------------------------------------------------------------------------

@test
def test_an_artificially_empty_suite_fails_and_a_declared_skip_does_not():
    """Демонстрация, исполняемая каждый прогон, а не показанная однажды.

    (а) Набор-лжец: печатает «всё хорошо», не выполняет ни одной проверки и
        выходит с кодом 0. Закон обязан признать это провалом - ровно тот
        случай, который раньше проходил зелёным.

    (б) Набор с объявленным пропуском: тоже ноль проверок, но код возврата
        берётся из закона и пропуск объявлен в манифесте. Закон обязан
        признать это пропуском, а не провалом.

    Оба запускаются дочерними процессами: закон проверяет код возврата,
    который видит CI."""
    fails = []

    liar = run_child(
        "print('all good, nothing to see here')\n"
        "raise SystemExit(0)\n"
    )
    label, why = law.verdict(liar.returncode, 0, False)
    if label != "FAIL":
        fails.append("a suite that checked nothing and exited 0 was judged "
                     "%r (%s) - zero checks is not success" % (label, why))
    if "ZERO" not in why:
        fails.append("the refusal to pass an empty suite does not name the "
                     "problem: %r" % why)

    real = run_child(
        "import zero_checks_law as law\n"
        "print('SKIP  the demonstration machine is neither Windows nor GPU')\n"
        "raise SystemExit(law.finish(1, 0, 0, 1, reason='no_windows_no_gpu'))\n"
    )
    if real.returncode != law.EXIT_SKIP:
        fails.append("a declared skip exited %d, expected the law's EXIT_SKIP=%d"
                     % (real.returncode, law.EXIT_SKIP))
    declared = "functions/startup/test_startup.py" in law.declared_skips()
    label, why = law.verdict(real.returncode, 0, declared)
    if label != "SKIP":
        fails.append("a declared skip was judged %r (%s) - a declared skip "
                     "must not redden the build" % (label, why))

    # объявленный пропуск, который никто не объявлял, - это ложь, а не пропуск
    label, _ = law.verdict(real.returncode, 0, False)
    if label != "FAIL":
        fails.append("an undeclared skip was judged %r - silence must not buy "
                     "a skip" % label)
    return fails


# --------------------------------------------------------------------------
# 3. код возврата отличает продукт от среды
# --------------------------------------------------------------------------

@test
def test_the_environment_code_is_not_the_broken_product_code():
    """«Среда не дала проверить» и «продукт сломан» - разные коды.

    Проверка провалилась - чинят код. Прогон не состоялся - чинят окружение.
    Один код на оба случая означает, что в инциденте ищут не там, и прогон
    годами числится зелёным, потому что «ну, наверное, упал»."""
    fails = []
    if law.EXIT_FAIL == law.EXIT_ENV:
        fails.append("EXIT_FAIL and EXIT_ENV are the same code (%d), so a "
                     "broken environment is indistinguishable from a broken "
                     "product" % law.EXIT_FAIL)
    if law.EXIT_SKIP == law.EXIT_ENV:
        fails.append("EXIT_SKIP and EXIT_ENV are the same code (%d), so a "
                     "declared skip is indistinguishable from an environment "
                     "break - and the former must not redden the build while "
                     "the latter must" % law.EXIT_SKIP)
    if law.EXIT_PASS != 0:
        fails.append("EXIT_PASS is %d, but CI and every shell in the "
                     "repository read 0 as success" % law.EXIT_PASS)
    if 0 not in law.LEGAL_EXITS:
        fails.append("the law does not define code 0 at all, yet every shell "
                     "in the repository reads 0 as success")
    # объявленный пропуск не должен ронять сборку, поломка среды - должна
    if law.verdict(law.EXIT_SKIP, 0, True)[0] != "SKIP":
        fails.append("a declared skip does not earn SKIP")
    if law.verdict(law.EXIT_ENV, 0, True)[0] != "FAIL":
        fails.append("an environment break is being treated as a declared "
                     "skip; it must fail the build instead")
    if law.verdict(law.EXIT_FAIL, 4, False)[0] != "FAIL":
        fails.append("a failing suite does not earn FAIL")
    if law.verdict(law.EXIT_PASS, 4, False)[0] != "PASS":
        fails.append("a passing suite does not earn PASS")
    if law.verdict(99, 4, False)[0] != "FAIL":
        fails.append("a suite exiting with a code the law does not define was "
                     "not failed - unknown codes must never read as success")
    return fails


# --------------------------------------------------------------------------
# 4. закон написан один раз
# --------------------------------------------------------------------------

@test
def test_no_suite_redefines_the_law_for_itself():
    """Ни один набор не определяет собственные коды и собственный `Skip`.

    Именно этим закон отличается от соглашения. Пока `Skip` жил в трёх
    наборах, четвёртый набор написал бы четвёртую копию и был бы прав по
   -своему; заметить расхождение можно было бы только по факту - когда
    зелёный набор уже уехал в master."""
    fails = []
    forbidden = (
        (re.compile(r'^\s*Skip\s*=\s*namedtuple\(', re.M),
         "определяет собственный Skip вместо zero_checks_law.Skip"),
        (re.compile(r'^\s*def skipped\(', re.M),
         "определяет собственный skipped() вместо zero_checks_law.skipped"),
        (re.compile(r'^\s*EXIT_(PASS|FAIL|SKIP|ENV)\s*=', re.M),
         "определяет собственный код возврата вместо закона"),
    )
    for path in suite_files():
        text = path.read_text(encoding="utf-8")
        for pattern, why in forbidden:
            m = pattern.search(text)
            if m:
                rel = path.relative_to(_ROOT)
                fails.append("%s %s (line %d) - the law is written once, in "
                             "functions/meta/zero_checks_law.py"
                             % (rel, why, text[:m.start()].count("\n") + 1))
    # набор, который умеет пропускать, обязан импортировать закон, иначе он
    # объявляет пропуск своим собственным кодом, а не кодом закона
    for path in suite_files():
        text = path.read_text(encoding="utf-8")
        if "zero_checks_law" not in text:
            continue
        if not re.search(r'law\.finish\(|\blaw\.finish\(', text):
            fails.append("%s imports the law but never routes its return code "
                         "through law.finish() - the law would then only be a "
                         "comment" % path.relative_to(_ROOT))
    return fails


@test
def test_the_declared_skip_list_comes_from_the_manifest_not_from_shell():
    """Список объявленных пропусков - данные, а не константа в shell.

    Пока список жил в шаге CI, объявить пропуск значило молча отредактировать
    shell - и это выглядело частью шага, а не изменением правил."""
    fails = []
    import json
    manifest = json.loads((_HERE / "coverage-manifest.json")
                          .read_text(encoding="utf-8"))
    from_manifest = sorted(e["path"] for e in manifest["suites"]
                           if e.get("allow_zero_checks"))
    from_law = sorted(law.declared_skips())
    if from_manifest != from_law:
        fails.append("declared_skips() returns %r but the manifest declares "
                     "%r - the workflow asks the law, so the two must agree"
                     % (from_law, from_manifest))
    workflow = (_ROOT / ".github" / "workflows" / "verify.yml").read_text(
        encoding="utf-8")
    # присваивание может быть разбито переносом строки, поэтому берём его
    # целиком, а не только первую строку
    m = re.search(r'^[ \t]*declared_skip=(?:.*\\\n)*.*$', workflow, re.M)
    if not m:
        fails.append("verify.yml no longer defines declared_skip at all - the "
                     "law is then not consulted and any suite could claim a "
                     "skip")
    elif "zero_checks_law.py --declared-skips" not in m.group(0):
        fails.append("verify.yml sets declared_skip from %r instead of asking "
                     "functions/meta/zero_checks_law.py - the list is back to "
                     "being a shell constant" % m.group(0).strip()[:120])
    # и набор, которому закон разрешает ноль проверок, обязан быть в CI:
    # иначе объявление нечего проверять
    for entry in manifest["suites"]:
        if entry.get("allow_zero_checks") and not entry.get("ci"):
            fails.append("%s declares allow_zero_checks but is not in CI, so "
                         "the declaration is never exercised" % entry["path"])
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

    passed = len(TESTS) - failed
    return law.finish(len(TESTS), passed, failed, 0,
                      reason="the law's own suite: it can only pass by having "
                             "run every group above")


if __name__ == "__main__":
    raise SystemExit(main())
