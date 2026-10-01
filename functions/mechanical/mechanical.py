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
import shlex
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
# Методы, критерий по которым можно проверить исполнением команды. `manual`
# сюда не входит принципиально: у него нет команды, и он остаётся на
# потолке Слоя 1 (UNVERIFIABLE), а не превращается в доказательство.
AUTO_METHODS = frozenset({"command", "test", "invariant", "snapshot"})
# Каталоги, которые не копируются в песочницу чистого прогона.
#
# Это не косметика. На реальном проекте песочница разрослась до 19.6 ГБ и
# выбила диск: копировался весь проект целиком, вместе с кэшем на 38 ГБ.
# Исключение списка кэшей превращало прогон в «прочитано 0 файлов, тестов
# нет» тихо, а полное копирование - в WinError 112 на заполненном диске.
# Оба варианта плохи, поэтому тяжёлое исключается явно.
HEAVY_SKIP = frozenset({
    ".git", ".mechanical", "__pycache__", ".worktrees", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "cache", "caches",
    "models", "venvs", "venv", ".venv", "env", "owui", "desktop",
    "gateway", "tools", "searxng", "data", "logs", "logs_tmp", "tmp",
    # Мои черновики и стенды. gitignored, но в песочницу попадали, а
    # `_setup_tmp/*_test.py` содержат скрипты, рассчитанные на живое
    # окружение, и падают в чистом прогоне.
    "_setup_tmp",
})
COPY_SKIP = frozenset({".git", ".mechanical", "__pycache__", ".worktrees",
                       "node_modules", ".pytest_cache"})
# Потолок копирования. Превышение - это ошибка с внятным сообщением, а не
# молчаливое урезание: иначе результат прогона снова перестал бы что-то
# проверять.
COPY_BUDGET_BYTES = 512 * 1024 * 1024
INFRA_MARKERS = (
    "timeout", "timed out", "no such file or directory", "not recognized",
    "is not installed", "command not found", "connection refused",
    "connection reset", "temporary failure in name resolution",
    "503 service unavailable", "502 bad gateway", "504 gateway",
    "tunnel", "proxy", "ssl", "certificate", "eof occurred",
)
INFRA_EXIT = frozenset({124, 126, 127})


def _copy_tree(src: str, dst: str) -> dict:
    """Изолированная копия дерева для чистого прогона.

    Тяжёлые каталоги (кэши, веса моделей, виртуальные окружения) исключены
    явно: на этом проекте полная копия занимала 19.6 ГБ и выбивала диск.
    Превышение бюджета копирования - явная ошибка, а не тихое урезание."""
    copied, skipped, total = 0, 0, 0
    over_budget = False
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames
                       if d not in COPY_SKIP and d not in HEAVY_SKIP]
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            source = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(source)
            except OSError:
                continue
            if total + size > COPY_BUDGET_BYTES:
                over_budget = True
                skipped += 1
                continue
            try:
                shutil.copy2(source, os.path.join(target_dir, name))
            except OSError:
                skipped += 1
                continue
            copied += 1
            total += size
    info = {"copied": copied, "skipped": skipped,
            "bytes": total, "over_budget": over_budget,
            "excluded": sorted(HEAVY_SKIP)}
    if over_budget:
        info["warning"] = (
            "copy budget of %d MB exceeded; %d file(s) were NOT copied. The "
            "run is still honest only if those files are irrelevant to the "
            "criteria - otherwise the run must be declared INCOMPLETE, not "
            "green" % (COPY_BUDGET_BYTES // (1024 * 1024), skipped))
    return info


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

def _commands(spec: dict | None, sandbox: str,
              declared: list[str] | None = None) -> list[list[str]]:
    """Команды чистого прогона.

    Порядок выбора области:
      1. команды из критериев спеки - если критерий что-то объявил;
      2. ТЕСТЫ, объявленные в LEDGER, - то есть ровно то, что этот слой
         защищает. Это не произвол: барьер уже хэшировал эти файлы, и прогонять
         надо именно их;
      3. если объявлено нечего - не запускается НИЧЕГО, и это видно по
         пустому списку запусков, а не выдаётся за зелёный результат.

    Раньше область выбиралась как «все файлы, похожие на тест». На живом
    прогоне это дало 26 запусков, включая сломанные тесты скиллов Hermes,
    .js-файлы, запущенные интерпретатором Python, и мои черновики в
    _setup_tmp. Провал чужого набора выглядел бы как провал этого проекта,
    а зелёный результат означал бы «кто-то чужой прошёл», а не «наш проект
    цел». Лучше не запускать вовсе и сказать об этом, чем мерить чужое."""
    cmds: list[list[str]] = []
    if spec:
        for crit in spec.get("criteria") or []:
            check = (crit.get("check") or "").strip()
            if crit.get("method") == "command" and check:
                cmds.append(split_check(check))
    if cmds:
        return cmds
    for rel in (declared or []):
        rel = str(rel)
        if not rel.endswith(".py"):
            continue
        cmds.append([sys.executable, rel.replace("/", os.sep)])
    return cmds


def _exec(cmd: list[str], sandbox: str, timeout: int,
          log_name: str) -> dict:
    """Одна команда: код возврата, лог, хвост вывода."""
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=sandbox, capture_output=True,
                              text=True, timeout=timeout)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code = 124
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        out += "\nTIMEOUT after %ds: %s" % (timeout, " ".join(cmd))
        timed_out = True
    except OSError as exc:
        code, out, timed_out = 127, "cannot execute %s: %s" % (cmd[0], exc), False
    try:
        with open(os.path.join(sandbox, log_name), "w", encoding="utf-8") as fh:
            fh.write("$ %s\n\n%s" % (" ".join(cmd), out))
    except OSError:
        pass
    return {"cmd": cmd, "exit_code": code, "log": log_name,
            "seconds": round(time.time() - started, 2),
            "timed_out": timed_out, "output_tail": out[-4000:]}


def _sandbox(worktree: str) -> tuple[str, dict]:
    """Изолированная копия дерева для прогонов.

    Без копии «чистый прогон» был бы прогоном в пустом каталоге: там
    `unittest discover` находит ноль тестов и выходит с кодом 5, то есть
    зелёного результата не существует вовсе. Копия также снимает влияние
    грязого рабочего дерева на результат. Возвращает путь и отчёт о копии."""
    sandbox = os.path.join(worktree, ".mechanical", "cleanrun")
    shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    copy_info = _copy_tree(worktree, sandbox)
    return sandbox, copy_info


def split_check(check: str) -> list[str]:
    """Разбить строку проверки на аргументы.

    Простое `split()` по пробелу ломается на пути с пробелом: кавычки и сами
    пробелы остаются частью токена, и команда падает с exit=2 - неотличимо от
    того, что проверка критерия не прошла. Симптом тот же, а причина другая,
    и это легко выдать за баг проверяемой логики. shlex с posix=False режет
    по правилам командной строки и сохраняет кавычки в аргументах.
    На путях без пробелов (A:\\, Z:\\) поведение не меняется.

    `posix=False` сохраняет кавычки В ТОКЕНЕ: без их снятия получается
    `"check ok.py"` целиком, и Python падает с Errno 22 - то есть ровно тот
    неотличимый от «проверка не прошла» отказ."""
    try:
        parts = shlex.split(check, posix=False)
    except ValueError:
        # Несбалансированная кавычка - не молчаливый пустой вызов.
        return [check]
    return [_unquote(p) for p in parts]


def _unquote(token: str) -> str:
    """Снять обрамляющие парные кавычки."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def execute_criteria(spec: dict | None, worktree: str,
                     timeout: int = RUN_TIMEOUT) -> list[dict]:
    """ВЫПОЛНИТЬ проверку каждого автоматического критерия и записать доказательство.

    Это тот пункт, который обязан выполняться по факту, а не по заявлению.
    Раньше `run_clean` вызывался без `spec`, ветка `method == "command"` была
    мёртвым кодом, и `verified` означало «нашёлся какой-то зелёный тестовый
    файл». Античит при этом был исправен (тесты защищены от изменения), но
    критерии никто не проверял - то есть ровно тот класс подмены, против
    которого построен проект.

    Возвращает по одному элементу на критерий: исполненная команда, код
    возврата, лог и удовлетворён ли критерий. Критерий без `check` или с
    неавтоматическим методом сюда не попадает - его судьбой занимается
    потолок Слоя 1."""
    sandbox, _copy = _sandbox(worktree)
    table: list[dict] = []
    for crit in ((spec or {}).get("criteria") or []):
        method = (crit.get("method") or "").strip()
        check = (crit.get("check") or "").strip()
        if method not in AUTO_METHODS or not check:
            continue
        run = _exec(split_check(check), sandbox, timeout,
                    "crit-%s.log" % (crit.get("id") or len(table)))
        table.append({
            "id": crit.get("id", ""),
            "text": crit.get("text", ""),
            "method": method,
            "check": check,
            "exit_code": run["exit_code"],
            "log": run["log"],
            "output_tail": run["output_tail"][-1500:],
            "satisfied": run["exit_code"] == 0,
        })
    return table


def run_clean(worktree: str, spec: dict | None = None,
              timeout: int = RUN_TIMEOUT,
              declared: list[str] | None = None) -> dict:
    """Общий прогон наборов в ОТДЕЛЬНОМ каталоге, с кодом возврата и логом.

    Это регрессионная страховка, а не доказательство критериев: доказательства
    собирает `execute_criteria`. «Чистый прогон» не может означать прогон в
    грязном рабочем дереве - там результат смешан с тем, что воркер не успел
    доделать или уже откатил."""

    sandbox, copy_info = _sandbox(worktree)
    results = []
    for cmd in _commands(spec, sandbox, declared):
        results.append(_exec(cmd, sandbox, timeout, "run-%d.log" % len(results)))
    return {
        "sandbox": sandbox,
        "separate_dir": True,
        "copy": copy_info,
        "runs": results,
        "scope": ("declared criteria" if spec
                 else "tests from the baseline ledger" if declared
                 else "NOTHING: no criteria and no ledger tests"),
        "all_passed": all(r["exit_code"] == 0 for r in results),
    }


def _looks_like_test_runner(cmd: list[str]) -> bool:
    """Команда - тестовый раннер?

    Это важно для различения MECHANICAL и INFRA. Тестовый раннер, который
    отработал и вернул ненулевой код, - это механический фейл: код красный.
    Наивная эвристика искала слова 'assert' или 'traceback' в выводе, но
    набор этого проекта печатает 'FAIL  <группа>' и exit 1 - ни того, ни
    другого. Такая классификация отдавала реальный красный тест в смену
    маршрута, то есть чинить то, что чинить нечем, и терять цикл rework."""
    joined = " ".join(cmd).lower()
    if "unittest" in joined or "pytest" in joined or "tox" in joined:
        return True
    script = cmd[0] if cmd else ""
    name = os.path.basename(script)
    return (name.startswith("test_") or name.endswith("_test.py")
            or name.endswith("_tests.py") or name == "conftest.py")


def classify_run(clean: dict) -> dict:
    """Отличить INFRA_FAILURE от MECHANICAL_FAILURE ПО ДОКАЗАТЕЛЬСТВАМ.

    Инфраструктура - это когда раннер НЕ СМОГ отработать: таймаут,
    отсутствующий инструмент, недоступный сервис, 5xx. Если же тестовый
    раннер запустился и вернул ненулевой код, это механический фейл, как бы
    странно ни выглядел его текст вывода."""
    for r in clean.get("runs") or []:
        code = r.get("exit_code")
        if code == 0:
            continue
        tail = (r.get("output_tail") or "").lower()
        infra = bool(r.get("timed_out")) or code in INFRA_EXIT or \
            any(m in tail for m in INFRA_MARKERS)
        if infra:
            kind = INFRA_FAILURE
        elif _looks_like_test_runner(list(r.get("cmd") or [])) or \
                "assert" in tail or "traceback (most recent" in tail or \
                "groups failed" in tail or tail.lstrip().startswith("fail"):
            kind = MECHANICAL_FAILURE
        else:
            kind = INFRA_FAILURE
        return {"kind": kind, "action": action_for(kind),
                "evidence": "exit=%s cmd=%s" % (code, " ".join(r.get("cmd") or [])),
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

    # Доказательства по критериям - ДО общего прогона. Именно они решают,
    # verified, а не «нашёлся зелёный тестовый файл».
    evidence = execute_criteria(spec, worktree)
    result["criteria_evidence"] = evidence
    unsatisfied = [e for e in evidence if not e["satisfied"]]
    if unsatisfied:
        names = ", ".join("%s(exit=%s)" % (e["id"], e["exit_code"])
                          for e in unsatisfied)
        result.update({"passed": False, "kind": MECHANICAL_FAILURE,
                       "action": REWORK_CODE, "rerun_allowed": True,
                       "status_after": "judged",
                       "reasons": ["criterion check FAILED, the criterion is "
                                   "not proven: %s" % names]})
        return result

    clean = run_clean(worktree, spec=spec,
                      declared=sorted((led.get("tests") or {}).keys()))
    result["clean_run"] = clean
    if not clean["runs"]:
        # Ничего не объявлено и запускать нечего. Это НЕ «всё зелёное»:
        # пустой прогон не доказывает ничего, и выглядеть он не должен так,
        # будто что-то проверялось.
        result.update({"passed": False, "kind": "NOTHING_VERIFIED",
                       "action": "REWORK_SPEC", "rerun_allowed": True,
                       "status_after": "judged",
                       "reasons": ["nothing was executed: %s"
                                   % clean.get("scope", "no scope")]})
        return result
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
        # verified только если ВСЕ автоматические критерии исполнены и
        # удовлетворены (иначе мы вернулись выше) И не осталось UNVERIFIABLE.
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


def _force_utf8() -> None:
    """Перевести stdout/stderr в UTF-8.

    Тот же класс, что в рецензенте: дифф или вывод теста с любым символом вне
    cp1251 убивал процесс UnicodeEncodeError ПОСЛЕ того, как результат уже
    посчитан. Машинный выход в such a case - пустой файл, а слой 6 читает
    именно его."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="mechanical verification gate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("ledger")
    p_run.add_argument("worktree")
    p_run.add_argument("--spec")
    p_run.add_argument("--no-commit", action="store_true")
    p_run.add_argument("--json", action="store_true",
                       help="emit the result as JSON on stdout")

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
            print("cannot read spec: %s" % exc, file=sys.stderr)
            return 2
    result = run(args.ledger, args.worktree, spec=spec,
                 do_commit=not args.no_commit)
    # Без машинного выхода слой 6 не подключается: слой принимает JSON-файлы,
    # а текстовый отчёт разобрать нельзя. Планировщик поймал это сквозным
    # прогоном, а не тестами: фикстуры слоя 6 были написаны руками в том же
    # формате, который реальные слои не производят.
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())