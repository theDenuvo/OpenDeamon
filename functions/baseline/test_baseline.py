"""baseline barrier tests — invariants of layer 2 (SCHEME.md §8, TODO.md слой 2).

Stdlib only, no network. Every group is a regression against a specific way
this barrier could rot into decoration:

  * baseline created after the worker          -> the barrier stops being one
  * a dirty tree accepted as baseline         -> "what changed" is unprovable
  * worker able to read the LEDGER            -> it prepares the review picture
  * a new test file treated as tampering      -> V2's mistake, kills useful TDD
  * a deleted baseline test read as harmless  -> removing an oracle == rewriting it
  * line-ending change tolerated              -> hash weakened by accident
  * snapshot prune eating the verified slot   -> reviewer reads a dead commit
  * quarantine marker in memory               -> restart makes a dirty tree usable
  * worker being given a PASS/FAIL voice      -> layer 4's right, stolen here

Real git repos are created in temp dirs: a barrier whose hashing was only
ever exercised against a fake tree is a barrier tested against nothing.
Git is required; if it is missing the suite says so instead of passing
vacuously.

Run: py A:/OpenDeamon/functions/baseline/test_baseline.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE / "baseline.py"

spec = importlib.util.spec_from_file_location("baseline", MODULE)
bl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bl)

PY = sys.executable


def git_ok() -> bool:
    return shutil.which("git") is not None and bl.git(["--version"])[0] == 0


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def make_repo(files: dict | None = None) -> str:
    """Настоящий git-репозиторий во временном каталоге."""
    root = tempfile.mkdtemp(prefix="baseline-repo-")
    bl.git(["init", "--quiet", "-b", "main", root])
    bl.git(["config", "user.email", "t@opendeamon.local"], cwd=root)
    bl.git(["config", "user.name", "baseline tests"], cwd=root)
    bl.git(["config", "commit.gpgsign", "false"], cwd=root)
    write_repo(root, files if files is not None else DEFAULT_FILES)
    bl.git(["add", "-A"], cwd=root)
    bl.git(["commit", "--quiet", "-m", "initial"], cwd=root)
    return root


DEFAULT_FILES = {
    "test_price.py": "def test_format():\n    assert True\n",
    "test_cart.py": "def test_total():\n    assert 1 + 1 == 2\n",
    "price.py": "def fmt(n):\n    return str(n)\n",
    "README.md": "# fixture\n",
    "src/test_helper.py": "def helper():\n    return 1\n",
}


def write_repo(root: str, files: dict) -> None:
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def hermes_home() -> str:
    return tempfile.mkdtemp(prefix="baseline-home-")


class Env:
    """Отдельный HERMES_HOME на тест: кэш и store не должны шарить
    состояние между группами.

    `keep=True` оставляет каталог на месте — нужно там, где LEDGER
    переживает выход из блока, иначе проверять после `__exit__` нечего."""

    def __init__(self, keep: bool = False):
        self.home = hermes_home()
        self.keep = keep
        self.old = os.environ.get("HERMES_HOME")

    def __enter__(self):
        os.environ["HERMES_HOME"] = self.home
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old
        if not self.keep:
            shutil.rmtree(self.home, ignore_errors=True)


# --------------------------------------------------------------------------
# the barrier itself
# --------------------------------------------------------------------------

def test_barrier_is_required_before_the_worker():
    """Инвариант №7: baseline ДО воркера, и это барьер, а не подсказка."""
    fails = []
    with Env():
        gate = bl.require_barrier("")
        if gate["worker_may_start"] is not False:
            fails.append("a missing ledger allowed the worker to start")
        if gate["status"] != bl.BARRIER_MISSING:
            fails.append("status %s for a missing ledger" % gate["status"])
        if gate["reason"] != bl.REASON_LEDGER_ABSENT:
            fails.append("reason %r" % gate["reason"])

        root = make_repo()
        try:
            result = bl.create_barrier(root, spec_id="S1")
            if not result.get("worker_may_start"):
                fails.append("a clean tree failed the barrier: %s"
                             % result.get("reasons"))
            if not result.get("ledger_path"):
                fails.append("no ledger path")
            after = bl.require_barrier(result["ledger_path"])
            if not after["worker_may_start"]:
                fails.append("a written ledger did not open the barrier")
            if after["baseline_id"] != result["baseline_id"]:
                fails.append("baseline id changed between write and read")
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return fails


def test_dirty_tree_is_refused():
    """TODO слой 2 п.4: перечень изменённых файлов на baseline — empty."""
    fails = []
    root = make_repo()
    try:
        with open(os.path.join(root, "price.py"), "a", encoding="utf-8") as f:
            f.write("\ndef extra():\n    return 2\n")
        cap = bl.capture(root, spec_id="dirty")
        if cap["status"] != bl.BARRIER_DIRTY:
            fails.append("dirty tree accepted: status %s" % cap["status"])
        if not cap["dirty"]:
            fails.append("the dirty path list is empty, so nothing was refused")
        if cap["reason"] != bl.REASON_TREE_DIRTY:
            fails.append("reason %r" % cap["reason"])

        with Env():
            barrier = bl.create_barrier(root, spec_id="dirty")
            if barrier["worker_may_start"]:
                fails.append("create_barrier let a worker start on a dirty tree")
            if os.path.exists(barrier["ledger_path"] or "\0"):
                fails.append("a ledger was written for a refused baseline")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_untracked_files_also_block():
    """Untracked — тоже «изменённые файлы»: иначе подмена проходит через
    `git add` воркера или через чистый untracked-файл рядом с тестом."""
    fails = []
    root = make_repo()
    try:
        with open(os.path.join(root, "test_new_untracked.py"), "w",
                  encoding="utf-8") as f:
            f.write("def test_x():\n    assert True\n")
        cap = bl.capture(root)
        if cap["status"] != bl.BARRIER_DIRTY:
            fails.append("untracked test file did not block the baseline")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_ledger_hashes_every_test_not_just_criteria():
    """Все существующие тесты, а не только те, что упомянуты в критериях:
    оракул нельзя переписать «не потому, что его не было в задаче»."""
    fails = []
    root = make_repo()
    try:
        cap = bl.capture(root, spec_id="hash-all")
        expected = {"test_price.py", "test_cart.py", "src/test_helper.py"}
        got = set(cap["tests"])
        missing = expected - got
        if missing:
            fails.append("baseline missed test files: %s" % sorted(missing))
        extra = got - expected
        if extra:
            fails.append("baseline hashed non-test files: %s" % sorted(extra))
        for rel, entry in cap["tests"].items():
            if not entry.get("raw") or not entry.get("norm"):
                fails.append("%s: incomplete hash entry" % rel)
        if not cap["all"]:
            fails.append("no aggregate signature")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_conftest_is_a_baseline_test():
    """conftest.py задаёт поведение тестов; его переписывание ломает их
    ровно так же, как переписывание ассерта."""
    fails = []
    root = make_repo({"conftest.py": "def pytest_configure():\n    pass\n"})
    try:
        cap = bl.capture(root)
        if "conftest.py" not in cap["tests"]:
            fails.append("conftest.py is not in the baseline")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


# --------------------------------------------------------------------------
# the worker cannot read the LEDGER
# --------------------------------------------------------------------------

def test_worker_cannot_read_the_ledger():
    """Не «мы не дали команду прочитать», а «пути из worktree нет».

    Единственная защита, которая держится сама: LEDGER вне дерева
    проекта и вне worktree. Иначе воркер подготавливает картину под
    ревью — ровно то, от чего барьер существует."""
    fails = []
    root = make_repo()
    try:
        with Env() as env:
            result = bl.create_barrier(root, spec_id="secrecy")
            if not result.get("worker_may_start"):
                fails.append("barrier failed on a clean tree: %s"
                             % result.get("reasons"))
                return fails
            ledger = result["ledger_path"]
            wt = result["worktree"]
            if not wt:
                fails.append("no worktree was created, isolation was not tested")
            if bl.is_within(ledger, root):
                fails.append("ledger %s is inside the project root" % ledger)
            if wt and bl.is_within(ledger, wt):
                fails.append("ledger %s is inside the worktree" % ledger)
            # Реальная проверка достижимости с позиции воркера.
            if wt and os.path.isdir(wt):
                probe = os.path.relpath(ledger, wt)
                if not probe.startswith(".."):
                    fails.append("ledger is reachable from the worktree "
                                 "(relpath %s)" % probe)
            if os.path.dirname(ledger) != bl.ledger_dir():
                fails.append("ledger stored outside the barrier state dir")
            if not os.path.exists(ledger):
                fails.append("ledger file does not exist")
            if env.home not in ledger:
                fails.append("ledger %s is not under HERMES_HOME" % ledger)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_ledger_inside_the_worker_root_is_refused():
    """Обход защиты: если подсунуть путь LEDGER внутрь worktree, барьер
    обязан отказаться, а не записать и пожать плечами.

    Раньше проверка подставляла HERMES_HOME внутрь проекта. Так тест
    больше не проверяет отказ - private_root() сам уводит хранилище за
    пределы репозитория, и опасная конфигурация просто не возникает.
    Теперь отказ проверяется там, где он возможен: через явный override
    HERMES_BASELINE_PRIVATE_DIR, который честно указывает внутрь root.
    """
    fails = []
    root = make_repo()
    old = os.environ.get("HERMES_BASELINE_PRIVATE_DIR")
    try:
        with Env():
            # Ставим приватное хранилище внутрь проекта — LEDGER окажется
            # достижим из worktree, который сам лежит внутри root.
            os.environ["HERMES_BASELINE_PRIVATE_DIR"] = os.path.join(
                root, "private")
            result = bl.create_barrier(root, spec_id="reachable")
            if result.get("worker_may_start"):
                fails.append("a reachable ledger was accepted")
            if result.get("status") != bl.BARRIER_LEDGER_REACHABLE:
                fails.append("status %s" % result.get("status"))
    finally:
        if old is None:
            os.environ.pop("HERMES_BASELINE_PRIVATE_DIR", None)
        else:
            os.environ["HERMES_BASELINE_PRIVATE_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_private_store_is_outside_the_repo():
    """Ключевой инвариант этого слоя: приватное хранилище - ЗА пределами
    репозитория, иначе воркер дойдёт до LEDGER парой уровней вверх.

    Проверяется на НАСТОЯЩЕМ HERMES_HOME, а не на временном: временный
    каталог тестов не лежит в git-репозитории, и repo_root_from() для
    него честно вернёт пусто - такой тест ничего бы не утверждал.

    Если HERMES_HOME не задан, группа ПРОПУСКАЕТСЯ, а не падает: иначе
    набор ломается в обычной оболочке, и «красный» перестаёт значить
    «слой сломан». Пропуск должен быть виден в выводе.
    """
    fails = []
    skipped = []
    old_home = os.environ.get("HERMES_HOME")
    old_dir = os.environ.get("HERMES_BASELINE_PRIVATE_DIR")
    os.environ.pop("HERMES_BASELINE_PRIVATE_DIR", None)
    try:
        if not old_home:
            skipped.append("HERMES_HOME is not set")
        else:
            root = bl.repo_root_from(old_home)
            if not root:
                skipped.append("real HERMES_HOME is not inside a git repo")
            else:
                priv = bl.private_root()
                if bl.is_within(priv, root):
                    fails.append("private_root %s is inside the repo %s"
                                 % (priv, root))
                # Worktree лежит внутри репозитория, поэтому относительный
                # путь от него обязан уходить вверх, а не вести внутрь.
                wt = os.path.join(root, ".worktrees", "probe")
                rel = os.path.relpath(priv, wt)
                if not rel.startswith(".."):
                    fails.append("private_root is reachable from a worktree: %s"
                                 % rel)
    finally:
        if old_dir is not None:
            os.environ["HERMES_BASELINE_PRIVATE_DIR"] = old_dir
    return fails, skipped


# --------------------------------------------------------------------------
# verification: what counts as tampering
# --------------------------------------------------------------------------

def _barrier(root: str, spec_id: str = "v") -> dict:
    """Барьер с изоляцией выключенной: изоляция проверяется отдельно, а
    здесь нужен только LEDGER, который переживёт выход из блока."""
    with Env(keep=True):
        return bl.create_barrier(root, spec_id=spec_id, isolation=False)


def test_modified_baseline_test_is_tampering():
    """Провал №1: `$1,234.50` с переписанным ассертом. Единственный
    класс, ради которого весь слой существует."""
    fails = []
    root = make_repo()
    try:
        barrier = _barrier(root)
        with open(os.path.join(root, "test_price.py"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("def test_format():\n    assert True  # worker rewrote it\n")
        v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
        if v["test_tampering"] is not True:
            fails.append("a rewritten assertion was not detected: %s" % v)
        if "test_price.py" not in v["modified"]:
            fails.append("modified list %s" % v["modified"])
        if v["status"] != bl.VERIFY_TAMPERED:
            fails.append("status %s" % v["status"])
        if bl.REASON_FILE_MODIFIED not in v["reasons"]:
            fails.append("reasons %s" % v["reasons"])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_deleted_baseline_test_is_tampering():
    """Удаление оракула ломает систему так же, как его переписывание."""
    fails = []
    root = make_repo()
    try:
        barrier = _barrier(root)
        os.remove(os.path.join(root, "test_cart.py"))
        v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
        if not v["test_tampering"]:
            fails.append("deleting a baseline test passed verification")
        if "test_cart.py" not in v["deleted"]:
            fails.append("deleted list %s" % v["deleted"])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_new_test_file_is_allowed():
    """V2 запрещал любой новый тест без критерия и ломал полезный TDD.
    Защита от подмены приходит из неизменяемости baseline, а не из
    запрета на рост числа тестов (SCHEME.md §5.5)."""
    fails = []
    root = make_repo()
    try:
        barrier = _barrier(root)
        write_repo(root, {"test_regression.py": "def test_r():\n    assert True\n"})
        v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
        if v["test_tampering"]:
            fails.append("a new test file was reported as tampering: %s"
                         % v["reasons"])
        if "test_regression.py" not in v["new_tests"]:
            fails.append("new test not listed: %s" % v["new_tests"])
        if v["status"] != bl.VERIFY_CLEAN:
            fails.append("status %s" % v["status"])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_line_ending_change_is_not_tolerated():
    """Сырой хэш — основа сравнения. Нормализованный нужен отчёту, а не
    ослаблению проверки: смена переводов строк переписывает файл, и
    «это же тот же файл» здесь ровно то, чем подмена притворяется."""
    fails = []
    root = make_repo()
    try:
        barrier = _barrier(root)
        path = os.path.join(root, "test_price.py")
        with open(path, "r", encoding="utf-8", newline="") as f:
            body = f.read()
        # Переводы строк меняет сам режим записи: тело остаётся тем же,
        # а на диске LF превращается в CRLF. Считать `body` руками нельзя
        # — иначе тест проверяет не CRLF-замену, а собственную ошибку.
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(body)
        v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
        if not v["test_tampering"]:
            fails.append("a CRLF rewrite passed as clean")
        if "test_price.py" not in v["eol_only"]:
            fails.append("eol_only %s should name the file" % v["eol_only"])
        if "test_price.py" not in v["modified"]:
            fails.append("the file must also count as modified")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_untouched_tree_verifies_clean():
    """Проверка обязана уметь проходить, иначе она не отличает подмену от
    нормальной работы и её отключат."""
    fails = []
    root = make_repo()
    try:
        barrier = _barrier(root)
        write_repo(root, {"price.py": "def fmt(n):\n    return f'{n:.2f}'\n"})
        v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
        if v["test_tampering"]:
            fails.append("ordinary source edits read as tampering: %s"
                         % v["modified"])
        if v["status"] != bl.VERIFY_CLEAN:
            fails.append("status %s" % v["status"])
        if v["signature_before"] != v["signature_now"]:
            fails.append("signature changed while no test file changed")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_unusable_ledger_proves_nothing():
    """Нет baseline ⇒ ничего не доказано. Тихий PASS здесь означал бы,
    что воркер сам себе выдал барьер."""
    fails = []
    v = bl.verify({}, "/nonexistent")
    if v["test_tampering"] is not True:
        fails.append("an empty ledger verified as clean")
    if not v["note"]:
        fails.append("no note explaining the refusal")
    bad = bl.verify({"schema": "something/else"}, "/nonexistent")
    if bad["test_tampering"] is not True:
        fails.append("a foreign schema verified as clean")
    return fails


def test_barrier_does_not_issue_verdicts():
    """Вердикт — слой 4. Барьер, который сам говорит PASS/FAIL, однажды
    окажется источником истины для самого себя."""
    fails = []
    root = make_repo()
    try:
        with Env():
            barrier = bl.create_barrier(root, spec_id="no-verdict")
            if barrier.get("gives_verdict") is not False:
                fails.append("capture claims it issues verdicts")
            if "verdict" in barrier:
                fails.append("a verdict field in the barrier result")
            gate = bl.require_barrier(barrier["ledger_path"])
            if "test_tampering" in gate:
                fails.append("the worker-side gate performs verification")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


# --------------------------------------------------------------------------
# isolation: shadow store and worktree
# --------------------------------------------------------------------------

def test_shadow_store_does_not_touch_the_project():
    """`checkpoints: true` работает через отдельные
    GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE: «nothing leaks into the user's
    project» — требование, а не пожелание."""
    fails = []
    root = make_repo()
    try:
        with Env():
            result = bl.create_barrier(root, spec_id="shadow")
            cp = result.get("checkpoint") or {}
            if not cp.get("commit"):
                fails.append("no checkpoint commit: %s" % result.get("reasons"))
            head_before = bl.head_commit(root)
            branches = bl.git(["for-each-ref", "--format=%(refname)",
                               "refs/heads"], cwd=root)[1].split()
            store = bl.store_dir(root)
            if not store.startswith(bl.home()):
                fails.append("store %s is outside HERMES_HOME" % store)
            if bl.is_within(store, root):
                fails.append("store landed inside the project")
            if bl.head_commit(root) != head_before:
                fails.append("the project HEAD moved during a snapshot")
            if len(branches) != 1:
                fails.append("snapshot created branches: %s" % branches)
            if os.path.exists(os.path.join(root, ".git", "index.lock")):
                fails.append("left an index.lock in the user's project")
            # Объект существует в теневом store и не в проекте.
            rc, _, _ = bl.git(["cat-file", "-e", "%s^{commit}" % cp["commit"]],
                              cwd=root, env=bl.shadow_env(store, root))
            if rc != 0:
                fails.append("checkpoint commit unreadable in the store")
            rc, _, _ = bl.git(["cat-file", "-e", "%s^{commit}" % cp["commit"]],
                              cwd=root)
            if rc == 0:
                fails.append("the checkpoint leaked into the project repo")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_worktree_is_created_and_isolated():
    """Изоляция дерева: правки воркера не обязаны трогать основной checkout."""
    fails = []
    root = make_repo()
    try:
        with Env():
            result = bl.create_barrier(root, spec_id="wt")
            wt = result.get("worktree")
            if not wt:
                fails.append("no worktree created")
                return fails
            if not os.path.isdir(wt):
                fails.append("worktree dir missing: %s" % wt)
                return fails
            if not wt.endswith(os.path.join(".worktrees",
                                            result["baseline_id"])):
                fails.append("worktree path is not per-baseline: %s" % wt)
            head = bl.head_commit(wt)
            if head != result["head"]:
                fails.append("worktree HEAD %s != baseline %s"
                             % (head, result["head"]))
            if ".worktrees" not in result["steps"][-1] + str(result["steps"]):
                pass  # the step text is informational
            write_repo(wt, {"test_price.py": "def test_format():\n    assert 1\n"})
            with open(os.path.join(root, "test_price.py"), encoding="utf-8") as f:
                if "assert 1\n" in f.read():
                    fails.append("worktree edits leaked into the main checkout")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_worktree_kept_only_with_unpushed_commits():
    """«Worktree не переживает выход, cleanup при закрытии, kept only when
    it has unpushed commits» (TODO слой 2 п.3). Чистый worktree удаляется."""
    fails = []
    root = make_repo()
    try:
        with Env():
            result = bl.create_barrier(root, spec_id="cleanup")
            wt = result["worktree"]
            done = bl.cleanup_worktree(root, wt, result["head"])
            if not done["removed"]:
                fails.append("a clean worktree was kept: %s" % done["reason"])
            if os.path.isdir(wt):
                fails.append("worktree dir survived cleanup")

            result2 = bl.create_barrier(root, spec_id="cleanup2")
            wt2 = result2["worktree"]
            bl.git(["commit", "--quiet", "--allow-empty", "-m", "worker work"],
                   cwd=wt2)
            kept = bl.cleanup_worktree(root, wt2, result2["head"])
            if kept["removed"]:
                fails.append("a worktree with unpushed commits was removed")
            if not kept.get("unpushed"):
                fails.append("unpushed commits were not reported")
            if not os.path.isdir(wt2):
                fails.append("worktree with work in it was destroyed")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_prune_removes_the_oldest_and_keeps_the_newest():
    """Срез берёт новейшие N. Если он вместо этого отбросит новейшие
    (как это выходит при сдвиге ref в линейной истории), откатываться
    будет не к чему — и prune сообщит об успехе."""
    fails = []
    root = make_repo()
    try:
        with Env():
            made = [bl.snapshot(root, "baseline %d" % i) for i in range(6)]
            out = bl.prune_snapshots(root, max_snapshots=5, reserve=2)
            rows = bl.snapshot_refs(root)
            if len(rows) > 3:
                fails.append("kept %d snapshots, limit not enforced (%s)"
                             % (len(rows), out))
            if not out["pruned"]:
                fails.append("nothing was pruned: %s" % out)
            live = {r["commit"] for r in rows}
            for snap in made[-3:]:
                if snap["commit"] not in live:
                    fails.append("newest snapshot %s was pruned away"
                                 % snap["commit"][:8])
            for commit in out["pruned"][:2]:
                if commit in live:
                    fails.append("%s reported pruned but is still live"
                                 % commit[:8])
            # Проверяем достижимость, а не наличие объекта: ref снят —
            # коммит стал неиспользуемым, и `gc` его удалит. Сам объект
            # лежит до сборки мусора, поэтому `cat-file -e` здесь успешен
            # и ничего не говорит о среде.
            store = bl.store_dir(root)
            env = bl.shadow_env(store, root)
            gone = out["pruned"][0]
            rc, _, _ = bl.git(["reflog", "exists", gone], cwd=root, env=env)
            if rc == 0:
                fails.append("a reflog keeps a pruned snapshot alive")
            refs = bl.git(["for-each-ref", "--format=%(objectname)",
                           bl.SNAP_REF_PREFIX], cwd=root, env=env)[1].split()
            if gone in refs:
                fails.append("a pruned snapshot still has a ref")
            if "gc" not in (out.get("space_freed") or ""):
                fails.append("the report hides when space is actually freed")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_prune_never_deletes_a_protected_snapshot():
    """Baseline и verified текущей задачи — её живое состояние. Если
    protected больше лимита, prune честно отказывается, а не выбирает
    жертву сам: выбор сделал бы кто-то другой, и тихо."""
    fails = []
    root = make_repo()
    try:
        with Env():
            snaps = [bl.snapshot(root, "b%d" % i) for i in range(4)]
            protected = [s["ref"] for s in snaps[:3]]
            out = bl.prune_snapshots(root, max_snapshots=2, reserve=0,
                                     protected_refs=protected)
            if out["pruned"]:
                fails.append("pruned protected snapshots: %s" % out)
            live = {r["ref"] for r in bl.snapshot_refs(root)}
            for ref in protected:
                if ref not in live:
                    fails.append("protected ref %s was deleted" % ref)
            if "exceed" not in (out.get("reason") or ""):
                fails.append("the refusal was not explained: %s" % out)

            # В пределах лимита protected переживает срез.
            out2 = bl.prune_snapshots(root, max_snapshots=3, reserve=0,
                                      protected_refs=protected)
            live2 = {r["ref"] for r in bl.snapshot_refs(root)}
            for ref in protected:
                if ref not in live2:
                    fails.append("protected ref %s lost in a normal prune"
                                 % ref)
            _ = out2
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_snapshot_lineage_is_recorded_as_metadata():
    """Родство нужно для диагностики: по цепочке видно, какой откат к
    какому приведёт.

    Родство хранится в поле `parent`, а не в git-родителе, и это
    требование, а не вкусовое решение: коммит с родителем остаётся
    достижимым, `gc` его не тронет, и срез перестаёт освобождать место,
    хотя рапортует об успехе."""
    fails = []
    root = make_repo()
    try:
        with Env():
            snaps = [bl.snapshot(root, "b%d" % i) for i in range(3)]
            if snaps[0]["parent"]:
                fails.append("the first snapshot already has a parent")
            for prev, cur in zip(snaps, snaps[1:]):
                if cur["parent"] != prev["commit"]:
                    fails.append("lineage broken: %s <- %s"
                                 % (cur["commit"][:8], (cur["parent"] or "")[:8]))
            distinct = {s["commit"] for s in snaps}
            if len(distinct) != 3:
                fails.append("snapshots collapsed to %d commits" % len(distinct))

            store = bl.store_dir(root)
            env = bl.shadow_env(store, root)
            for snap in snaps:
                rc, out, _ = bl.git(["log", "--format=%P", "-n", "1",
                                     snap["commit"]], cwd=root, env=env)
                if rc == 0 and out.strip():
                    fails.append("snapshot %s has a git parent (%s) - a "
                                 "referenced commit can never be pruned"
                                 % (snap["commit"][:8], out.strip()[:8]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_snapshot_order_survives_same_second_commits():
    """Несколько baseline-коммитов легко укладываются в одну секунду, а
    порядок по времени коммита при этом неустойчив: срез отбрасывал
    новейший снапшот вместо старого — то есть ломал откат именно тогда,
    когда он нужен. Порядок обязан быть состоянием на диске."""
    fails = []
    root = make_repo()
    try:
        with Env():
            made = [bl.snapshot(root, "same-second %d" % i) for i in range(5)]
            stamps = {s["commit"][:8] for s in made}
            rows = bl.snapshot_refs(root)
            order = [r["commit"] for r in rows]
            expected = [s["commit"] for s in made]
            if order != expected:
                fails.append("registry order != creation order (%s vs %s)"
                             % (order, expected))
            out = bl.prune_snapshots(root, max_snapshots=3, reserve=1)
            live = [r["commit"] for r in bl.snapshot_refs(root)]
            for snap in made[-2:]:
                if snap["commit"] not in live:
                    fails.append("newest snapshot %s was pruned"
                                 % snap["commit"][:8])
            if made[0]["commit"] in live:
                fails.append("the oldest snapshot survived the prune")
            if out["reserved"] != 1:
                fails.append("reserve not reported: %s" % out)
            _ = stamps
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_a_pruned_snapshot_really_becomes_unreferenced():
    """Срез, который удаляет ref, но оставляет коммит в `objects/`,
    освобождает ноль места. Проверяем достижимость, а не наличие ref."""
    fails = []
    root = make_repo()
    try:
        with Env():
            made = [bl.snapshot(root, "b%d" % i) for i in range(4)]
            doomed = made[0]["commit"]
            out = bl.prune_snapshots(root, max_snapshots=2, reserve=0)
            store = bl.store_dir(root)
            env = bl.shadow_env(store, root)
            rc, refs, _ = bl.git(["for-each-ref", "--format=%(objectname)",
                                  bl.SNAP_REF_PREFIX], cwd=root, env=env)
            if doomed in refs.split():
                fails.append("the pruned snapshot still has a ref")
            if not bl.snapshot_refs(root):
                fails.append("the registry lost every live snapshot")
            # Достижимость: коммит без родителя и без ref должен быть
            # недостижим, то есть `gc` его удалит.
            rc, _, _ = bl.git(["reflog", "exists", doomed], cwd=root, env=env)
            if rc == 0:
                fails.append("a reflog keeps the pruned snapshot alive")
            if "gc" not in (out.get("space_freed") or ""):
                fails.append("the report does not say when space is actually "
                             "freed: %s" % out)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_snapshot_failure_is_reported_not_swallowed():
    """Ошибка git не должна выглядеть как «задача не выполнена»: сбой
    барьера обязан быть виден как сбой барьера, с причиной в тексте.

    Снаружи сломанный store — это каталог, а не git-репозиторий:
    `ensure_store` считает, что он уже инициализирован, и коммит
    падает. Ровно этот случай и обязан быть диагностируемым."""
    fails = []
    root = make_repo()
    try:
        with Env():
            # Не-каталог на месте `git/` — самый честный из сбоев: mkdir
            # не проходит, и никакой «успешный» коммит невозможен.
            store = bl.store_dir(root)
            os.makedirs(store, exist_ok=True)
            with open(os.path.join(store, "git"), "w", encoding="utf-8") as f:
                f.write("not a directory\n")
            try:
                bl.snapshot(root, "x", "refs/checkpoints/baseline")
            except RuntimeError as exc:
                if not str(exc).strip():
                    fails.append("empty failure message")
                if "shadow" not in str(exc).lower():
                    fails.append("unhelpful failure: %s" % exc)
            else:
                fails.append("a broken shadow store still produced a commit")

            # Тот же сбой через create_barrier обязан быть отказом с
            # worker_may_start=False, а не исключением наружу.
            barrier = bl.create_barrier(root, spec_id="broken-store")
            if barrier.get("worker_may_start"):
                fails.append("a broken store still opened the barrier")
            if not barrier.get("reasons"):
                fails.append("the refusal carried no reason")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


# --------------------------------------------------------------------------
# quarantine marker (invariant 4; state machine is layer 3)
# --------------------------------------------------------------------------

def test_quarantine_marker_is_on_disk_and_survives_restart():
    """Маркер в памяти бесполезен: перезапуск сделал бы загрязнённую среду
    снова пригодной (SCHEME.md §5.3)."""
    fails = []
    with Env() as env:
        marker = bl.mark_quarantine("b" * 16, "A:/wt/abc", "deadbeef",
                                    "rollback impossible: file outside worktree")
        if marker["worktree_identity"] != "A:/wt/abc":
            fails.append("worktree identity not stored")
        if marker["baseline_identity"] != "b" * 16:
            fails.append("baseline identity not stored")
        if marker["checkpoint_identity"] != "deadbeef":
            fails.append("checkpoint identity not stored")
        if not marker["reason"]:
            fails.append("no reason")
        if not marker["timestamp"]:
            fails.append("no timestamp")
        path = bl.quarantine_path("b" * 16)
        if not os.path.exists(path):
            fails.append("no file on disk: %s" % path)

        # "Перезапуск": новый импорт модуля, тот же HERMES_HOME.
        spec2 = importlib.util.spec_from_file_location("baseline_restart",
                                                       MODULE)
        again = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(again)
        if not again.is_quarantined("b" * 16):
            fails.append("the marker did not survive a process restart")
        reread = again.read_quarantine("b" * 16)
        if reread.get("reason") != marker["reason"]:
            fails.append("reason lost across restart")
        if again.is_quarantined("c" * 16):
            fails.append("a foreign baseline id reported quarantined")
        if not again.clear_quarantine("b" * 16):
            fails.append("clear_quarantine did not remove the marker")
        if again.is_quarantined("b" * 16):
            fails.append("marker survived an explicit clear")
        _ = env
    return fails


def test_quarantine_refuses_a_traversing_id():
    """Идентичность приходит от вызывающего кода; путь к маркеру строится
    из неё, поэтому `..` здесь — дыра в файловой системе."""
    fails = []
    with Env():
        for bad in ("../escape", "a/b", "", "x" * 200, "..\\win"):
            try:
                bl.mark_quarantine(bad, "wt", "cp", "reason")
            except ValueError:
                continue
            fails.append("unsafe baseline id accepted: %r" % bad)
    return fails


def test_quarantine_is_visible_in_the_worker_gate():
    """Карантин обязан быть виден на входе: иначе «до явной очистки
    человеком» ничем не enforced."""
    fails = []
    root = make_repo()
    try:
        with Env():
            barrier = bl.create_barrier(root, spec_id="q")
            if bl.require_barrier(barrier["ledger_path"])["quarantined"]:
                fails.append("a fresh baseline reported as quarantined")
            bl.mark_quarantine(barrier["baseline_id"], barrier["worktree"],
                               (barrier["checkpoint"] or {}).get("commit", ""),
                               "WORKER_CRASH, rollback impossible")
            gate = bl.require_barrier(barrier["ledger_path"])
            if not gate["quarantined"]:
                fails.append("the gate hides an active quarantine")
            if "QUARANTINE" not in bl.render(gate):
                fails.append("the report does not show the quarantine")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


# --------------------------------------------------------------------------
# integrity of the LEDGER itself
# --------------------------------------------------------------------------

def test_ledger_seal_detects_rewriting():
    """Подпись ловит правку файла на диске. Против воркера она не
    защищает — он до файла не добирается, и это и есть настоящая защита;
    подпись нужна против тихой порчи и подмены на диске."""
    fails = []
    root = make_repo()
    try:
        with Env():
            barrier = bl.create_barrier(root, spec_id="seal", isolation=False)
            path = barrier["ledger_path"]
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["tests"]["test_price.py"]["raw"] = "0" * 64
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            loaded, reason = bl.load_ledger(path)
            if loaded:
                fails.append("a rewritten ledger verified its own seal")
            if reason != bl.REASON_LEDGER_CORRUPT:
                fails.append("reason %r" % reason)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_corrupt_or_foreign_ledger_is_refused():
    fails = []
    with Env():
        os.makedirs(bl.ledger_dir(), exist_ok=True)
        path = os.path.join(bl.ledger_dir(), "junk.ledger.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        if bl.load_ledger(path)[0]:
            fails.append("garbage loaded as a ledger")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"schema": "someone-elses/9"}, f)
        if bl.load_ledger(path)[0]:
            fails.append("a foreign schema loaded as a ledger")
        gate = bl.require_barrier(path)
        if gate["worker_may_start"]:
            fails.append("a corrupt ledger opened the barrier")
    return fails


def test_baseline_id_varies_with_time():
    """Один проект и одна спека в разные моменты — разные baseline.
    Иначе повторный прогон нашёл бы чужой baseline."""
    fails = []
    root = make_repo()
    try:
        a = bl.capture(root, spec_id="S")
        time.sleep(0.01)
        b = bl.capture(root, spec_id="S")
        if a["baseline_id"] == b["baseline_id"]:
            fails.append("two baselines shared an id")
        c = bl.capture(root, spec_id="OTHER")
        if c["baseline_id"] == a["baseline_id"]:
            fails.append("different specs shared a baseline id")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return fails


def test_broken_root_never_crashes():
    """Мусор на входе = отказ с причиной, а не трассировка: вызывающий
    код — воркер, и падение выглядит как «слой сломался»."""
    fails = []
    missing = os.path.join(tempfile.gettempdir(), "baseline-does-not-exist-9")
    for root in (missing, "", os.path.abspath(os.sep)):
        try:
            cap = bl.capture(root)
        except Exception as exc:                              # noqa: BLE001
            fails.append("capture(%r) raised %s: %s"
                         % (root, type(exc).__name__, exc))
            continue
        if cap["status"] not in (bl.BARRIER_NOT_REPO, bl.BARRIER_DIRTY,
                                 bl.BARRIER_OK):
            fails.append("capture(%r) status %s" % (root, cap["status"]))
    # Не-dict на входе обязан давать тот же отказ, а не исключение и не
    # «пустой, значит чисто»: тихий CLEAN здесь означал бы подмену,
    # прошедшую как «тестов не было».
    for bad in (None, 42, [], "text"):
        try:
            out = bl.verify(bad)
        except Exception as exc:                              # noqa: BLE001
            fails.append("verify(%r) raised %s: %s"
                         % (bad, type(exc).__name__, exc))
            continue
        if not isinstance(out, dict):
            fails.append("verify(%r) did not return a result" % (bad,))
        elif out.get("test_tampering") is not True:
            fails.append("verify(%r) did not refuse" % (bad,))
    return fails


def test_report_bytes_are_utf8():
    """Консоль здесь cp866/cp1251, читатель ждёт UTF-8. Барьер, который
    не прочитать, — барьер, который не соблюдают (тот же класс, что
    cp866-ловушка в слое 1)."""
    root = make_repo()
    try:
        with Env():
            barrier = bl.create_barrier(root, spec_id="utf8")
            write_repo(root, {"test_price.py": "def t():\n    assert True\n"})
            v = bl.verify(bl.load_ledger(barrier["ledger_path"])[0], root)
            text = bl.render(v)
            try:
                text.encode("utf-8")
            except UnicodeEncodeError as exc:
                return ["report is not UTF-8 encodable: %s" % exc]
            if "читабельность" in text:
                return ["sanity marker leaked into the report"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return []


# --------------------------------------------------------------------------
# CLI, run as a subprocess (the real production path)
# --------------------------------------------------------------------------

def _cli(args, env_home: str, cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_HOME=env_home, PYTHONIOENCODING="")
    return subprocess.run([PY, str(MODULE)] + args, capture_output=True,
                          timeout=180, env=env, cwd=cwd)


def test_cli_capture_then_verify_requires_then_shows():
    """Сквозной путь воркера: барьер → require → verify. Ни одного
    пропущенного шага, потому что каждый шаг и есть барьер."""
    fails = []
    root = make_repo()
    home = hermes_home()
    try:
        cap = _cli(["capture", "--json", "--root", root, "--spec-id", "cli"],
                   home, root)
        if cap.returncode != 0:
            fails.append("capture exited %d: %s"
                         % (cap.returncode, cap.stderr.decode("utf-8", "replace")[-200:]))
            return fails
        try:
            payload = json.loads(cap.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ["stdout is not valid UTF-8 JSON: %s" % exc]
        if not payload.get("worker_may_start"):
            fails.append("capture refused a clean tree")
        ledger = payload["ledger_path"]

        # `--json` после имени команды: если подпарсер его не знает,
        # вызывающий молча получит человекочитаемый отчёт вместо JSON,
        # и сломанный разбор уедет в лог как «тест упал».
        req = _cli(["require", "--json", "--ledger", ledger], home, root)
        if req.returncode != 0:
            fails.append("require exited %d on a written ledger" % req.returncode)
        if json.loads(req.stdout.decode("utf-8"))["worker_may_start"] is not True:
            fails.append("require blocked a valid barrier")

        missing = _cli(["require", "--ledger",
                        os.path.join(home, "nope.ledger.json")], home, root)
        if missing.returncode == 0:
            fails.append("require exited 0 without a baseline")

        write_repo(root, {"test_cart.py": "def test_total():\n    assert False\n"})
        ver = _cli(["verify", "--json", "--ledger", ledger, "--root", root],
                   home, root)
        if ver.returncode == 0:
            fails.append("verify exited 0 after a test was rewritten")
        v = json.loads(ver.stdout.decode("utf-8"))
        if v["test_tampering"] is not True:
            fails.append("test_tampering not reported")

        show = _cli(["show", "--ledger", ledger], home, root)
        if show.returncode != 0:
            fails.append("show exited %d" % show.returncode)
        # `show` — это про отладку барьера, поэтому печатает сам LEDGER,
        # а не сводку: пропали хэши — чинить придётся вслепую.
        try:
            shown = json.loads(show.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fails.append("show did not print the ledger as JSON: %s" % exc)
        else:
            if "test_price.py" not in (shown.get("tests") or {}):
                fails.append("show dropped the test hashes: %s"
                             % sorted(shown.get("tests") or {}))
            if not shown.get("ledger_digest"):
                fails.append("show dropped the seal")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    return fails


def test_cli_quarantine_round_trip():
    fails = []
    home = hermes_home()
    try:
        q = _cli(["quarantine", "--json", "--baseline-id", "d" * 16, "--worktree",
                  "A:/wt/d", "--checkpoint", "cafe", "--reason",
                  "WORKER_CRASH: rollback impossible"], home, home)
        if q.returncode != 0:
            fails.append("quarantine exited %d: %s"
                         % (q.returncode, q.stderr.decode("utf-8", "replace")[-200:]))
        marker = json.loads(q.stdout.decode("utf-8"))
        if marker["checkpoint_identity"] != "cafe":
            fails.append("checkpoint identity lost through the CLI")

        uq = _cli(["unquarantine", "--json", "--baseline-id", "d" * 16],
                  home, home)
        if uq.returncode != 0:
            fails.append("unquarantine exited %d on an existing marker")
        if json.loads(uq.stdout.decode("utf-8"))["cleared"] is not True:
            fails.append("marker not cleared")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return fails


def test_cli_refuses_a_dirty_tree_with_a_nonzero_exit():
    """Отказ обязан быть виден в коде возврата: иначе вызывающий код
    продолжит цепочку, не заметив, что барьер не пройден."""
    fails = []
    root = make_repo()
    home = hermes_home()
    try:
        write_repo(root, {"test_price.py": "def test_format():\n    assert 0\n"})
        proc = _cli(["capture", "--root", root, "--spec-id", "dirty"], home, root)
        if proc.returncode == 0:
            fails.append("capture exited 0 on a dirty tree")
        try:
            text = proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ["stdout is not valid UTF-8: %s" % exc]
        if "БАРЬЕР" in text and "test_price.py" not in text:
            fails.append("the refusal did not name the dirty path")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    return fails


# --------------------------------------------------------------------------
# suite
# --------------------------------------------------------------------------

TESTS = [
    ("barrier is mandatory before the worker (invariant 7)", test_barrier_is_required_before_the_worker),
    ("dirty tree refused, changed files must be empty", test_dirty_tree_is_refused),
    ("untracked files block too", test_untracked_files_also_block),
    ("every test file is hashed, not just criteria tests", test_ledger_hashes_every_test_not_just_criteria),
    ("conftest.py counts as a baseline test", test_conftest_is_a_baseline_test),
    ("worker cannot reach the LEDGER", test_worker_cannot_read_the_ledger),
    ("a reachable ledger path is refused", test_ledger_inside_the_worker_root_is_refused),
    ("private store is outside the repo", test_private_store_is_outside_the_repo),
    ("modified baseline test -> tampering", test_modified_baseline_test_is_tampering),
    ("deleted baseline test -> tampering", test_deleted_baseline_test_is_tampering),
    ("new test file is allowed (TDD not broken)", test_new_test_file_is_allowed),
    ("line-ending change is not tolerated", test_line_ending_change_is_not_tolerated),
    ("untouched tests verify clean", test_untouched_tree_verifies_clean),
    ("unusable ledger proves nothing", test_unusable_ledger_proves_nothing),
    ("barrier issues no verdicts (layer 4's right)", test_barrier_does_not_issue_verdicts),
    ("shadow store does not touch the project", test_shadow_store_does_not_touch_the_project),
    ("worktree is created and isolated", test_worktree_is_created_and_isolated),
    ("worktree kept only with unpushed commits", test_worktree_kept_only_with_unpushed_commits),
    ("prune removes the oldest, keeps the newest", test_prune_removes_the_oldest_and_keeps_the_newest),
    ("prune never deletes a protected snapshot", test_prune_never_deletes_a_protected_snapshot),
    ("snapshot lineage is metadata, not a git parent", test_snapshot_lineage_is_recorded_as_metadata),
    ("snapshot order survives same-second commits", test_snapshot_order_survives_same_second_commits),
    ("a pruned snapshot becomes unreferenced", test_a_pruned_snapshot_really_becomes_unreferenced),
    ("git failures are visible as barrier failures", test_snapshot_failure_is_reported_not_swallowed),
    ("quarantine marker survives restart (invariant 4)", test_quarantine_marker_is_on_disk_and_survives_restart),
    ("quarantine refuses a traversing id", test_quarantine_refuses_a_traversing_id),
    ("quarantine is visible in the worker gate", test_quarantine_is_visible_in_the_worker_gate),
    ("ledger seal detects rewriting", test_ledger_seal_detects_rewriting),
    ("corrupt/foreign ledger refused", test_corrupt_or_foreign_ledger_is_refused),
    ("baseline id varies with time and spec", test_baseline_id_varies_with_time),
    ("broken input never crashes", test_broken_root_never_crashes),
    ("report bytes are UTF-8", test_report_bytes_are_utf8),
    ("cli capture/require/verify/show end to end", test_cli_capture_then_verify_requires_then_shows),
    ("cli quarantine round trip", test_cli_quarantine_round_trip),
    ("cli exits nonzero on a dirty tree", test_cli_refuses_a_dirty_tree_with_a_nonzero_exit),
]


def main() -> int:
    if not git_ok():
        print("git is required for these tests (real repos, no fakes)")
        return 2

    failed = 0
    skipped = 0
    for label, fn in TESTS:
        try:
            fails = fn() or []
        except AssertionError as exc:
            fails = ["assertion: %s" % exc]
        except Exception as exc:                               # noqa: BLE001
            fails = ["%s: %s" % (type(exc).__name__, exc)]
        # Группа может вернуть (fails, skipped): пропуск - это не падение,
        # но он обязан быть виден, иначе «тихо зелёный» набор врёт.
        notes = []
        if isinstance(fails, tuple):
            fails, notes = list(fails[0]), list(fails[1] or [])
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        elif notes:
            skipped += 1
            print("skip  %s (%s)" % (label, "; ".join(notes)))
        else:
            print("pass  %s" % label)

    print()
    if skipped:
        print("%d group(s) skipped" % skipped)
    if failed:
        print("%d/%d groups failed" % (failed, len(TESTS)))
        return 1
    print("all pass (%d groups)" % (len(TESTS) - skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
