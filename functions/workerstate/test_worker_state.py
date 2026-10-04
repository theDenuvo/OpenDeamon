"""worker state machine tests — invariants of layer 3.

SCHEME.md §5.3 (`RECOVERY_BLOCKED` terminal), §6 (states, failure typing),
§8 invariants 3, 4 and 7; TODO.md Фаза 2-ter слой 3 and its DoD:

  * crash -> rollback impossible gives RECOVERY_BLOCKED          (DoD)
  * the quarantine marker survives a process restart           (DoD, inv. 4)

Stdlib only, no network, no LLM. Every group is a regression against a
specific way this state machine could rot into decoration:

  * the worker declaring its own failure class   -> FAILED_CLEAN lies
  * a state machine that only draws a diagram    -> nothing is enforced
  * quarantine in process memory                 -> restart makes a dirty tree usable
  * RECOVERY_BLOCKED with an exit                -> infinite dirty retries
  * WORKER_CRASH retried automatically           -> the crash reproduces
  * rollback declared without a re-measure       -> an unverified restore
  * rollback "restoring" ignored artefacts       -> it deletes what it did not create
  * paid model reachable through the ladder      -> the $0 law with one flag away
  * one attempt declaring a model dead           -> a 75-81s hang is a flake
  * the state machine handing out a verdict      -> layer 4's right, stolen here

Real git repositories are created in temp dirs, and the rollback path is
exercised against a real shadow store. A state machine tested only
against a fake tree is a state machine tested against nothing.

Git is required; if it is missing the suite says so instead of passing
vacuously.

Run: py A:/OpenDeamon/functions/workerstate/test_worker_state.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE / "worker_state.py"

_spec = importlib.util.spec_from_file_location("worker_state", MODULE)
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)
bl = ws.bl

PY = sys.executable


def git_ok() -> bool:
    return shutil.which("git") is not None and bl.git(["--version"])[0] == 0


# --------------------------------------------------------------------------
# fixtures: a real repo + a real layer-2 barrier
# --------------------------------------------------------------------------

DEFAULT_FILES = {
    "price.py": "def fmt(n):\n    return str(n)\n",
    "test_price.py": "def test_format():\n    assert True\n",
    "test_cart.py": "def test_total():\n    assert 1 + 1 == 2\n",
    "README.md": "# fixture\n",
}


def write_repo(root: str, files: dict) -> None:
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def make_repo(files: dict | None = None) -> str:
    root = tempfile.mkdtemp(prefix="workerstate-repo-")
    bl.git(["init", "--quiet", "-b", "main", root])
    bl.git(["config", "user.email", "t@opendeamon.local"], cwd=root)
    bl.git(["config", "user.name", "worker state tests"], cwd=root)
    bl.git(["config", "commit.gpgsign", "false"], cwd=root)
    # `core.autocrlf=true` - В САМОМ репозитории, не в глобальном конфиге.
    #
    # Это обычный способ настроить политику переводов строк, и именно он
    # делает расхождение видимым: `git worktree add` смотрит настройку
    # этого репозитория и кладёт в worktree CRLF, а теневой store - это
    # ДРУГОЙ репозиторий, и её он не видит вовсе. Именно это расхождение
    # и ломало откат: `checkout-index` писал байты блоба (LF) поверх
    # CRLF-дерева, и после отката `git status` в откатанном worktree
    # показывал «M» на каждом файле. Фикстура воспроизводит условие честно,
    # а починка живёт в `worker_state.restore`.
    bl.git(["config", "core.autocrlf", "true"], cwd=root)
    write_repo(root, files if files is not None else DEFAULT_FILES)
    bl.git(["add", "-A"], cwd=root)
    bl.git(["commit", "--quiet", "-m", "initial"], cwd=root)
    return root


class Fixture:
    """Барьер слоя 2 поверх настоящего репозитория.

    `Env` держит HERMES_HOME в отдельном временном каталоге: приватное
    хранилище (LEDGER, store, состояние слоя 3) не должно делиться
    между группами, иначе карантин одной группы заблокирует другую."""

    # ------------------------------------------------------------------ eol
    def _isolate_git_config(self):
        """Отрезать тест от глобальной настройки git на хосте.

        Конфиг пишется ПУСТЫМ по смыслу: он не задаёт `core.autocrlf` и не
        задаёт ничего вообще. Задача этого файла - только изоляция, чтобы
        машина с autocrlf=true (или с чем угодно ещё) не изменила результат
        теста.

        Почему autocrlf задаётся НЕ здесь, а в самом репозитории фикстуры
        (`make_repo`): расхождение "worktree CRLF против baseline LF" создают
        ДВА разных репозитория. `git worktree add` смотрит настройку
        основного, а `checkout-index` во время отката работает с `GIT_DIR`
        теневого store и берёт настройку уже оттуда.

        Если бы autocrlf лежал в глобальном конфиге, его увидели бы оба, и
        расхождение настроек - ровно то, из-за которого откат оставляет
        дерево грязным для git, - просто не воспроизвелось бы. Настройка в
        репозитории означает, что store её не видит, как в жизни.
        """
        cfg = tempfile.mkdtemp(prefix="workerstate-gitconfig-")
        path = os.path.join(cfg, "gitconfig")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# deliberately empty: isolation only, no settings\n")
        saved = {k: os.environ.get(k) for k in
                 ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = path
        # Системный конфиг тоже выключаем: иначе `include.path` оттуда
        # мог бы вернуть чужие настройки поверх наших.
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        return cfg, saved

    @staticmethod
    def _restore_eol_env(cfg, saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cfg, ignore_errors=True)

    def __init__(self, files: dict | None = None, isolation: bool = True):
        self.home = tempfile.mkdtemp(prefix="workerstate-home-")
        self.old_home = os.environ.get("HERMES_HOME")
        self.old_private = os.environ.get("HERMES_BASELINE_PRIVATE_DIR")
        os.environ["HERMES_HOME"] = self.home
        # Приватный корень уводим в temp: `private_root()` по умолчанию
        # создаёт `A:\OpenDeamon.private` рядом с настоящим репозиторием,
        # и тест не должен писать в проект.
        private = tempfile.mkdtemp(prefix="workerstate-private-")
        os.environ["HERMES_BASELINE_PRIVATE_DIR"] = private
        self.private = private
        self._eol_env = self._isolate_git_config()
        self.root = make_repo(files)
        self.barrier = bl.create_barrier(self.root, spec_id="S-L3",
                                         isolation=isolation)
        self.state = None
        if self.barrier.get("worker_may_start"):
            self.state = ws.new_state(self.barrier)

    @property
    def worktree(self) -> str:
        """Изолированное дерево воркера.

        Отдельный метод, а не `self.root`, потому что разница между
        ними и есть предмет слоя: воркер пишет в worktree, и запись в
        корневой проект — это уже другая история (вне контура)."""
        return self.barrier.get("worktree") or self.root

    def write(self, files: dict) -> None:
        """Правка в дереве ВОРКЕРА — то, что делает живой воркер."""
        write_repo(self.worktree, files)

    def write_root(self, files: dict) -> None:
        """Правка в корневом проекте, мимо worktree. Отдельный метод,
        чтобы «мимо контура» было написано явно, а не получилось
        случайно."""
        write_repo(self.root, files)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False

    def cleanup(self) -> None:
        bl.git(["worktree", "prune"], cwd=self.root)
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.private, ignore_errors=True)
        if self.old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old_home
        if self.old_private is None:
            os.environ.pop("HERMES_BASELINE_PRIVATE_DIR", None)
        else:
            os.environ["HERMES_BASELINE_PRIVATE_DIR"] = self.old_private
        # Изолированный gitconfig снимается ПОСЛЕ prune: prune сам вызывает
        # git и должен видеть ту же среду, что и остальная группа.
        cfg, saved = self._eol_env
        self._restore_eol_env(cfg, saved)


def _command_of(args) -> list:
    """argv без ведущих `-c key=value`, то есть сама команда git.

    `restore()` передаёт политику переводов строк через `-c core.autocrlf=...`
    (см. `_eol_config_args` в worker_state), и подмена git должна бить по
    существу команды, а не по её позиции в списке аргументов. Без этого
    подмена перестала бы перехватывать `checkout-index` и группа молча
    проверяла бы ничего.
    """
    out = list(args or [])
    while out[:1] == ["-c"]:
        out = out[2:]
    return out


def cli(fixture, args: list) -> subprocess.CompletedProcess:
    """CLI в отдельном процессе.

    `HERMES_BASELINE_PRIVATE_DIR` обязан совпадать с тем, что задал
    фикстурой, иначе новый процесс просто не найдёт ни состояния, ни
    маркера карантина — и проверка «пережил ли карантин перезапуск»
    прошла бы, ни разу не проверив ничего."""
    env = dict(os.environ)
    env["HERMES_HOME"] = fixture.home
    env["HERMES_BASELINE_PRIVATE_DIR"] = fixture.private
    return subprocess.run([PY, str(MODULE), "--json"] + args,
                          cwd=fixture.root, env=env, capture_output=True,
                          timeout=300)


# --------------------------------------------------------------------------
# the transition table is the contract
# --------------------------------------------------------------------------

def test_transition_table_matches_the_scheme():
    """Диаграмма из SCHEME.md §6, дословно. Расхождение здесь — не
    стилистика: это контракт, по которому принимает решения слой 6."""
    fails = []
    want = {
        ws.NOT_STARTED: (ws.RUNNING,),
        ws.RUNNING: (ws.COMPLETED, ws.FAILED_CLEAN, ws.FAILED_DIRTY),
        ws.FAILED_DIRTY: (ws.ROLLING_BACK,),
        # Self-loop: откат мог быть прерван, и продолжить его обязана
        # быть можно — иначе прерванный откат неотличим от тупика.
        ws.ROLLING_BACK: (ws.ROLLING_BACK, ws.BASELINE_RESTORED,
                           ws.RECOVERY_BLOCKED),
        ws.BASELINE_RESTORED: (ws.RUNNING,),
        ws.COMPLETED: (),
        ws.RECOVERY_BLOCKED: (),
    }
    for state, targets in want.items():
        got = tuple(ws.TRANSITIONS.get(state, ()))
        if got != targets:
            fails.append("%s: table says %s, scheme says %s"
                         % (state, list(got), list(targets)))
    if ws.FAILED_CLEAN not in ws.STARTABLE:
        fails.append("FAILED_CLEAN must be startable: откат не нужен")
    if ws.RECOVERY_BLOCKED in ws.STARTABLE:
        fails.append("RECOVERY_BLOCKED must not be startable")
    for state in ws.STATES:
        if state not in ws.TRANSITIONS:
            fails.append("state %s has no entry in the table" % state)
    return fails


def test_recovery_blocked_has_no_exit():
    """§5.3: из карантина нет ни одного перехода. Иначе защита
    превращается в бесконечный источник грязных повторов."""
    fails = []
    state = ws.new_state({"baseline_id": "abc123def4560000", "root": "r",
                          "checkpoint": {"commit": "c"}})
    state["state"] = ws.RECOVERY_BLOCKED
    for target in ws.STATES:
        if target == ws.RECOVERY_BLOCKED:
            continue
        try:
            ws.transition(state, target)
            fails.append("RECOVERY_BLOCKED -> %s was allowed" % target)
        except ws.IllegalTransition:
            pass
    if state["state"] != ws.RECOVERY_BLOCKED:
        fails.append("state moved to %s after refused transitions"
                     % state["state"])
    if ws.terminal_blocked(state) is not True:
        fails.append("terminal_blocked() does not report the quarantine")
    return fails


def test_illegal_transition_raises_instead_of_writing():
    """Молчаливый отказ здесь означал бы, что состояние разъехалось с
    реальностью, а читатель увидит правдоподобный отчёт."""
    fails = []
    state = ws.new_state({"baseline_id": "abc123def4560000"})
    try:
        ws.transition(state, ws.COMPLETED)
        fails.append("NOT_STARTED -> COMPLETED was accepted")
    except ws.IllegalTransition as exc:
        if "terminal" not in str(exc) and "not a transition" not in str(exc):
            fails.append("unhelpful error: %s" % exc)
    state["state"] = ws.COMPLETED
    try:
        ws.transition(state, ws.RUNNING)
        fails.append("COMPLETED -> RUNNING was accepted: a finished task "
                     "must not be silently restarted")
    except ws.IllegalTransition:
        pass
    return fails


# --------------------------------------------------------------------------
# CLEAN / DIRTY is measured, not declared
# --------------------------------------------------------------------------

def test_the_worker_cannot_declare_its_own_failure_class():
    """`FAILED_CLEAN` означает «упал до правок». Если это заявление, а
    не измерение, откат не случается никогда — а именно ради него
    слой написан. Воркер говорит «упал», код решает, чисто ли."""
    fails = []
    with Fixture() as fx:
        state = fx.state
        began = ws.start(fx.barrier, state)
        if not began["worker_may_start"]:
            return ["start refused on a clean barrier: %s" % began["reason"]]

        # Воркер «упал», но перед уходом поправил два файла и создал третий.
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n",
                  "test_price.py": "def test_format():\n    assert True\n"})
        with open(os.path.join(fx.worktree, "extra.py"), "w",
                  encoding="utf-8") as f:
            f.write("x = 1\n")

        # Вызывающий настаивает, что падение было «чистым».
        ended = ws.finish(began["state"], ok=False,
                          detail={"declared": ws.FAILED_CLEAN})
        if ended["state"]["state"] != ws.FAILED_DIRTY:
            fails.append("a dirty tree was classified %s; the caller's "
                         "declaration was taken at face value"
                         % ended["state"]["state"])
        if not ended["rollback_needed"]:
            fails.append("rollback_needed is False on a dirty tree")
        res = ended["residue"]
        if "price.py" not in (res.get("modified") or []):
            fails.append("modified price.py not reported: %s" % res)
        if "extra.py" not in (res.get("extra") or []):
            fails.append("extra.py not reported: %s" % res)
    return fails


def test_a_clean_failure_is_really_clean():
    """Обратная сторона: если воркер упал, ничего не тронув, состояние
    обязано быть FAILED_CLEAN, иначе слои будут катать откат ради пустоты."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        ended = ws.finish(began["state"], ok=False, detail={"signal": "kill"})
        if ended["state"]["state"] != ws.FAILED_CLEAN:
            fails.append("untouched tree classified %s"
                         % ended["state"]["state"])
        if ended["rollback_needed"]:
            fails.append("rollback demanded on a clean tree")
    return fails


def test_rewrite_with_identical_length_is_still_dirty():
    """Провал №1 проекта — ассерт переписан под свой баг. Если
    классификация смотрела бы на размер файла, такой подмены она бы
    не увидела."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        # `assert True` -> `assert None`: та же длина, другие байты и
        # другая по смыслу проверка.
        path = os.path.join(fx.worktree, "test_price.py")
        original = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original.replace("assert True", "assert None"))
        if os.path.getsize(path) != len(original.encode("utf-8")):
            fails.append("fixture no longer has equal length; the test would "
                         "be measuring the wrong thing")
        ended = ws.finish(began["state"], ok=True)
        if ended["state"]["state"] != ws.FAILED_DIRTY:
            fails.append("a rewritten test did not make the tree dirty: %s"
                         % ended["state"]["state"])
        if "test_price.py" not in (ended["residue"].get("modified") or []):
            fails.append("the rewritten test was not named: %s"
                         % ended["residue"])
    return fails


def test_worker_reports_success_but_dirty_tree_still_fails():
    """`COMPLETED` — это «воркер сказал готово И дерево совпадает с
    baseline». Нет: успех воркера при изменённом дереве — это
    FAILED_DIRTY. Иначе слой 4 получил бы «готово» на грязном дереве."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        with open(os.path.join(fx.worktree, "price.py"), "a",
                  encoding="utf-8") as f:
            f.write("\n\n")
        ended = ws.finish(began["state"], ok=True)
        if ended["state"]["state"] != ws.FAILED_DIRTY:
            fails.append("a success claim on a dirty tree gave %s"
                         % ended["state"]["state"])
        if ended["state"]["failure"].get("kind") != ws.FAILURE_CONFLICT:
            fails.append("typed as %r" % ended["state"]["failure"].get("kind"))
    return fails


def test_untouched_success_is_completed():
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        ended = ws.finish(began["state"], ok=True)
        if ended["state"]["state"] != ws.COMPLETED:
            fails.append("untouched success classified %s"
                         % ended["state"]["state"])
        if ended["state"].get("state") in ws.TERMINAL:
            pass
        if ws.TERMINAL != frozenset({ws.COMPLETED, ws.RECOVERY_BLOCKED}):
            fails.append("COMPLETED must be terminal, got %s" % sorted(ws.TERMINAL))
    return fails


# --------------------------------------------------------------------------
# rollback is code, and it re-measures itself
# --------------------------------------------------------------------------

def test_rollback_restores_the_baseline():
    """Главное обещание слоя: после отката дерево побайтово равно
    baseline. Проверяется не флагом, а повторным измерением."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        root = fx.worktree
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        os.remove(os.path.join(root, "README.md"))
        with open(os.path.join(root, "added.py"), "w", encoding="utf-8") as f:
            f.write("y = 2\n")

        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        if state["state"] != ws.FAILED_DIRTY:
            return ["setup: expected FAILED_DIRTY, got %s" % state["state"]]

        rolled = ws.rollback(state)
        if not rolled["restored"]:
            return ["rollback failed: %s / %s" % (rolled["reason"],
                                                  rolled.get("detail", ""))]
        if state["state"] != ws.BASELINE_RESTORED:
            fails.append("state after a successful rollback: %s"
                         % state["state"])

        with open(os.path.join(root, "price.py"), encoding="utf-8") as f:
            if f.read() != DEFAULT_FILES["price.py"]:
                fails.append("price.py was not restored")
        if not os.path.isfile(os.path.join(root, "README.md")):
            fails.append("README.md was deleted by the worker and not restored")
        if os.path.exists(os.path.join(root, "added.py")):
            fails.append("the worker's new file survived the rollback")

        # Независимая проверка тем же кодом, что и классификация.
        measured = ws.classify(state)
        res = measured["residue"]
        if measured["dirty"] is not False:
            fails.append("post-rollback measurement says dirty: %s" % res)
        if res["missing"] or res["modified"] or res["extra"]:
            fails.append("residue survived the rollback: %s" % res)
        # `eol_only` после отката — ожидаем: `checkout-index` кладёт
        # байты блоба, то есть LF, а `git worktree add` с autocrlf
        # клал CRLF. Это НЕ остаток (иначе откат никогда не был бы
        # чистым) и НЕ тишина: слой 2 считает такую разницу подменой
        # намеренно, поэтому список обязан быть виден в отчёте.
        if res.get("eol_only") and "eol only" not in ws.render(measured):
            fails.append("the eol difference is not reported anywhere")

        # Дерево воркера обязано выглядеть нормально и для самого git:
        # иначе воркер увидит «всё изменено» и не сможет работать.
        rc, out, _ = bl.git(["status", "--porcelain", "--untracked-files=all"],
                            cwd=root)
        if rc != 0 or out.strip():
            fails.append("git status inside the restored worktree is not "
                         "clean: %s" % out.strip()[:200])

        # Сквозной контраст со слоем 2, и он неочевиден.
        #
        # LEDGER слоя 2 снимает хэши с КОРНЯ проекта, где у вызывающих
        # тестов переводы строк LF, а `git worktree add` (тоже слой 2) с
        # `core.autocrlf=true` кладёт в worktree CRLF. Этот `core.autocrlf`
        # задаёт фикстура (`Fixture._isolate_git_config`), поэтому
        # расхождение воспроизводится
        # на любом хосте и не зависит от глобальной настройки машины.
        # Из-за этого `baseline.verify(ledger, worktree)` на нетронутом дереве
        # показывает подмену — верную по байтам, ложную по смыслу.
        #
        # Это не дефект слоя 3 и не повод «починить» его тем же
        # сравнением: значит, что слой 4 обязан знать про два разных
        # ответа (слой 2 — строгий по байтам для корня, слой 3 — по
        # содержимому для worktree). Проверяется именно это знание,
        # а не «всё зелёное».
        ledger, reason = bl.load_ledger(fx.barrier["ledger_path"])
        if reason:
            fails.append("ledger unreadable: %s" % reason)
            return fails
        in_worktree = bl.verify(ledger, root)
        if not in_worktree["test_tampering"]:
            fails.append("the fixture did not produce the CRLF/LF difference "
                         "it is supposed to produce, so this group measured "
                         "nothing: root=%r eol_only=%r modified=%r"
                         % (DEFAULT_FILES.keys(),
                            in_worktree.get("eol_only"),
                            in_worktree.get("modified")))
        elif in_worktree["eol_only"] != in_worktree["modified"]:
            fails.append("expected the difference to be eol-only: %s"
                         % in_worktree["reasons"])

        # А ответ слоя 3 внутри worktree обязан быть честным: тесты
        # целы, дерево совпадает с baseline.
        answer = ws.classify(state)
        if answer["baseline_tests_intact"] is not True:
            fails.append("layer 3 reports the oracle as broken: %s"
                         % answer["residue"]["tests_modified"])
    return fails


def test_line_endings_alone_are_not_worker_damage():
    """`core.autocrlf=true` (его ставит фикстура, а не глобальная настройка
    машины) кладёт в worktree CRLF, а в теневом store лежит LF. Наивное
    сравнение объявило бы нетронутое дерево грязным, и каждый заход
    заканчивался бы ложным FAILED_DIRTY. При этом смена переводов строк
    остаётся видна — отдельным списком, а не «молча чисто».

    Настоящую подмену содержимого это не ослабляет: тест ниже
    переписывает ассерт, и он ловится."""
    fails = []
    with Fixture() as fx:
        pristine = ws.classify(fx.state)
        if pristine["dirty"] is not False:
            fails.append("an untouched worktree reads as dirty: %s"
                         % pristine["residue"])
        if not pristine["residue"].get("eol_only"):
            # Условие теперь создаёт фикстура (`Fixture._isolate_git_config` ставит
            # core.autocrlf=true в самом репозитории), поэтому пустой
            # список - это дефект, а не «машина не та».
            fails.append("the fixture produced no CRLF/LF difference, so the "
                         "eol invariant was not exercised at all: %s"
                         % (pristine["residue"].get("eol_only"),))

        # Настоящая правка при сохранении длины — обязана ловиться.
        began = ws.start(fx.barrier, fx.state)
        path = os.path.join(fx.worktree, "test_price.py")
        original = open(path, encoding="utf-8", newline="").read()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original.replace("assert True", "assert None"))
        ended = ws.finish(began["state"], ok=True)
        if ended["state"]["state"] != ws.FAILED_DIRTY:
            fails.append("a real content rewrite slipped through the "
                         "eol-tolerant comparison")
        if "test_price.py" not in (ended["residue"].get("modified") or []):
            fails.append("the rewritten test is not in `modified`: %s"
                         % ended["residue"])
    return fails


def test_rollback_leaves_ignored_artifacts_alone():
    """`node_modules/`, `cache/` и прочее вне `git ls-files` создал не
    воркер. Откат, удаляющий такое, — это вред: он сносит окружение,
    которое не трогал, и при этом ничего не чинит."""
    fails = []
    with Fixture() as fx:
        with open(os.path.join(fx.root, ".gitignore"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("cache/\n")
        bl.git(["add", "-A"], cwd=fx.root)
        bl.git(["commit", "--quiet", "-m", "ignore cache"], cwd=fx.root)

        barrier = bl.create_barrier(fx.root, spec_id="S-L3b")
        state = ws.new_state(barrier)
        began = ws.start(barrier, state)

        artifact_dir = os.path.join(fx.worktree, "cache")
        os.makedirs(artifact_dir, exist_ok=True)
        with open(os.path.join(artifact_dir, "model.bin"), "wb") as f:
            f.write(b"\x00\x01" * 64)
        with open(os.path.join(fx.worktree, "price.py"), "w",
                  encoding="utf-8") as f:
            f.write("def fmt(n):\n    return 'X'\n")

        ended = ws.finish(began["state"], ok=False)
        rolled = ws.rollback(ended["state"])
        if not rolled["restored"]:
            return ["rollback failed on an ignored artifact: %s" % rolled["reason"]]
        if not os.path.isfile(os.path.join(artifact_dir, "model.bin")):
            fails.append("rollback deleted an ignored artifact it did not create")
    return fails


def test_a_restore_that_lies_is_not_believed():
    """`checkout-index` может отчитаться об успехе и ничего не сделать.
    Единственная защита — повторное измерение остатка тем же кодом.

    Проверяется подменой git, а не удалением файла вручную: подмена
    бьёт по тому месту, где модуль принимает результат чужого
    инструмента на веру. Без проверки «откат выполнен» здесь было бы
    ровно тем тихим успехом, ради которого слой и написан."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        if state["state"] != ws.FAILED_DIRTY:
            return ["setup: expected FAILED_DIRTY, got %s" % state["state"]]

        real_git = ws.bl.git

        def lying_git(args, cwd=None, env=None):
            if _command_of(args)[:2] == ["checkout-index", "-a"]:
                return 0, "", ""          # отчёт об успехе, файлов нет
            return real_git(args, cwd=cwd, env=env)

        ws.bl.git = lying_git
        try:
            rolled = ws.rollback(state)
        finally:
            ws.bl.git = real_git

        if rolled.get("restored"):
            fails.append("a rollback that restored nothing reported success")
        if rolled["reason"] != ws.REASON_TREE_RESIDUE:
            fails.append("reason %r, expected %s"
                         % (rolled["reason"], ws.REASON_TREE_RESIDUE))
        if state["state"] != ws.RECOVERY_BLOCKED:
            fails.append("a lying restore left the state %s, re-runnable"
                         % state["state"])
        with open(os.path.join(fx.worktree, "price.py"), encoding="utf-8") as f:
            if "return 'X'" in f.read():
                pass                        # файл не тронут — это и ожидалось
            else:
                fails.append("the fixture did not actually dirty the tree")
    return fails


def test_a_locked_file_blocks_the_rollback():
    """Одна из четырёх причин провала отката из §5.3: файл занят
    процессом. Проверяется настоящим локом, а не подменой os.remove:
    подмена доказала бы только то, что модуль читает возвращаемое
    значение, а не то, что замкнутый файл действительно не удаляется."""
    fails = []
    if os.name != "nt":
        return []                          # лок под Windows; на других ОС
                                            # поведение блокировки другое
    import msvcrt

    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]

        # Цель отката: файл, который откат обязан удалить.
        with open(os.path.join(fx.worktree, "added.py"), "w",
                  encoding="utf-8") as f:
            f.write("y = 2\n")
        handle = open(os.path.join(fx.worktree, "added.py"), "r+b")
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            rolled = ws.rollback(state)
        finally:
            handle.close()

        if rolled.get("restored"):
            fails.append("the rollback deleted a file held by a process")
        if rolled["reason"] != ws.REASON_FILE_LOCKED:
            fails.append("reason %r, expected %s"
                         % (rolled["reason"], ws.REASON_FILE_LOCKED))
        if "added.py" not in (rolled.get("locked") or []):
            fails.append("the locked file is not named: %s"
                         % rolled.get("locked"))
        if state["state"] != ws.RECOVERY_BLOCKED:
            fails.append("state %s after a blocked rollback" % state["state"])
    return fails


def test_rollback_of_a_missing_checkpoint_is_refused_not_guessed():
    """Откатываться не к чему — это не повод «откатить куда-нибудь»."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        state["checkpoint"] = {"commit": "1" * 40,
                               "store": os.path.join(fx.private, "gone")}
        rolled = ws.rollback(state)
        if rolled["restored"]:
            fails.append("rollback succeeded without a checkpoint")
        if rolled["reason"] != ws.REASON_NO_CHECKPOINT:
            fails.append("reason %r, expected %s"
                         % (rolled["reason"], ws.REASON_NO_CHECKPOINT))
        if rolled["state"]["state"] != ws.RECOVERY_BLOCKED:
            fails.append("no checkpoint must not leave the state re-runnable: %s"
                         % rolled["state"]["state"])
    return fails


# --------------------------------------------------------------------------
# RECOVERY_BLOCKED is terminal, on disk
# --------------------------------------------------------------------------

def test_crash_with_impossible_rollback_blocks_terminally():
    """DoD фазы: `crash -> откат невозможен` даёт RECOVERY_BLOCKED."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]

        # Откат невозможен: дерево baseline недоступно (store потерян
        # или коммит вычищен), то есть ровно «причины отката могут не
        # сработать» из §5.3.
        state["checkpoint"] = {"commit": "0" * 40,
                               "store": os.path.join(fx.private, "gone")}
        rolled = ws.rollback(state)
        if rolled["status"] != "BLOCKED":
            fails.append("impossible rollback gave %s, expected BLOCKED"
                         % rolled["status"])
        if rolled["state"]["state"] != ws.RECOVERY_BLOCKED:
            fails.append("state %s after an impossible rollback"
                         % rolled["state"]["state"])

        # И никакого следующего шага.
        for label, attempt in (
                ("start", lambda: ws.start(fx.barrier, rolled["state"])),
                ("rework", lambda: ws.rework(rolled["state"])),
                ("rollback again", lambda: ws.rollback(rolled["state"]))):
            result = attempt()
            if result.get("worker_may_start"):
                fails.append("%s allowed after RECOVERY_BLOCKED" % label)
            if result.get("status") not in ("REFUSED", "BLOCKED"):
                fails.append("%s gave %s, expected a refusal" % (label,
                                                                 result["status"]))
    return fails


def test_residue_outside_the_worktree_is_terminal():
    """Причина «файл вне worktree» из §5.3. Откат чинит дерево
    worktree и ничего больше, поэтому такая грязь не лечится откатом —
    это и есть карантин."""
    fails = []
    with Fixture() as fx:
        if not fx.barrier.get("worktree"):
            return ["fixture has no worktree; the check would be vacuous"]
        began = ws.start(fx.barrier, fx.state)
        # Воркер тронул проект, а не изолированное дерево.
        fx.write_root({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        rolled = ws.rollback(ended["state"])
        # Корневой проект грязен → откат worktree его не покрывает.
        if rolled["status"] != "BLOCKED":
            fails.append("residue outside the worktree gave %s (%s)"
                         % (rolled["status"], rolled["reason"]))
        if rolled["reason"] != ws.REASON_OUTSIDE_WORKTREE:
            fails.append("reason %r, expected %s"
                         % (rolled["reason"], ws.REASON_OUTSIDE_WORKTREE))
        if rolled["state"]["state"] != ws.RECOVERY_BLOCKED:
            fails.append("state %s" % rolled["state"]["state"])
    return fails


def test_quarantine_marker_is_on_disk_with_four_fields():
    """Инвариант №4: маркер переживает перезапуск и несёт ровно четыре
    поля — иначе устаревший маркер невозможно диагностировать."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        with open(os.path.join(fx.worktree, "price.py"), "w",
                  encoding="utf-8") as f:
            f.write("def fmt(n):\n    return 'X'\n")
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        # Тождество baseline задаётся переменной, а не двумя литералами
        # в разных местах: рассинхрон между «что подложили» и «что
        # ждём» давал зелёный тест на сломанной проверке.
        lost = "1" * 40
        state["checkpoint"] = {"commit": lost,
                               "store": os.path.join(fx.private, "gone")}
        rolled = ws.rollback(state)

        marker = ws.bl.read_quarantine(rolled["state"]["baseline_id"])
        if not marker:
            fails.append("no marker on disk")
            return fails
        for field in ("worktree_identity", "checkpoint_identity", "reason",
                      "timestamp"):
            if not marker.get(field):
                fails.append("marker field %r is empty" % field)
        if not marker["worktree_identity"]:
            fails.append("worktree identity missing: the marker cannot say "
                         "which tree is quarantined")
        if marker["checkpoint_identity"] != lost:
            fails.append("checkpoint identity %r does not name the baseline"
                         % marker["checkpoint_identity"][:12])
        if not marker.get("timestamp_iso"):
            fails.append("no human-readable timestamp")
    return fails


def test_quarantine_survives_a_process_restart():
    """DoD фазы: «маркер карантина переживает перезапуск процесса».
    Проверяется запуском CLI в новом процессе — состояние в памяти
    исчезло бы, маркер на диске нет."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        state["checkpoint"] = {"commit": "2" * 40,
                               "store": os.path.join(fx.private, "gone")}
        ws.rollback(state)
        bid = state["baseline_id"]

        # Новый процесс. Если бы карантин жил в памяти, `start` прошёл бы.
        proc = cli(fx, ["start", "--ledger", fx.barrier["ledger_path"]])
        if proc.returncode == 0:
            fails.append("a new process was allowed to start the worker on a "
                         "quarantined baseline")
        try:
            payload = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ["CLI stdout is not UTF-8 JSON: %s" % exc]
        if payload.get("worker_may_start"):
            fails.append("worker_may_start true after a restart on a "
                         "quarantined baseline")
        if ws.RECOVERY_BLOCKED not in str(payload.get("reason", "")):
            fails.append("refusal does not name the quarantine: %r"
                         % payload.get("reason"))
        if not ws.bl.is_quarantined(bid):
            fails.append("the marker did not survive")
    return fails


def test_unblock_requires_a_reason_and_returns_to_not_started():
    """Явная очистка человеком. Без причины маркер снимается и
    непонятно, что человек вообще видел; а состояние возвращается в
    NOT_STARTED, потому что правильное восстановление загрязнённой среды
    — новый baseline, а не продолжение старого."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        state = ws.finish(began["state"], ok=False)["state"]
        state["checkpoint"] = {"commit": "3" * 40,
                               "store": os.path.join(fx.private, "gone")}
        rolled = ws.rollback(state)
        if rolled["status"] != "BLOCKED":
            return ["setup: expected BLOCKED, got %s" % rolled["status"]]

        quiet = ws.unblock(fx.barrier, "   ")
        if quiet["status"] != "REFUSED":
            fails.append("unblock without a reason was accepted")
        if not ws.bl.is_quarantined(rolled["state"]["baseline_id"]):
            fails.append("a refused unblock still cleared the marker")

        released = ws.unblock(fx.barrier, "removed the stray file by hand")
        if released["status"] != "OK":
            fails.append("unblock refused: %s" % released["reason"])
        if ws.bl.is_quarantined(rolled["state"]["baseline_id"]):
            fails.append("marker survived an explicit release")
        if released["state"]["state"] != ws.NOT_STARTED:
            fails.append("after a release the state is %s; a fresh baseline "
                         "is required, not a resume"
                         % released["state"]["state"])
        allowed, why = ws.can_run(released["state"])
        if not allowed:
            fails.append("after an explicit release the worker cannot start: %s"
                         % why)
    return fails


def test_contradicted_clean_classification_blocks():
    """Два измерения разошлись: сначала «чисто», потом остаток. Значит
    одно из них соврало, и продолжать нельзя — это карантин, а не
    предупреждение."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        ended = ws.finish(began["state"], ok=False)
        state = ended["state"]
        if state["state"] != ws.FAILED_CLEAN:
            return ["setup: expected FAILED_CLEAN, got %s" % state["state"]]
        # Воркер пишет уже после классификации.
        with open(os.path.join(fx.worktree, "price.py"), "w",
                  encoding="utf-8") as f:
            f.write("def fmt(n):\n    return 'X'\n")
        rolled = ws.rollback(state)
        if rolled["status"] != "BLOCKED":
            fails.append("a contradicted measurement gave %s" % rolled["status"])
        if rolled["reason"] != ws.REASON_CLEAN_CONTRADICTED:
            fails.append("reason %r" % rolled["reason"])
    return fails


# --------------------------------------------------------------------------
# WORKER_CRASH is not a reason to repeat the same prompt
# --------------------------------------------------------------------------

def test_worker_crash_never_auto_reworks():
    """§6/§10: повтор после краша воспроизводит то же самое. Но §5.3
    говорит «после успешного отката можно rework». Оба выполняются:
    машина разрешает, человек решает, решение в истории."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False, detail={"signal": "killed"})
        state = ended["state"]
        if state["failure"].get("kind") != ws.FAILURE_CRASH:
            return ["crash not typed as %s, got %r"
                    % (ws.FAILURE_CRASH, state["failure"].get("kind"))]
        rolled = ws.rollback(state)
        if not rolled["restored"]:
            return ["setup: rollback failed: %s" % rolled.get("reason")]
        if not rolled["rework_possible"]:
            fails.append("after a successful rollback rework must be "
                         "physically possible (§5.3)")
        if not rolled["rework_needs_human"]:
            fails.append("rework_needs_human must be set after a crash")

        silent = ws.rework(rolled["state"], task_digest="same")
        if silent["status"] != "REFUSED":
            fails.append("a crash was retried without a human decision")
        if not silent.get("needs_human_authorization"):
            fails.append("the refusal does not name the missing decision")
        if rolled["state"]["state"] != ws.BASELINE_RESTORED:
            fails.append("a refused rework moved the state to %s"
                         % rolled["state"]["state"])

        allowed = ws.rework(rolled["state"], task_digest="same",
                            human_authorized=True, reason="different approach")
        if allowed["status"] != "OK":
            fails.append("an authorized rework was refused: %s"
                         % allowed["reason"])
        if rolled["state"]["state"] != ws.RUNNING:
            fails.append("state after an authorized rework: %s"
                         % rolled["state"]["state"])
        last = rolled["state"]["history"][-1]["detail"]
        if not last.get("human_authorized") or not last.get("reason"):
            fails.append("the human decision is not in the history: %s" % last)
    return fails


def test_clean_failure_reworks_without_a_human():
    """Упал, ничего не тронув, — это `WORKER_ABORTED`, и повтор там
    обычное дело. Иначе конвейер вставал бы на первой же неудаче, а
    §5.1 прямо запрещает останавливать работу там, где доказать нельзя,
    а сделать можно."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        ended = ws.finish(began["state"], ok=False, detail={"tests": "3 failed"})
        kind = ended["state"]["failure"].get("kind")
        if kind != ws.FAILURE_ABORTED:
            fails.append("a clean failure typed as %r, expected %s"
                         % (kind, ws.FAILURE_ABORTED))
        again = ws.rework(ended["state"], task_digest="t2")
        if again["status"] != "OK":
            fails.append("a plain failure refused rework: %s" % again["reason"])
        if ended["state"]["state"] != ws.RUNNING:
            fails.append("state after rework: %s" % ended["state"]["state"])
    return fails


def test_a_lying_success_is_gated_like_a_crash():
    """Воркер рапортует «готово», оставив дерево изменённым. Это
    ровно форма провала `$1,234.50`: тест переписан под свой вывод, а
    воркер доволен. Повтор такого поведения вслепую повторяет его."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        path = os.path.join(fx.worktree, "test_price.py")
        original = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original.replace("assert True", "assert None"))
        ended = ws.finish(began["state"], ok=True)
        state = ended["state"]
        if state["failure"].get("kind") != ws.FAILURE_CONFLICT:
            fails.append("a lying success typed as %r"
                         % state["failure"].get("kind"))
        rolled = ws.rollback(state)
        if not rolled["restored"]:
            return ["setup: rollback failed: %s" % rolled.get("reason")]
        silent = ws.rework(rolled["state"], task_digest="t")
        if silent["status"] != "REFUSED":
            fails.append("a lying success was retried without a human")
        if not silent.get("needs_human_authorization"):
            fails.append("the refusal does not name the missing decision")
    return fails


# --------------------------------------------------------------------------
# barrier first (invariant 7)
# --------------------------------------------------------------------------

def test_no_ledger_no_worker():
    """Инвариант №7. «Проверь барьер перед запуском» должно быть
    свойством модуля, а не дисциплиной вызывающего."""
    fails = []
    with Fixture() as fx:
        missing = os.path.join(fx.home, "absent.ledger.json")
        result = ws.run(missing, "do something")
        if result["status"] != "REFUSED":
            fails.append("run without a ledger gave %s" % result["status"])
        if result["worker_may_start"]:
            fails.append("worker_may_start without a baseline")
        if result["phase"] != "barrier":
            fails.append("phase %r: the refusal must come from the barrier"
                         % result["phase"])
    return fails


def test_dirty_tree_refuses_before_the_worker():
    fails = []
    with Fixture() as fx:
        with open(os.path.join(fx.root, "price.py"), "a", encoding="utf-8") as f:
            f.write("\n# someone else's work\n")
        result = ws.run(fx.barrier["ledger_path"], "do something")
        if result["status"] != "REFUSED":
            fails.append("run on a dirty tree gave %s" % result["status"])
        if "tree_dirty" not in str(result["reason"]):
            fails.append("reason %r does not name the dirty tree"
                         % result["reason"])
    return fails


def test_second_run_on_a_running_baseline_is_refused():
    """Два воркера на одном baseline — гонка, в которой выигравший
    неизвестен, а барьер слоя 2 на них общий."""
    fails = []
    with Fixture() as fx:
        first = ws.start(fx.barrier, fx.state)
        if not first["worker_may_start"]:
            return ["setup: start refused: %s" % first["reason"]]
        second = ws.start(fx.barrier, first["state"])
        if second["worker_may_start"]:
            fails.append("a second worker was started on a RUNNING baseline")
        if second["status"] != "REFUSED":
            fails.append("second start gave %s" % second["status"])
    return fails


# --------------------------------------------------------------------------
# the $0 law and the measurement rules
# --------------------------------------------------------------------------

def test_paid_routes_cannot_reach_the_worker():
    """`openrouter/*` внутри opencode CLI тянет общий дневной бакет
    OpenRouter — единственный способ нарушить закон $0 отсюда."""
    fails = []
    for model in ("openrouter/nemotron-3-super", "anthropic/claude",
                  "", "gpt-4"):
        ok, why = ws.free_route_ok(model)
        if ok:
            fails.append("route %r accepted" % model)
        if not why:
            fails.append("route %r refused without a reason" % model)
    for model in ws.LADDER:
        ok, why = ws.free_route_ok(model)
        if not ok:
            fails.append("ladder rung %r rejected: %s" % (model, why))
    if "opencode/ling-3.0-flash-fin-free" not in ws.DEAD_MODELS:
        fails.append("the dead model is not in DEAD_MODELS; the ladder would "
                     "burn 77-81s on it again")
    ok, why = ws.free_route_ok("opencode/ling-3.0-flash-fin-free")
    if ok or "dead" not in why:
        fails.append("the dead model was not refused as dead: %r" % why)

    outcome = ws.run_worker("task", models=["openrouter/nemotron-3-super"])
    if outcome["attempts"]:
        fails.append("a paid route was launched: %s" % outcome["attempts"])
    if outcome["ok"]:
        fails.append("a paid route reported success")
    return fails


def test_ladder_takes_two_attempts_per_model(monkey_free=()):
    """Правило двух попыток: мёртвый эндпоинт держит соединение 75-81 с
    и только потом отдаёт ошибку, а живые модели при этом давали 91.4 с.
    Одна попытка — это флак, а не приговор."""
    fails = []
    calls = []

    def fake(model, task, timeout=None):
        calls.append(model)
        return {"model": model, "launched": True, "exit_code": 1,
                "timed_out": False, "seconds": 80.0, "stdout": "boom",
                "stderr": "Endpoint is unavailable", "claim": "boom", "ok": False,
                "reason": ws.REASON_WORKER_NONZERO}

    original = ws.attempt_model
    ws.attempt_model = fake
    try:
        outcome = ws.run_worker("task", models=["opencode/a-free",
                                                 "opencode/b-free"],
                                attempts=ws.ATTEMPTS_PER_MODEL)
    finally:
        ws.attempt_model = original

    if len(calls) != 4:
        fails.append("expected 2 attempts on each of 2 models, got %d: %s"
                     % (len(calls), calls))
    if outcome["ok"] or outcome["served_by"]:
        fails.append("a ladder of dead models reported success")
    if len(outcome["attempts"]) != 4:
        fails.append("the history lost attempts: %d" % len(outcome["attempts"]))
    if not all(a.get("seconds") is not None for a in outcome["attempts"]):
        fails.append("attempt durations missing: a single measurement must not "
                     "be mistaken for a verdict (§3: median of 5)")
    return fails


def test_a_slow_success_is_not_killed_after_one_try():
    """Живая модель может отвечать минуту и больше. Таймаут по умолчанию
    это переживает, но проверка обязана быть явной: иначе «медленно»
    будет выглядеть как «мёртво»."""
    fails = []
    calls = []

    def slow_but_alive(model, task, timeout=None):
        calls.append(model)
        if len(calls) == 1:
            return {"model": model, "launched": True, "exit_code": 1,
                    "timed_out": False, "seconds": 81.0, "stdout": "",
                    "stderr": "timeout-ish", "claim": "", "ok": False,
                    "reason": ws.REASON_WORKER_NONZERO}
        return {"model": model, "launched": True, "exit_code": 0,
                "timed_out": False, "seconds": 91.4, "stdout": "done",
                "stderr": "", "claim": "done", "ok": True,
                "reason": ws.REASON_WORKER_OK}

    original = ws.attempt_model
    ws.attempt_model = slow_but_alive
    try:
        outcome = ws.run_worker("task", models=["opencode/space-bunny-free"])
    finally:
        ws.attempt_model = original
    if not outcome["ok"] or outcome["served_by"] != "opencode/space-bunny-free":
        fails.append("a model that succeeded on the second try was dropped")
    if len(calls) != 2:
        fails.append("calls: %s" % calls)
    return fails


def test_argv_has_no_dash_f_attachment_trap():
    """`opencode run -f FILE` — это вложение, а не сообщение: промпт уходит
    в Attach, ран висит без вывода (NOTES.md). И `-m` обязателен."""
    fails = []
    argv = ws.build_argv("opencode/space-bunny-free", "a" * 5000)
    if "-f" in argv or "--file" in argv:
        fails.append("argv uses -f: the prompt would be attached, not sent")
    if "-m" not in argv:
        fails.append("argv has no -m: the CLI would fall back to its default "
                     "model, which here has resolved to openrouter/*")
    if argv[1] != "run" or argv[2] != "-m":
        fails.append("unexpected argv shape: %s" % argv[:4])
    if argv[-1] != "a" * 5000:
        fails.append("the task was mangled on the way to argv")
    return fails


# --------------------------------------------------------------------------
# this layer gives no verdicts
# --------------------------------------------------------------------------

def test_state_machine_issues_no_verdict():
    """Вердикт — право слоя 4 и ядра. Здесь любой `task_status` или
    `PASS` был бы вором права, и проверять это надо на выходе, а не на
   мере слова в docstring."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        write_repo(fx.root, {"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        rolled = ws.rollback(ended["state"])
        rendered = ws.render(rolled)
        state = rolled["state"]
        for banned in ("task_status", "verified", "TEST_TAMPERING", "PASS",
                       "REVIEW_FAILURE", "MECHANICAL_FAILURE"):
            if banned in rendered:
                fails.append("the report contains %r, which belongs to layers "
                             "4-6" % banned)
            if banned in json.dumps(state, ensure_ascii=False):
                fails.append("the state file contains %r: this layer must not "
                             "reach into the verdict vocabulary" % banned)
        if "PASS" in ws.render(ended):
            fails.append("finish() reported a PASS")
    return fails


def test_worker_claim_is_kept_separate_and_labelled():
    """§7: `WORKER_CLAIM` — с наименьшим доверием. Смешанный с фактами,
    он становится частью доказательства, которой не является.

    Вывод воркера берётся из фикстуры (`ws.attempt_model`), а не из живого
    `opencode` в PATH. Причина не в скорости: `opencode` есть только на
    машине разработчика, и без него `_tail("")` даёт пустую строку, `render`
    не печатает строку claim вовсе — и группа падала, ничего не проверяя.
    То есть проверка метки зависела от того, установлен ли сторонний CLI.

    Фикстура использует тот же шов, что и группа полного цикла ниже
    (`ws.attempt_model` подменяется и восстанавливается), поэтому проверка
    остаётся настоящей: claim непустой, попадает в отчёт, помечен
    «low trust, not a verdict» и не превращается в вердикт слоя 4.
    """
    fails = []
    worker_says = "I fixed it, trust me\ntests pass, I checked"
    original = ws.attempt_model

    def fixture_worker(model, task, timeout=None):
        return {"model": model, "launched": True, "exit_code": 0,
                "timed_out": False, "seconds": 0.01, "stdout": worker_says,
                "stderr": "", "claim": ws._tail(worker_says), "ok": True,
                "reason": ws.REASON_WORKER_OK}

    with Fixture() as fx:
        ws.attempt_model = fixture_worker
        try:
            began = ws.start(fx.barrier, fx.state)
            outcome = ws.run_worker("t", models=["opencode/space-bunny-free"])
        finally:
            ws.attempt_model = original
        if not outcome["stdout"]:
            fails.append("the fixture worker produced no stdout, so the claim "
                         "under test would be empty by construction")
        state = began["state"]
        state["run"] = {"ok": outcome["ok"], "reason": outcome["reason"],
                        "served_by": outcome["served_by"],
                        "attempts": outcome["attempts"],
                        "skipped": outcome["skipped"],
                        "worker_claim": {"trust": "low",
                                         "text": ws._tail(outcome["stdout"])}}
        rendered = ws.render({"status": "OK", "state": state})
        if "worker claim" not in rendered:
            fails.append("the claim is not marked as low trust in the report")
        if state["run"]["worker_claim"]["trust"] != "low":
            fails.append("claim trust marker missing")
        # Метка должна быть ИМЕННО про доверие, а не про успех: зелёный текст
        # воркера не является вердиктом, и отчёт не имеет права этого скрыть.
        if "not a verdict" not in rendered:
            fails.append("the claim line does not say it is not a verdict: %s"
                         % [l for l in rendered.splitlines()
                            if "worker claim" in l])
        if "I fixed it" not in rendered:
            fails.append("the worker's own words are missing from the report: "
                         "a claim nobody can read is not a separate record")
    return fails


# --------------------------------------------------------------------------
# resilience
# --------------------------------------------------------------------------

def test_state_survives_on_disk_and_reloads():
    """Состояние обязано быть на диске: процесс умирает в любой момент,
    а карантин, потерянный вместе с памятью, перестаёт быть карантином."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        bid = began["state"]["baseline_id"]
        path = ws.state_path(bid)
        if not os.path.isfile(path):
            return ["no state file at %s" % path]
        reloaded = ws.load_state(bid)
        if reloaded.get("state") != ws.RUNNING:
            fails.append("reloaded state %r" % reloaded.get("state"))
        if not reloaded.get("history"):
            fails.append("history lost on reload: %s" % reloaded["history"])
        if not reloaded.get("checkpoint", {}).get("commit"):
            fails.append("checkpoint identity lost: rollback would have "
                         "nothing to roll back to")
    return fails


def test_unsafe_ids_are_refused():
    """Идентификатор попадает в имя файла конкатенацией — без проверки
    это путь обхода карантина."""
    fails = []
    for bad in ("../../escape", "..", "a/b", "", "x" * 200, "a\\..\\b"):
        try:
            ws.state_path(bad)
            fails.append("id %r was accepted" % bad)
        except ValueError:
            pass
    return fails


def test_state_survives_a_corrupt_file():
    """Битый файл состояния не должен приводить к «карантина нет»."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        bid = began["state"]["baseline_id"]
        with open(ws.state_path(bid), "w", encoding="utf-8") as f:
            f.write("{not json")
        if ws.load_state(bid) != {}:
            fails.append("a corrupt state file was accepted")
        # А карантин, если он есть, читается с диска независимо.
        ws.bl.mark_quarantine(bid, "wt@head", "commit", "manual test")
        ctx = {"ledger": fx.barrier, "state": ws.new_state(fx.barrier)}
        allowed, why = ws.can_run(ctx["state"])
        if allowed:
            fails.append("a corrupt state file hid the quarantine marker")
    return fails


def test_broken_input_never_crashes():
    """Модуль стоит в конвейере: исключение на ровном месте выглядит
    как «слой сломался», а не как «данные плохие»."""
    fails = []
    for bad_root in ("", os.path.join(os.environ.get("TEMP", "C:\\"),
                                      "definitely-missing-dir-xyz"),
                     "Z:\\no-such-volume"):
        state = ws.new_state({"baseline_id": "abc123def4560000",
                              "root": bad_root,
                              "worktree": bad_root,
                              "checkpoint": {"commit": "0" * 40,
                                             "store": bad_root}})
        try:
            measured = ws.classify(state)
            if measured["dirty"] is True and not measured.get("residue"):
                fails.append("dirty without a reason for %r" % bad_root)
            ws.rollback(state)
        except Exception as exc:                                # noqa: BLE001
            fails.append("%r raised %s: %s" % (bad_root, type(exc).__name__, exc))
    if ws._digest("x") == ws._digest("y"):
        fails.append("task digests collide on trivially different input")
    return fails


def test_report_bytes_are_utf8():
    """Отчёт по-русски, консоль cp866/cp1251. Мусор в выводе читается
    как поломка и маскирует настоящую (тот же класс, что MEMORY.md)."""
    fails = []
    with Fixture() as fx:
        began = ws.start(fx.barrier, fx.state)
        write_repo(fx.root, {"price.py": "def fmt(n):\n    return 'X'\n"})
        ended = ws.finish(began["state"], ok=False)
        rendered = ws.render(ws.rollback(ended["state"]))
        try:
            rendered.encode("utf-8")
        except UnicodeEncodeError as exc:
            fails.append("render is not encodable as UTF-8: %s" % exc)
        proc = cli(fx, ["classify", "--ledger", fx.barrier["ledger_path"]])
        try:
            proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            fails.append("CLI stdout is not valid UTF-8: %s" % exc)
    return fails


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_lifecycle_end_to_end():
    """CLI — это то, чем модулем пользуется ядро. Он обязан работать
    сам по себе, без python-импорта руками."""
    fails = []
    with Fixture() as fx:
        ledger = fx.barrier["ledger_path"]

        start = cli(fx, ["start", "--ledger", ledger])
        if start.returncode != 0:
            fails.append("start exited %d: %s"
                         % (start.returncode,
                            start.stderr.decode("utf-8", "replace")[-300:]))
        try:
            payload = json.loads(start.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ["start output is not UTF-8 JSON: %s" % exc]
        if payload["state"]["state"] != ws.RUNNING:
            fails.append("start left the state %s" % payload["state"]["state"])

        # Падение с правками: цена $1,234.50 в виде грязного дерева.
        with open(os.path.join(fx.worktree, "test_price.py"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("def test_format():\n    assert None\n")
        failed = cli(fx, ["fail", "--ledger", ledger, "--reason", "killed"])
        payload = json.loads(failed.stdout.decode("utf-8"))
        if payload["state"]["state"] != ws.FAILED_DIRTY:
            fails.append("fail classified %s" % payload["state"]["state"])

        rolled = cli(fx, ["rollback", "--ledger", ledger])
        if rolled.returncode != 0:
            fails.append("rollback exited %d on a restorable tree: %s"
                         % (rolled.returncode,
                            rolled.stderr.decode("utf-8", "replace")[-300:]))
        payload = json.loads(rolled.stdout.decode("utf-8"))
        if not payload["restored"]:
            fails.append("rollback did not restore: %s" % payload["reason"])
        with open(os.path.join(fx.worktree, "test_price.py"),
                  encoding="utf-8") as f:
            if f.read() != DEFAULT_FILES["test_price.py"]:
                fails.append("the CLI rollback did not restore the test file")

        shown = cli(fx, ["state", "--ledger", ledger])
        payload = json.loads(shown.stdout.decode("utf-8"))
        if payload["state"]["state"] != ws.BASELINE_RESTORED:
            fails.append("state after the CLI rollback: %s"
                         % payload["state"]["state"])
        if not payload.get("history"):
            fails.append("the CLI lost the history")

        table = cli(fx, ["transitions"])
        body = table.stdout.decode("utf-8")
        if ws.RECOVERY_BLOCKED not in body or ws.BASELINE_RESTORED not in body:
            fails.append("transitions does not print the contract")
    return fails


def test_cli_rollback_failure_exits_nonzero():
    """Отказ обязан быть виден в коде возврата: иначе вызывающий
    продолжит конвейер, не заметив карантина."""
    fails = []
    with Fixture() as fx:
        ledger = fx.barrier["ledger_path"]
        cli(fx, ["start", "--ledger", ledger])
        fx.write({"price.py": "def fmt(n):\n    return 'X'\n"})
        cli(fx, ["fail", "--ledger", ledger, "--reason", "x"])

        # Убираем точку отката — откат обязан провалиться, а не
        # «успешно» ничего не сделать.
        store = fx.barrier["checkpoint"]["store"]
        shutil.rmtree(store, ignore_errors=True)
        rolled = cli(fx, ["rollback", "--ledger", ledger])
        if rolled.returncode == 0:
            fails.append("a failed rollback exited 0")
        payload = json.loads(rolled.stdout.decode("utf-8"))
        if payload["status"] != "BLOCKED":
            fails.append("status %s without a checkpoint" % payload["status"])

        # И новый процесс уже не может начать работу.
        again = cli(fx, ["start", "--ledger", ledger])
        if again.returncode == 0:
            fails.append("start after a quarantine exited 0")
    return fails


def test_cli_run_refuses_without_a_ledger():
    fails = []
    with Fixture() as fx:
        proc = cli(fx, ["run", "--ledger", os.path.join(fx.home, "nope.json"),
                        "--task", "do it"])
        if proc.returncode == 0:
            fails.append("run without a ledger exited 0")
    return fails


def test_run_full_cycle_rolls_back_a_dirty_worker():
    """Полный заход целиком, с подменённым исполнителем: барьер →
    RUNNING → падение → измерение → откат. Именно этот путь вызывает
    ядро, и он обязан сходиться в BASELINE_RESTORED, а не в
    полузаписанном состоянии."""
    fails = []
    original = ws.attempt_model
    holder = {}

    def dirty_worker(model, task, timeout=None):
        write_repo(holder["wt"], {"price.py": "def fmt(n):\n    return 'X'\n"})
        return {"model": model, "launched": True, "exit_code": 1,
                "timed_out": False, "seconds": 3.0, "stdout": "I fixed it",
                "stderr": "", "claim": "I fixed it", "ok": False,
                "reason": ws.REASON_WORKER_NONZERO}

    with Fixture() as fx:
        holder["wt"] = fx.worktree
        ws.attempt_model = dirty_worker
        try:
            result = ws.run(fx.barrier["ledger_path"], "implement fmt")
        finally:
            ws.attempt_model = original

        if result["status"] != "OK":
            fails.append("full cycle gave %s (%s)" % (result["status"],
                                                      result["reason"]))
        state = result["state"]
        if state["state"] != ws.BASELINE_RESTORED:
            fails.append("state after the cycle: %s" % state["state"])
        if not result.get("rollback", {}).get("restored"):
            fails.append("the cycle did not restore the tree")
        if result["worker"]["worker_claim"]["text"] != "I fixed it":
            fails.append("the worker's claim was lost instead of being kept "
                         "separately: %s" % result.get("worker"))
        if result["worker"]["worker_claim"]["trust"] != "low":
            fails.append("the claim is not marked as low trust")
        with open(os.path.join(fx.worktree, "price.py"), encoding="utf-8") as f:
            if f.read() != DEFAULT_FILES["price.py"]:
                fails.append("price.py was not restored by the full cycle")
        if ws.classify(state)["dirty"] is not False:
            fails.append("post-cycle measurement says the tree is dirty")
        if not os.path.isfile(result["worker"]["log"]):
            fails.append("no worker log was written")
    return fails


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------

TESTS = [
    ("transition table matches the scheme", test_transition_table_matches_the_scheme),
    ("RECOVERY_BLOCKED has no exit (invariant 3)", test_recovery_blocked_has_no_exit),
    ("illegal transition raises, never writes", test_illegal_transition_raises_instead_of_writing),
    ("the worker cannot declare CLEAN vs DIRTY", test_the_worker_cannot_declare_its_own_failure_class),
    ("a clean failure is really clean", test_a_clean_failure_is_really_clean),
    ("same-length rewrite is still dirty", test_rewrite_with_identical_length_is_still_dirty),
    ("line endings alone are not worker damage", test_line_endings_alone_are_not_worker_damage),
    ("success claim on a dirty tree is not COMPLETED", test_worker_reports_success_but_dirty_tree_still_fails),
    ("untouched success is COMPLETED", test_untouched_success_is_completed),
    ("rollback restores the baseline, re-measured", test_rollback_restores_the_baseline),
    ("rollback leaves ignored artifacts alone", test_rollback_leaves_ignored_artifacts_alone),
    ("a restore that lies is not believed", test_a_restore_that_lies_is_not_believed),
    ("a locked file blocks the rollback", test_a_locked_file_blocks_the_rollback),
    ("rollback without a checkpoint is refused", test_rollback_of_a_missing_checkpoint_is_refused_not_guessed),
    ("crash + impossible rollback = RECOVERY_BLOCKED", test_crash_with_impossible_rollback_blocks_terminally),
    ("residue outside the worktree is terminal", test_residue_outside_the_worktree_is_terminal),
    ("quarantine marker has four fields on disk", test_quarantine_marker_is_on_disk_with_four_fields),
    ("quarantine survives a process restart", test_quarantine_survives_a_process_restart),
    ("unblock needs a reason, returns to NOT_STARTED", test_unblock_requires_a_reason_and_returns_to_not_started),
    ("contradicted clean measurement blocks", test_contradicted_clean_classification_blocks),
    ("WORKER_CRASH never auto-reworks", test_worker_crash_never_auto_reworks),
    ("a plain failure reworks freely", test_clean_failure_reworks_without_a_human),
    ("a lying success is gated like a crash", test_a_lying_success_is_gated_like_a_crash),
    ("no ledger, no worker (invariant 7)", test_no_ledger_no_worker),
    ("dirty tree refuses before the worker", test_dirty_tree_refuses_before_the_worker),
    ("a second run on RUNNING is refused", test_second_run_on_a_running_baseline_is_refused),
    ("paid routes cannot reach the worker", test_paid_routes_cannot_reach_the_worker),
    ("two attempts per model (rule of two)", test_ladder_takes_two_attempts_per_model),
    ("a slow success survives the first failure", test_a_slow_success_is_not_killed_after_one_try),
    ("argv avoids the -f attachment trap", test_argv_has_no_dash_f_attachment_trap),
    ("this layer issues no verdict", test_state_machine_issues_no_verdict),
    ("worker claim is separate and labelled", test_worker_claim_is_kept_separate_and_labelled),
    ("state lives on disk and reloads", test_state_survives_on_disk_and_reloads),
    ("unsafe ids are refused", test_unsafe_ids_are_refused),
    ("a corrupt state file cannot hide the quarantine", test_state_survives_a_corrupt_file),
    ("broken input never crashes", test_broken_input_never_crashes),
    ("report bytes are UTF-8", test_report_bytes_are_utf8),
    ("cli lifecycle end to end", test_cli_lifecycle_end_to_end),
    ("cli rollback failure exits nonzero", test_cli_rollback_failure_exits_nonzero),
    ("cli run refuses without a ledger", test_cli_run_refuses_without_a_ledger),
    ("full cycle rolls back a dirty worker", test_run_full_cycle_rolls_back_a_dirty_worker),
]


def main() -> int:
    if not git_ok():
        print("git is required for these tests (real repos, no fakes)")
        return 2

    failed = 0
    for label, fn in TESTS:
        try:
            fails = fn() or []
        except AssertionError as exc:
            fails = ["assertion: %s" % exc]
        except Exception as exc:                               # noqa: BLE001
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
