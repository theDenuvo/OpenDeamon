"""Слой 4 - механическая проверка. Главный слой системы.

Почему он существует. Реальный провал стоил $1,234.50: ассерт приёмки был
переписан вместе с кодом, который он должен был проверять. Проверка прошла,
поведение изменилось, никто не заметил. Никакая LLM-просьба это не ловит -
чтобы понять, что ассерт неверен, нужна ИСХОДНАЯ спека, а её переписали
вместе с кодом. Значит проверка обязана быть механической, и модель в этом
слое не участвует.

Ядро этого модуля - четыре правила, которые нельзя ослаблять:

1. Сравнение хэша baseline-тестов с текущим. Разошёлся -> TEST_TAMPERING
   -> FAIL. Изменённый тест и УДАЛЁННЫЙ тест одинаково подмена: ослабить
   проверку можно и выбросив её. Новые тесты разрешены явно: запрет на новые
   тесты без критерия в прошлой версии ломал полезный TDD.

2. Каждый автоматический критерий подтверждается ФАКТО - запущенной командой
   и наблюдённым кодом возврата. Не заявлением воркера. Если утверждение
   воркера нельзя показать механически, критерий не проходит.

3. Потолок из Слоя 1 доезжает сюда: UNVERIFIABLE никогда не становится PASS
   и никогда не даёт verified.

4. Каждая НОВАЯ мутация снова проходит механику. После rework полный цикл
   обязателен: worker -> mechanical -> reviewer. Иначе получается
   worker -> verify -> reviewer -> FAIL -> worker -> reviewer, где вторая
   мутация не встретила барьера вовсе.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.dirname(_HERE)            # .../functions
# Модуль должен работать и как `python mechanical.py`, и как импорт из
# тестового набора. При запуске скриптом в sys.path попадает только каталог
# самого модуля, поэтому siblings приходится добавлять явно.
for _d in ("", "baseline", "specgate"):
    _p = os.path.join(_FUNCTIONS, _d) if _d else _HERE
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import baseline as bl  # noqa: E402
import spec_gate as sg  # noqa: E402

# --- типология фейлов и НАЗНАЧЕННОЕ действие ---------------------------
# Метка бесполезна без действия: именно действие отличает «починить код» от
# «сменить маршрут» и от «эскалировать». TEST_TAMPERING и WORKER_CRASH
# никогда не являются поводом дать воркеру ещё раз: повтор воспроизводит то
# же самое. INFRA_FAILURE выделен отдельно, чтобы сетевая проблема не съела
# цикл rework.
SPEC_FAILURE = "SPEC_FAILURE"
MECHANICAL_FAILURE = "MECHANICAL_FAILURE"
TEST_TAMPERING = "TEST_TAMPERING"
REVIEW_FAILURE = "REVIEW_FAILURE"
INFRA_FAILURE = "INFRA_FAILURE"
WORKER_CRASH = "WORKER_CRASH"

REWORK_CODE = "REWORK_CODE"
REWORK_SPEC = "REWORK_SPEC"
ESCALATE = "ESCALATE"
CHANGE_ROUTE = "CHANGE_ROUTE"
ROLLBACK = "ROLLBACK"

ACTION = {
    SPEC_FAILURE: REWORK_SPEC,
    MECHANICAL_FAILURE: REWORK_CODE,
    TEST_TAMPERING: ESCALATE,
    REVIEW_FAILURE: REWORK_CODE,
    INFRA_FAILURE: CHANGE_ROUTE,
    WORKER_CRASH: ROLLBACK,
}

# Метки, после которых повтор того же промта бессмыслен.
NO_RERUN = frozenset({TEST_TAMPERING, WORKER_CRASH})

RUN_TIMEOUT = 600
COPY_SKIP = frozenset({".git", ".mechanical", "__pycache__", ".worktrees",
                       "node_modules", ".pytest_cache"})
INFRA_MARKERS = (
    "timeout", "timed out", "no such file or directory", "not recognized",
    "is not installed", "command not found", "connection refused",
    "connection reset", "temporary failure in name resolution",
    "503 service unavailable", "502 bad gateway", "504 gateway",
    "tunnel", "proxy", "ssl", "certificate", "eof occurred",
)
INFRA_EXIT = frozenset({124, 126, 127})


def _copy_tree(src: str, dst: str) -> None:
    """Изолированная копия дерева для чистого прогона."""
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in COPY_SKIP]
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            shutil.copy2(os.path.join(dirpath, name),
                         os.path.join(target_dir, name))


def action_for(kind: str) -> str:
    """Назначенное действие для типа фейла."""
    return ACTION.get(kind, ESCALATE)


def rerun_allowed(kind: str) -> bool:
    """Можно ли дать воркеру ещё заход после этого типа фейла."""
    return kind not in NO_RERUN


# --- проверка подмены тестов ------------------------------------------


def check_tampering(ledger: dict, root: str) -> dict:
    """Хэш baseline-тестов против текущего состояния дерева.

    Возвращает вердикт слоя 4, а не «зелёное/красное»: конкретные имена
    файлов нужны и человеку, и эскалации."""
    report = bl.verify(ledger, root) if isinstance(ledger, dict) else \
        bl.verify({}, root)
    tampered = report.get("status") == bl.VERIFY_TAMPERED
    modified = list(report.get("modified") or [])
    deleted = list(report.get("deleted") or [])
    # Ключ в baseline.verify - `new_tests`; `added` принимается на всякий случай.
    added = list(report.get("new_tests") or report.get("added") or [])
    # Разница только в переводах строк - не подмена: файл не менялся по смыслу.
    eol_only = list(report.get("eol_only") or [])
    # Новые тесты - не подмена: их рост разрешён явно.
    kind = TEST_TAMPERING if tampered else ""
    out = {
        "verdict": kind,
        "passed": not tampered,
        "modified": modified,
        "deleted": deleted,
        "added": added,
        "reasons": list(report.get("reasons") or []),
        "new_tests_allowed": added,
        "eol_only": eol_only,
        "baseline_id": (ledger or {}).get("baseline_id", ""),
    }
    if tampered:
        out["action"] = ESCALATE
        out["rerun_allowed"] = False
    return out


# --- чистый прогон в отдельной папке ---------------------------------

def _commands(spec: dict | None, sandbox: str) -> list[list[str]]:
    """Команды чистого прогона.

    Дефолт - запуск КАЖДОГО найденного тестового файла как скрипта, а не
    `unittest discover`. Причина замеренная: discover собирает только
    подклассы unittest.TestCase, а наборы этого проекта - обычные функции с
    собственным раннером, поэтому discover честно рапортует "NO TESTS RAN" и
    выходит с кодом 5. Зелёного результата не существует, а барьер обязан
    что-то проверять по-настоящему.
    """
    cmds: list[list[str]] = []
    if spec:
        for crit in spec.get("criteria") or []:
            check = (crit.get("check") or "").strip()
            if crit.get("method") == "command" and check:
                cmds.append(check.split())
    if cmds:
        return cmds
    for dirpath, dirnames, filenames in os.walk(sandbox):
        dirnames[:] = [d for d in dirnames if d not in COPY_SKIP]
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), sandbox)
            if bl.is_test_file(rel.replace("\\", "/")):
                cmds.append([sys.executable, rel.replace("/", os.sep)])
    if not cmds:
        cmds = [[sys.executable, "-m", "unittest", "discover", "-q"]]
    return cmds


def run_clean(worktree: str, spec: dict | None = None,
              timeout: int = RUN_TIMEOUT) -> dict:
    """Прогнать проверки в ОТДЕЛЬНОМ каталоге, записать код возврата и лог.

    «Чистый прогон» не может означать прогон в грязном рабочем дереве: там
    результат смешан с тем, что воркер не успел доделать или уже откатил."""

    sandbox = os.path.join(worktree, ".mechanical", "cleanrun")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    # Копия дерева в изолированную папку. Без копии «чистый прогон» был бы
    # прогоном в пустом каталоге: unittest discover там находит ноль тестов и
    # выходит с кодом 5, то есть зелёного результата не существует вовсе.
    # Изолированная копия также снимает влияние грязного рабочего дерева на
    # результат - именно это требование к «чистому прогону».
    _copy_tree(worktree, sandbox)
    results = []
    for cmd in _commands(spec, sandbox):
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=sandbox, capture_output=True,
                                  text=True, timeout=timeout)
            code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            code = 124
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            out += "\nTIMEOUT after %ds: %s" % (timeout, " ".join(cmd))
            timed_out = True
        log_name = "run-%d.log" % len(results)
        try:
            with open(os.path.join(sandbox, log_name), "w",
                      encoding="utf-8") as fh:
                fh.write("$ %s\n\n%s" % (" ".join(cmd), out))
        except OSError:
            pass
        results.append({"cmd": cmd, "exit_code": code, "log": log_name,
                        "seconds": round(time.time() - started, 2),
                        "timed_out": timed_out,
                        "output_tail": out[-4000:]})
    return {
        "sandbox": sandbox,
        "separate_dir": True,
        "runs": results,
        "all_passed": all(r["exit_code"] == 0 for r in results),
    }


def classify_run(clean: dict) -> dict:
    """Отличить INFRA_FAILURE от MECHANICAL_FAILURE ПО ДОКАЗАТЕЛЬСТВАМ.

    Красный тест - это Mechanical, а не инфраструктура. Таймаут, отсутствующий
    инструмент или недоступный сервис - инфраструктура. Ошибка здесь стоит
    цикл rework: сетевая проблема не лечится правкой кода, и наоборот."""
    for r in clean.get("runs") or []:
        code = r.get("exit_code")
        if code == 0:
            continue
        tail = (r.get("output_tail") or "").lower()
        infra = bool(r.get("timed_out")) or code in INFRA_EXIT or \
            any(m in tail for m in INFRA_MARKERS)
        # Утверждение теста - это механический фейл, даже если текст содержит
        # слово «connection»: важна природа падения, а не слово.
        looks_like_assertion = ("assert" in tail or "traceback (most recent"
                                in tail) and not r.get("timed_out")
        kind = MECHANICAL_FAILURE if looks_like_assertion else INFRA_FAILURE
        if r.get("timed_out"):
            kind = INFRA_FAILURE
        return {"kind": kind, "action": action_for(kind),
                "evidence": "exit=%s cmd=%s" % (code, " ".join(r["cmd"])),
                "rerun_allowed": rerun_allowed(kind)}
    return {"kind": "", "action": "", "evidence": "all runs exited 0",
            "rerun_allowed": True}


# --- мутации и барьер -------------------------------------------------

class AttemptGate:
    """Счётчик попыток: вторая мутация не может проскочить мимо барьера.

    Каждая новая мутация обязана снова пройти механику. Без этого счётчика
    получается worker -> verify -> reviewer -> FAIL -> worker -> reviewer,
    где у второй мутации не было барьера."""

    SCHEMA = "opendeamon.attemptgate/1"

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"schema": self.SCHEMA, "attempts": 0,
                    "verified_at": [], "task_digest": ""}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def open_attempt(self, task_digest: str = "") -> dict:
        """Открыть попытку. mechanical_required=True означает: барьер обязан
        быть пройден заново, иначе попытка недопустима."""
        data = self._load()
        if task_digest and data.get("task_digest") and \
                task_digest != data["task_digest"]:
            data = {"schema": self.SCHEMA, "attempts": 0,
                    "verified_at": [], "task_digest": task_digest}
        data["task_digest"] = task_digest or data.get("task_digest", "")
        data["attempts"] = int(data.get("attempts", 0)) + 1
        # Счётчик обязан быть на диске: иначе следующий процесс откроет
        # попытку 1 и решит, что барьер ещё не проходился.
        self._save(data)
        return {"attempt": data["attempts"],
                "mechanical_required": True,
                "reason": "every new mutation must pass through mechanics "
                          "again; a rework without a barrier is the exact "
                          "path this gate exists to close"}

    def mark_verified(self) -> None:
        data = self._load()
        data.setdefault("verified_at", []).append(time.time())
        self._save(data)


def commit_verified(root: str, baseline: dict, message: str = "") -> dict:
    """PASS -> настоящий коммит в теневом store.

    Это та самая иммутабельная точка, с которой читает рецензент. Коммит
    должен быть настоящим и адресуемым после, иначе рецензент пришлось бы
    читать живое состояние - а это ровно TOCTOU."""
    text = message or ("verified by mechanical layer for baseline %s"
                       % (baseline or {}).get("baseline_id", "?"))
    return bl.snapshot(root, text)


# --- итоговый вердикт -------------------------------------------------

def run(ledger_path: str, worktree: str, spec: dict | None = None,
        baseline: dict | None = None, do_commit: bool = True) -> dict:
    """Полный прогон механики: подмена -> чистый прогон -> вердикт."""
    led, err = bl.load_ledger(ledger_path)
    if not led:
        led = baseline or {}
    root = worktree or led.get("root", "")

    tamper = check_tampering(led, worktree or root)
    result: dict[str, Any] = {
        "schema": "opendeamon.mechanical/1",
        "baseline_id": led.get("baseline_id", ""),
        "worktree": worktree,
        "tampering": tamper,
    }

    if not tamper["passed"]:
        # Подмена - это ESCALATE, а не повод для rework.
        result.update({"passed": False, "kind": TEST_TAMPERING,
                       "action": ESCALATE, "rerun_allowed": False,
                       "status_after": "judged",
                       "reasons": ["baseline test hash diverged: modified=%s "
                                   "deleted=%s" % (tamper["modified"],
                                                   tamper["deleted"])]})
        return result

    clean = run_clean(worktree)
    result["clean_run"] = clean
    if not clean["all_passed"]:
        verdict = classify_run(clean)
        result.update({"passed": False, "kind": verdict["kind"],
                       "action": verdict["action"],
                       "rerun_allowed": verdict["rerun_allowed"],
                       "status_after": "judged",
                       "reasons": [verdict["evidence"]]})
        return result

    # Потолок Слоя 1: UNVERIFIABLE не даёт verified.
    unverifiable = 0
    if spec:
        spec_result = sg.run(spec, use_llm=False)
        unverifiable = int((spec_result.get("counts") or {})
                           .get("unverifiable", 0))
        result["spec"] = {"decision": spec_result.get("decision"),
                          "counts": spec_result.get("counts"),
                          "status_after": spec_result.get("status_after")}

    verified_commit = {}
    if do_commit and root:
        try:
            verified_commit = commit_verified(root, led)
            result["verified_commit"] = {
                "commit": verified_commit.get("commit", ""),
                "ref": verified_commit.get("ref", ""),
                "store": verified_commit.get("store", ""),
            }
        except (RuntimeError, OSError) as exc:
            result["verified_commit_error"] = str(exc)[:200]

    result.update({
        "passed": True,
        "kind": "",
        "action": "",
        "rerun_allowed": True,
        "unverifiable": unverifiable,
        # verified только если механика прошла И не осталось UNVERIFIABLE.
        "status_after": "verified" if unverifiable == 0 else "judged",
        "reasons": ([] if unverifiable == 0
                    else ["%d criterion(s) stay UNVERIFIABLE; a manual "
                          "criterion never yields verified" % unverifiable]),
    })
    return result


def render(result: dict) -> str:
    lines = ["MECHANICAL %s" % ("PASS" if result.get("passed") else "FAIL"),
             "baseline: %s" % result.get("baseline_id", ""),
             "worktree: %s" % result.get("worktree", "")]
    t = result.get("tampering") or {}
    if t:
        lines.append("tampering: passed=%s modified=%s deleted=%s added=%s"
                     % (t.get("passed"), t.get("modified"),
                        t.get("deleted"), t.get("added")))
    cr = result.get("clean_run") or {}
    for r in cr.get("runs") or []:
        lines.append("  run exit=%s log=%s cmd=%s"
                     % (r.get("exit_code"), r.get("log"), " ".join(r.get("cmd", []))))
    vc = result.get("verified_commit")
    if vc:
        lines.append("verified commit: %s (%s)"
                     % (vc.get("commit", "")[:12], vc.get("ref", "")))
    if not result.get("passed"):
        lines.append("kind:   %s" % result.get("kind", ""))
        lines.append("ACTION: %s" % result.get("action", ""))
        lines.append("rerun allowed: %s" % result.get("rerun_allowed", ""))
    lines.append("status_after: %s" % result.get("status_after", ""))
    for r in result.get("reasons") or []:
        lines.append("  - %s" % r)
    return "\n".join(lines)


def explain(kind: str) -> str:
    return "%s -> %s (rerun allowed: %s)" % (
        kind, action_for(kind), rerun_allowed(kind))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mechanical verification gate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("ledger")
    p_run.add_argument("worktree")
    p_run.add_argument("--spec")
    p_run.add_argument("--no-commit", action="store_true")

    p_ex = sub.add_parser("explain")
    p_ex.add_argument("failure_kind")

    args = ap.parse_args(argv)
    if args.cmd == "explain":
        print(explain(args.failure_kind))
        return 0

    spec = None
    if args.spec:
        try:
            with open(args.spec, encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, ValueError) as exc:
            print("cannot read spec: %s" % exc)
            return 2
    result = run(args.ledger, args.worktree, spec=spec,
                 do_commit=not args.no_commit)
    print(render(result))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())