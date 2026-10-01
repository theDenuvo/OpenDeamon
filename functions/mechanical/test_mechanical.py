"""Тесты Слоя 4 - механической проверки.

Написаны так, чтобы ломаться при ослаблении правил, а не при опечатке.
Особенно важны два класса проверок:

  - подмена теста обязана давать ESCALATE, а не rework (иначе воркер получает
    второй шанс дописать тест под свой код - ровно тот путь, который стоил
    $1,234.50);
  - INFRA_FAILURE и MECHANICAL_FAILURE обязаны различаться ПО ДОКАЗАТЕЛЬСТВУ,
    иначе сетевая проблема съедает цикл rework, а красный тест отправляется
    менять маршрут.

Реальные git-репозитории и настоящие worktree, без подделок: правило 4
(чистый прогон в отдельной папке) иначе нечего проверять.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)          # .../functions
_ROOT = os.path.dirname(_PROJ)           # корень проекта
for _d in ("baseline", "mechanical", "specgate"):
    _p = os.path.join(_PROJ, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseline as bl  # noqa: E402
import mechanical as mech  # noqa: E402

TESTS = []
DT = tempfile.gettempdir()


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def make_repo(prefix="mech-"):
    root = tempfile.mkdtemp(prefix=prefix)
    proj = os.path.join(root, "proj")
    os.makedirs(proj)
    git("init", "-q", cwd=proj)
    git("config", "user.email", "t@t", cwd=proj)
    git("config", "user.name", "t", cwd=proj)
    with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n")
    with open(os.path.join(proj, "test_app.py"), "w", encoding="utf-8") as f:
        f.write("import app\n\n\ndef test_add():\n"
                "    assert app.add(1, 2) == 3\n")
    git("add", "-A", cwd=proj)
    git("commit", "-q", "-m", "base", cwd=proj)
    return root, proj


def barrier(proj, home):
    os.environ["HERMES_HOME"] = home
    bar = bl.create_barrier(proj, spec_id="mech")
    return bar


# --------------------------------------------------------------------------
# типология фейлов: действие важнее метки
# --------------------------------------------------------------------------

@test
def test_every_failure_kind_has_the_prescribed_action():
    fails = []
    want = {
        mech.SPEC_FAILURE: mech.REWORK_SPEC,
        mech.MECHANICAL_FAILURE: mech.REWORK_CODE,
        mech.TEST_TAMPERING: mech.ESCALATE,
        mech.REVIEW_FAILURE: mech.REWORK_CODE,
        mech.INFRA_FAILURE: mech.CHANGE_ROUTE,
        mech.WORKER_CRASH: mech.ROLLBACK,
    }
    for kind, action in want.items():
        got = mech.action_for(kind)
        if got != action:
            fails.append("%s -> %s, expected %s" % (kind, got, action))
    return fails


@test
def test_tampering_and_crash_never_allow_a_rerun():
    fails = []
    for kind in (mech.TEST_TAMPERING, mech.WORKER_CRASH):
        if mech.rerun_allowed(kind) is not False:
            fails.append("%s must never authorise a rerun" % kind)
    for kind in (mech.MECHANICAL_FAILURE, mech.INFRA_FAILURE):
        if mech.rerun_allowed(kind) is not True:
            fails.append("%s should leave the rerun decision open" % kind)
    return fails


# --------------------------------------------------------------------------
# правило 1: подмена тестов
# --------------------------------------------------------------------------

@test
def test_modified_baseline_test_is_tampering_and_escalates():
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        target = os.path.join(wt, "test_app.py")
        with open(target, encoding="utf-8") as f:
            body = f.read()
        # Ослабляем ассерт - длина файла меняется, хэш по содержимому обязан это поймать.
        with open(target, "w", encoding="utf-8") as f:
            f.write(body.replace("== 3", "== 4"))
        out = mech.check_tampering(bar, wt)
        if out["passed"]:
            fails = ["a weakened assertion passed as clean"]
        else:
            fails = []
            if out["verdict"] != mech.TEST_TAMPERING:
                fails.append("verdict %s" % out["verdict"])
            if out.get("action") != mech.ESCALATE:
                fails.append("action %s, expected ESCALATE" % out.get("action"))
            if out.get("rerun_allowed") is not False:
                fails.append("tampering must not authorise a rerun")
            if not out["modified"]:
                fails.append("the modified file was not named: %s" % out["modified"])
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_deleted_baseline_test_is_tampering():
    """Ослабить проверку можно и выбросив её - удаление равнозначно подмене."""
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        os.remove(os.path.join(wt, "test_app.py"))
        out = mech.check_tampering(bar, wt)
        fails = []
        if out["passed"]:
            fails.append("deleting a baseline test passed as clean")
        if out["verdict"] != mech.TEST_TAMPERING:
            fails.append("verdict %s" % out["verdict"])
        if not out["deleted"]:
            fails.append("the deleted file was not named")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_new_test_file_is_allowed_not_tampering():
    """Рост числа тестов разрешён явно: защита идёт от неизменяемости baseline."""
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        with open(os.path.join(wt, "test_new.py"), "w", encoding="utf-8") as f:
            f.write("def test_more():\n    assert True\n")
        out = mech.check_tampering(bar, wt)
        fails = []
        if not out["passed"]:
            fails.append("a NEW test file was treated as tampering: %s"
                         % out["modified"])
        if not out["added"]:
            fails.append("the new file was not reported as added")
        if out["added"] and not out["new_tests_allowed"]:
            fails.append("new tests are not marked allowed")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# правило 2 и 3: факт, а не заявление; потолок UNVERIFIABLE
# --------------------------------------------------------------------------

@test
def test_unverifiable_criterion_never_passes_and_blocks_verified():
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        spec = {"criteria": [
            {"id": "a", "text": "looks right to a human", "method": "manual",
             "severity": "LOW"}]}
        out = mech.run(bar["ledger_path"], wt, spec=spec, do_commit=False)
        if out.get("status_after") != "judged":
            fails.append("a manual criterion yielded status_after=%s"
                         % out.get("status_after"))
        if out.get("unverifiable", 0) < 1:
            fails.append("the manual criterion was not counted as unverifiable")
        if out.get("passed") and out.get("status_after") == "verified":
            fails.append("mechanical reported verified despite UNVERIFIABLE")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_worker_claim_without_a_command_is_not_a_pass():
    """Критерий, заявленный как выполненный, но без проверяемой команды,
    не может дать PASS - иначе доверяем словам воркера."""
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        spec = {"criteria": [
            {"id": "a", "text": "the worker says the fix works",
             "method": "command", "check": "", "severity": "LOW"}]}
        out = mech.run(bar["ledger_path"], wt, spec=spec, do_commit=False)
        if out.get("status_after") == "verified":
            fails.append("a claim with no checkable command produced 'verified'")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# правило 4: чистый прогон в отдельной папке, код возврата и лог
# --------------------------------------------------------------------------

@test
def test_clean_run_uses_a_separate_dir_and_records_exit_and_log():
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        shutil.copy(os.path.join(proj, "test_app.py"), os.path.join(wt, "test_app.py"))
        spec = {"criteria": [{"id": "a", "text": "unit tests", "method": "command",
                              "check": "%s -m unittest discover -q" % sys.executable,
                              "severity": "LOW"}]}
        clean = mech.run_clean(wt, spec=spec)
        if clean.get("separate_dir") is not True:
            fails.append("the clean run did not declare a separate dir")
        if not clean.get("runs"):
            fails.append("nothing was run")
            return fails
        if os.path.abspath(clean["sandbox"]) == os.path.abspath(wt):
            fails.append("the clean run happened in the worktree itself")
        r = clean["runs"][0]
        if "exit_code" not in r:
            fails.append("no exit code recorded")
        if "log" not in r:
            fails.append("no log recorded")
        if not os.path.exists(os.path.join(clean["sandbox"], r["log"])):
            fails.append("the recorded log does not exist on disk")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_criterion_with_a_failing_check_is_not_verified():
    """ПРИЁМОЧНЫЙ ТЕСТ Р1 - тот самый обход, который нашёл планировщик.

    Критерий «add складывает два числа» с проверкой, которая НАМЕРЕННО падает
    на баге add(2,3)!=5, при этом все тесты проекта зелёные. До починки
    `run_clean` вызывался без `spec`, ветка `method == "command"` была мёртвым
    кодом, и результат был `verified` - то есть «доказан» критерий, который
    никто не проверял. Ровно тот класс подмены, против которого построен
    проект."""
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        shutil.copy(os.path.join(proj, "test_app.py"), os.path.join(wt, "test_app.py"))
        # Настоящий баг в коде: add возвращает a+b-1. Проверка требует
        # ПРАВИЛЬНОГО поведения, поэтому она обязана упасть. (Проверка с
        # неверным ожиданием тут не годится - она прошла бы по случайности.)
        app = os.path.join(wt, "app.py")
        with open(app, encoding="utf-8") as f:
            good = f.read()
        with open(app, "w", encoding="utf-8") as f:
            f.write(good.replace("return a + b", "return a + b - 1"))
        checker = os.path.join(wt, "check_add.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write("import app\n"
                    "assert app.add(2, 3) == 5, 'add(2,3) must be 5'\n")
        spec = {"criteria": [{"id": "add", "text": "add складывает два числа",
                             "method": "command",
                             "check": "%s check_add.py" % sys.executable,
                             "severity": "LOW"}]}
        out = mech.run(bar["ledger_path"], wt, spec=spec, do_commit=False)
        if out.get("status_after") == "verified":
            fails.append("a criterion whose check FAILS was reported verified")
        if out.get("passed"):
            fails.append("the gate passed with a failing criterion check")
        table = out.get("criteria_evidence") or []
        if not table:
            fails.append("no per-criterion evidence was recorded")
        else:
            row = table[0]
            if row.get("id") != "add":
                fails.append("evidence names %r" % row.get("id"))
            if row.get("satisfied") is not False:
                fails.append("evidence says satisfied=%r" % row.get("satisfied"))
            if row.get("exit_code") == 0:
                fails.append("the failing check reported exit 0")
            if not row.get("log"):
                fails.append("no log recorded for the criterion check")
        if out.get("kind") != mech.MECHANICAL_FAILURE:
            fails.append("kind %s, expected MECHANICAL_FAILURE" % out.get("kind"))
        if out.get("action") != mech.REWORK_CODE:
            fails.append("action %s, expected REWORK_CODE" % out.get("action"))
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_criterion_with_a_passing_check_is_proven():
    """Обратная сторона: исполняемый и зелёный критерий обязан быть доказан,
    иначе починка превратилась бы в «никогда не verified»."""
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        shutil.copy(os.path.join(proj, "test_app.py"), os.path.join(wt, "test_app.py"))
        checker = os.path.join(wt, "check_ok.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write("import app\nassert app.add(2, 3) == 5\n"
                    "assert app.add(1, 2) == 3\n")
        spec = {"criteria": [{"id": "ok", "text": "add работает",
                             "method": "command",
                             "check": "%s check_ok.py" % sys.executable,
                             "severity": "LOW"}]}
        out = mech.run(bar["ledger_path"], wt, spec=spec, do_commit=False)
        table = out.get("criteria_evidence") or []
        if not table:
            fails.append("no per-criterion evidence was recorded")
        elif table[0].get("satisfied") is not True:
            fails.append("a passing check was not recorded as satisfied: %s"
                         % table[0])
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_manual_criterion_is_never_executed_as_evidence():
    """У manual нет команды, поэтому он не может попасть в таблицу
    доказательств - его судьба на потолке Слоя 1 (UNVERIFIABLE)."""
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        spec = {"criteria": [{"id": "m", "text": "выглядит правильно",
                             "method": "manual", "check": "%s -c pass"
                             % sys.executable, "severity": "LOW"}]}
        table = mech.execute_criteria(spec, wt)
        if any(r["id"] == "m" for r in table):
            fails.append("a manual criterion was executed as proof: %s" % table)
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_a_project_style_failure_output_is_mechanical_not_infra():
    """Найдено на сквозном прогоне: набор этого проекта печатает
    'FAIL  <группа>' и выходит с 1 - ни 'assert', ни 'traceback'. Эвристика
    искала именно эти слова и объявляла реальный красный тест сменой
    маршрута, то есть чинить то, что чинить нечем."""
    fails = []
    style = "FAIL  some group\n        - expected 3, got 4\n1/35 groups failed\n"
    for cmd in ([sys.executable, "functions/baseline/test_baseline.py"],
                [sys.executable, "-m", "unittest", "discover"],
                ["pytest", "-q"]):
        v = mech.classify_run({"runs": [{"cmd": cmd, "exit_code": 1,
                                         "timed_out": False,
                                         "output_tail": style, "log": "x"}]})
        if v["kind"] != mech.MECHANICAL_FAILURE:
            fails.append("%s -> %s, expected MECHANICAL_FAILURE"
                         % (cmd[-1], v["kind"]))
        if v["action"] != mech.REWORK_CODE:
            fails.append("action %s, expected REWORK_CODE" % v["action"])
    # А настоящая инфраструктура так и должна остаться инфраструктурой.
    for clean in ({"runs": [{"cmd": ["pytest"], "exit_code": 124,
                             "timed_out": True, "output_tail": style,
                             "log": "x"}]},
                  {"runs": [{"cmd": ["pytest"], "exit_code": 127,
                             "timed_out": False,
                             "output_tail": "'pytest' is not recognized",
                             "log": "x"}]}):
        v = mech.classify_run(clean)
        if v["kind"] != mech.INFRA_FAILURE:
            fails.append("a genuine infra signal became %s" % v["kind"])
    return fails


@test
def test_clean_run_copy_does_not_swallow_the_project():
    """Найдено на живом прогоне: песочница чистого прогона разрослась до
    19.6 ГБ и выбила диск (WinError 112), потому что копировался весь проект
    вместе с кэшем на 38 ГБ. Исключение тяжёлых каталогов обязано быть
    явным, а превышение бюджета - ошибкой, а не тихим урезанием."""
    fails = []
    for heavy in ("cache", "models", "venvs", "node_modules", ".venv"):
        if heavy not in mech.HEAVY_SKIP:
            fails.append("%r is not excluded from the clean-run copy" % heavy)
    root, proj = make_repo()
    try:
        big = os.path.join(root, "cache")
        os.makedirs(big, exist_ok=True)
        with open(os.path.join(big, "blob.bin"), "wb") as f:
            f.write(b"x" * 4096)
        info = mech._copy_tree(root, os.path.join(root, "_sandbox"))
        if os.path.exists(os.path.join(root, "_sandbox", "cache")):
            fails.append("the heavy dir was copied anyway")
        if info.get("copied", 0) <= 0:
            fails.append("nothing was copied at all: %s" % info)
        if "warning" in info and not info.get("over_budget"):
            fails.append("a warning was emitted without exceeding the budget")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_copy_budget_overrun_is_reported():
    """Превышение бюджета - это внятная ошибка. Молчаливый пропуск файлов
    означал бы, что прогон перестал что-то проверять, но выглядел зелёным."""
    fails = []
    root, proj = make_repo()
    old = mech.COPY_BUDGET_BYTES
    try:
        mech.COPY_BUDGET_BYTES = 1024          # deliberately tiny
        with open(os.path.join(root, "big1.py"), "w", encoding="utf-8") as f:
            f.write("x" + "y" * 4000)
        with open(os.path.join(root, "big2.py"), "w", encoding="utf-8") as f:
            f.write("x" + "y" * 4000)
        info = mech._copy_tree(root, os.path.join(root, "_s2"))
        if not info.get("over_budget"):
            fails.append("the budget overrun was not flagged: %s" % info)
        if not info.get("warning"):
            fails.append("no warning text for the overrun")
        if info.get("skipped", 0) <= 0:
            fails.append("skipped count is zero despite an overrun")
        return fails
    finally:
        mech.COPY_BUDGET_BYTES = old
        shutil.rmtree(root, ignore_errors=True)


@test
def test_red_test_is_mechanical_failure_not_infrastructure():
    fails = []
    clean = {"runs": [{"cmd": ["pytest"], "exit_code": 1, "timed_out": False,
                       "output_tail": "Traceback (most recent call last):\n"
                                      "  assert 1 == 2\nAssertionError",
                       "log": "x"}]}
    v = mech.classify_run(clean)
    if v["kind"] != mech.MECHANICAL_FAILURE:
        fails.append("a red assertion became %s" % v["kind"])
    if v["action"] != mech.REWORK_CODE:
        fails.append("action %s, expected REWORK_CODE" % v["action"])
    return fails


@test
def test_timeout_and_missing_tool_are_infrastructure():
    fails = []
    for clean in (
        {"runs": [{"cmd": ["x"], "exit_code": 124, "timed_out": True,
                   "output_tail": "TIMEOUT", "log": "x"}]},
        {"runs": [{"cmd": ["pytest"], "exit_code": 127, "timed_out": False,
                   "output_tail": "'pytest' is not recognized", "log": "x"}]},
        {"runs": [{"cmd": ["curl"], "exit_code": 1, "timed_out": False,
                   "output_tail": "503 Service Unavailable", "log": "x"}]},
    ):
        v = mech.classify_run(clean)
        if v["kind"] != mech.INFRA_FAILURE:
            fails.append("infrastructure signal became %s" % v["kind"])
        if v["action"] != mech.CHANGE_ROUTE:
            fails.append("infrastructure must change the route, got %s"
                         % v["action"])
    return fails


@test
def test_a_failing_run_is_not_misreported_as_infrastructure():
    """Слово 'connection' само по себе не должно переводить падение ассерта в
    инфраструктуру.

    Раньше этот тест требовал обратного - чтобы 'connection refused'
    игнорировался. Теперь он этого НЕ требует: 'connection refused', 'proxy',
    'ssl', 'certificate' и коды 5xx - сильные инфраструктурные признаки,
    ставить их выше текста вывода правильно, потому что раннер в таком случае
    действительно не смог отработать. Тест проверяет именно слабый случай:
    обычный ассерт, где слово 'connection' встречается как часть сообщения."""
    fails = []
    clean = {"runs": [{"cmd": [sys.executable, "test_thing.py"],
                       "exit_code": 1, "timed_out": False,
                       "output_tail": "E   assert app.connect(1) == 3\n"
                                      "E   assert 5 == 3\nAssertionError\n"
                                      "connection settings differ from the "
                                      "expected baseline",
                       "log": "x"}]}
    v = mech.classify_run(clean)
    if v["kind"] != mech.MECHANICAL_FAILURE:
        fails.append("an assertion was classified %s" % v["kind"])
    # Сильные маркеры, наоборот, обязаны оставаться инфраструктурой.
    for marker in ("connection refused", "proxy", "certificate error"):
        c2 = {"runs": [{"cmd": [sys.executable, "test_thing.py"],
                        "exit_code": 1, "timed_out": False,
                        "output_tail": "E   assert x == 1\n%s" % marker,
                        "log": "x"}]}
        if mech.classify_run(c2)["kind"] != mech.INFRA_FAILURE:
            fails.append("%r was not treated as infrastructure" % marker)
    return fails


# --------------------------------------------------------------------------
# правило 5: PASS -> настоящий коммит в теневом store
# --------------------------------------------------------------------------

@test
def test_pass_creates_a_retrievable_commit_in_the_shadow_store():
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        shutil.copy(os.path.join(proj, "test_app.py"), os.path.join(wt, "test_app.py"))
        out = mech.run(bar["ledger_path"], wt, do_commit=True)
        if not out.get("passed"):
            fails.append("a clean tree failed: %s" % out.get("reasons"))
            return fails
        vc = out.get("verified_commit") or {}
        if not vc.get("commit"):
            fails.append("PASS produced no commit: %s" % out.get("verified_commit_error"))
            return fails
        if not vc.get("store"):
            fails.append("the commit has no store path")
            return fails
        # Коммит обязан быть адресуемым ПОСЛЕ - это то, что читает рецензент.
        rc = subprocess.run(["git", "--git-dir", os.path.join(vc["store"], "git"),
                             "cat-file", "-t", vc["commit"]],
                            capture_output=True, text=True)
        if rc.returncode != 0 or "commit" not in (rc.stdout or ""):
            fails.append("the verified commit is not readable afterwards: %s"
                         % (rc.stderr or rc.stdout)[:120])
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# правило 8: вторая мутация не может проскочить мимо барьера
# --------------------------------------------------------------------------

@test
def test_second_attempt_cannot_skip_the_mechanical_barrier():
    fails = []
    root, proj = make_repo()
    try:
        gate = mech.AttemptGate(os.path.join(root, "attempts.json"))
        a1 = gate.open_attempt("task-1")
        gate.mark_verified()
        a2 = gate.open_attempt("task-1")
        if a1["attempt"] != 1 or a2["attempt"] != 2:
            fails.append("attempt numbering is wrong: %s %s"
                         % (a1["attempt"], a2["attempt"]))
        if a2.get("mechanical_required") is not True:
            fails.append("attempt 2 was allowed without re-running mechanics")
        if not a2.get("reason"):
            fails.append("no reason given for requiring the barrier again")
        # Другая задача обнуляет счётчик - но тоже требует барьера.
        a3 = gate.open_attempt("task-2")
        if a3["mechanical_required"] is not True:
            fails.append("a new task skipped the barrier")
        return fails
    except AssertionError:
        raise
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# сквозной путь
# --------------------------------------------------------------------------

@test
def test_tampered_baseline_fails_the_gate_end_to_end():
    """Сквозная проверка правила 1: подмена обязана остановить всё и
    сказать ESCALATE, а не «почини код»."""
    fails = []
    root, proj = make_repo()
    try:
        bar = barrier(proj, os.path.join(root, "hermes"))
        wt = bar["worktree"]
        target = os.path.join(wt, "test_app.py")
        shutil.copy(os.path.join(proj, "test_app.py"), target)
        with open(target, encoding="utf-8") as f:
            body = f.read()
        with open(target, "w", encoding="utf-8") as f:
            f.write(body.replace("== 3", "== 99"))
        out = mech.run(bar["ledger_path"], wt)
        if out.get("passed"):
            fails.append("the gate passed a tampered baseline")
        if out.get("kind") != mech.TEST_TAMPERING:
            fails.append("kind %s" % out.get("kind"))
        if out.get("action") != mech.ESCALATE:
            fails.append("action %s, expected ESCALATE" % out.get("action"))
        if out.get("status_after") == "verified":
            fails.append("status_after was verified despite tampering")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_cli_explain_prints_the_prescribed_action():
    fails = []
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mech.main(["explain", mech.INFRA_FAILURE])
    text = buf.getvalue()
    if rc != 0:
        fails.append("explain exited %s" % rc)
    if mech.CHANGE_ROUTE not in text:
        fails.append("explain did not print the action: %r" % text)
    return fails


@test
def test_broken_input_never_crashes():
    fails = []
    try:
        out = mech.check_tampering({}, "")
        if not isinstance(out, dict):
            fails.append("check_tampering did not return a dict")
    except Exception as exc:  # noqa: BLE001
        fails.append("check_tampering raised %s: %s" % (type(exc).__name__, exc))
    try:
        out = mech.classify_run({"runs": []})
        if out.get("kind") != "":
            fails.append("empty runs should classify as no failure: %s" % out)
    except Exception as exc:  # noqa: BLE001
        fails.append("classify_run raised %s: %s" % (type(exc).__name__, exc))
    try:
        if mech.action_for("NONSENSE") != mech.ESCALATE:
            fails.append("an unknown failure kind must default to ESCALATE")
    except Exception as exc:  # noqa: BLE001
        fails.append("action_for raised %s" % type(exc).__name__)
    try:
        json.dumps(mech.run("no-such-ledger.json", "", do_commit=False))
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        fails.append("run raised an unexpected %s: %s" % (type(exc).__name__, exc))
    return fails


@test
def test_report_bytes_are_utf8():
    """Репозиторий уже кусался ложным mojibake от cp866-консоли."""
    fails = []
    r = mech.run("no-such-ledger.json", "", do_commit=False)
    try:
        mech.render(r).encode("utf-8")
    except UnicodeEncodeError as exc:
        fails.append("render is not encodable as UTF-8: %s" % exc)
    return fails


def main() -> int:
    if bl.git(["--version"], cwd=DT if os.path.isdir(DT) else None)[0] != 0:
        print("git is required for these tests (real repos, no fakes)")
        return 2
    failed = 0
    for label, fn in TESTS:
        try:
            res = fn() or []
            fails = list(res[0]) if isinstance(res, tuple) else list(res)
            notes = list(res[1] or []) if isinstance(res, tuple) else []
        except AssertionError as exc:
            fails, notes = ["assertion: %s" % exc], []
        except Exception as exc:  # noqa: BLE001
            fails, notes = ["%s: %s" % (type(exc).__name__, exc)], []
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        elif notes:
            print("skip  %s (%s)" % (label, "; ".join(notes)))
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