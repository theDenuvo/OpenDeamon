#!/usr/bin/env python
"""baseline — слой 2 «baseline и барьер» схемы делегации v3.

Эталон: `SCHEME.md` §3 (схема конвейера, блок 1), §5.3-§5.4, §7
(доверительная цепочка), §8 (инвариант 7), §10; `TODO.md` Фаза 2-ter,
слой 2. Слой 1 живёт в `functions/specgate/spec_gate.py`, слои 3-7 —
отдельные модули; здесь их нет намеренно.

Что делает
----------
Один обязательный барьер между одобренной спекой и воркером. Он
выполняется **до** запуска воркера и состоит из четырёх шагов
(TODO.md, слой 2, пп. 1-4):

1. **Грязное дерево = отказ.** `git status --porcelain` обязан быть пуст.
   Перечень изменённых файлов на baseline — `empty`, иначе baseline
   описывает смесь чужой работы с работой воркера, а сравнение хэшей
   потом невозможно: «что изменилось» нечем будет считать.
2. **Хэш всех существующих тестов → LEDGER.** Не только тестов из
   критериев: все файлы, которые ведут себя как тест, плюс
   `conftest.py`. Именно они образуют oracle, который нельзя переписать.
3. **Изоляция.** Теневой git-store рядом с baseline-коммитом
   (`GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`, теневой store под
   `$HERMES_HOME/checkpoints/`, «nothing leaks into the user's project»)
   плюс `git worktree` для дерева воркера.
4. **Воркер не имеет доступа к LEDGER.** LEDGER лежит вне дерева
   проекта и вне worktree, и барьер **отказывается** работать, если
   заданный путь оказался достижим из корня воркера. Не «мы не дали
   воркеру команду прочитать», а «пути не существует».

Границы, которые модуль держит механически
-----------------------------------------
1. **Без baseline воркер не стартует.** `require_barrier()` не умеет
   «пропустить по-хорошему»: отсутствующий или несогласованный
   LEDGER — это `BARRIER_MISSING`, а не предупреждение. Инвариант №7
   («baseline создаётся ДО воркера, это барьер») проверяется тестом
   `test_barrier_is_required_before_the_worker`.
2. **Сравнение хэшей живёт здесь, вердикт — в слое 4.** `verify()`
   возвращает `test_tampering: bool` и список расхождений. Право
   превратить это в `FAIL` и в `TEST_TAMPERING` — у механики; барьер
   не решает, что делать с расхождением.
3. **Новый тест — не преступление.** Рост числа тестов не запрещён
   (V2 запрещал, и это ломало полезный TDD, SCHEME.md §5.5). Барьер
   фиксирует новые файлы как `NEW_TESTS` и не трогает их. Защита от
   подмены приходит из неизменяемости baseline, а не из запрета на
   рост. Тест `test_new_test_file_is_allowed`.
4. **Удаление baseline-теста = подмена.** Исчезновение файла из
   baseline — это `TAMPERED`, а не «тест убрали, всё хорошо»: удаление
   оракула и переписывание ассерта ломают систему одинаково.
5. **Хэш строгий, но диагностируемый.** В LEDGER хранятся два хэша:
   сырой (по байтам) и нормализованный (BOM + CRLF). Сравнение идёт по
   сырому — иначе смена переводов строк была бы незаметной подменой.
   Нормализованный хэш пригодится в отчёте: он отличает «переписали
   ассерт» от «файл пересохранили в другом редакторе». Инвариант
   проверяется тестом `test_line_ending_change_is_not_tolerated`.
6. **Маркер карантина — на диске** и переживает перезапуск процесса:
   worktree identity, baseline/checkpoint identity, reason, timestamp
   (инвариант №4). Саму state machine слоя 3 здесь нет — здесь только
   носитель маркера.

Резерв места в store
--------------------
`max_snapshots: 20` при 20 задачах в день — впритык (SCHEME.md §5.4).
Поэтому место под verified-коммит резервируется **явно**:
`prune_snapshots()` держит `max_snapshots - reserve` обычных снапшотов
и не трогает `reserve` новейших, отведённых под verified-коммиты слоя 4.
Надеяться на то, что срез их не съест, нельзя: срез берёт новейшие N, а
verified-коммит может оказаться не самым новым.

Ограничения, признанные, а не замаскированные
---------------------------------------------
* **Worktree ≠ sandbox.** Изоляция защищает дерево проекта. Файлы вне
  него, фоновые процессы, внешние API — вне контура (SCHEME.md §9).
* **Хэшируются файлы, а не смысл.** Файл `test_x.py`, спрятанный за
  rename в `helper.py`, формально перестанет быть тестом. Такой класс
  подмены ловит рецензент (слой 5) и чтение диффа, не барьер.
* **Вспомогательные модули тестов вне `tests/` не покрыты.** Хэш
  берётся по перечислению git, а не по графу импортов: модуль-хелпер
  в корне репозитория формально не тест. Расширить набор — одна
  правка `TEST_PATTERNS`.
* **Права на файл LEDGER — 0600, а на Windows это `icacls`-обвязка.**
  На NTFS модуль выставляет ACL через `icacls`, если она есть, и
  честно сообщает, что не смог. Права — не главная защита; главная —
  то, что путь LEDGER недостижим из worktree воркера.

Интерфейс
---------
CLI: ``py baseline.py capture [--root R] [--spec-id ID]`` |
``verify --ledger PATH`` | ``show --ledger PATH`` | ``cleanup`` |
``quarantine``/``unquarantine`` | ``--json``
Библиотека: ``baseline.create_barrier()`` / ``baseline.require_barrier()`` /
``baseline.verify()``

Читает:  дерево проекта (git), существующий LEDGER по пути.
Пишет:   `$HERMES_HOME/state/baseline/<id>.ledger.json`,
         `$HERMES_HOME/checkpoints/<slug>/` (теневой store),
         `$HERMES_HOME/state/baseline/quarantine/<id>.json`,
         worktree в `<root>/.worktrees/<id>`.

Сети нет: модуль ничего не отправляет наружу.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LEDGER_SCHEMA = "opendeamon.baseline/1"

# Статусы барьера.
BARRIER_OK = "OK"
BARRIER_MISSING = "BARRIER_MISSING"          # нет LEDGER → воркер не стартует
BARRIER_DIRTY = "BARRIER_DIRTY"              # дерево не пусто
BARRIER_NOT_REPO = "BARRIER_NOT_REPO"        # не git-репозиторий
BARRIER_LEDGER_REACHABLE = "BARRIER_LEDGER_REACHABLE"   # воркер дотянется

# Статусы сверки (слой 4 превращает их в вердикт).
VERIFY_CLEAN = "CLEAN"
VERIFY_TAMPERED = "TAMPERED"

REASON_TREE_DIRTY = "tree_dirty"
REASON_NOT_REPO = "not_a_repo"
REASON_LEDGER_ABSENT = "ledger_absent"
REASON_LEDGER_UNREADABLE = "ledger_unreadable"
REASON_LEDGER_CORRUPT = "ledger_corrupt"
REASON_LEDGER_IN_WORKTREE = "ledger_inside_worker_root"
REASON_FILE_MODIFIED = "baseline_test_modified"
REASON_FILE_DELETED = "baseline_test_deleted"
REASON_ONLY_EOL = "line_endings_only"

# Что считается тестом. Список расширений покрывает то, что этот
# проект и типовые воркеры пишут; всё, что не перечислено, остаётся
# обычным кодом и в baseline не попадает.
TEST_PATTERNS = (
    "test_*.py", "*_test.py", "conftest.py",
    "test_*.go", "*_test.go",
    "test_*.js", "test_*.jsx", "test_*.ts", "test_*.tsx",
    "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx",
    "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx",
    "test_*.rb", "*_spec.rb",
    "*Test.java", "*Tests.cs", "*_test.rs",
)

# Каталоги, которые не перечисляются вообще: VCS-метаданные, изоляция
# воркера, кэши и чужие экосистемы. Совпадает со списком исключений
# теневого store в SCHEME.md §5.4 плюс обычный мусор.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".worktrees",
    "node_modules", ".venv", "venv", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "target", ".gradle",
})

GIT_TIMEOUT = 120

# Резерв под verified-коммиты слоя 4. Не «надеяться на срез»: срез
# берёт новейшие N, и verified может оказаться не самым новым.
DEFAULT_MAX_SNAPSHOTS = 20
DEFAULT_RESERVED = 4

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(args, cwd: str | None = None, env: dict | None = None) -> tuple[int, str, str]:
    """Запустить git и вернуть (rc, stdout, stderr). Никогда не бросает.

    Обёртка существует ради одного свойства: молчащий git с кодом 0 —
    это хуже, чем явная ошибка, поэтому stdout/stderr всегда возвращаются
    вызывающему, а исключение не проглатывается наружу."""
    full = dict(os.environ)
    if env:
        full.update(env)
    # Теневой store коммитит от фиксированного автора: иначе коммит
    # падает на машине без user.name, и барьер отказывает по причине,
    # не связанной с задачей.
    full.setdefault("GIT_AUTHOR_NAME", "opendeamon-baseline")
    full.setdefault("GIT_AUTHOR_EMAIL", "baseline@opendeamon.local")
    full.setdefault("GIT_COMMITTER_NAME", full["GIT_AUTHOR_NAME"])
    full.setdefault("GIT_COMMITTER_EMAIL", full["GIT_AUTHOR_EMAIL"])
    full.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd, env=full,
                              capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def repo_root(path: str) -> str:
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd=path)
    if rc != 0:
        return ""
    return out.strip()


def head_commit(root: str) -> str:
    rc, out, _ = git(["rev-parse", "HEAD"], cwd=root)
    return out.strip() if rc == 0 else ""


def dirty_files(root: str) -> list[str]:
    """Список изменённых файлов относительно HEAD, включая untracked.

    Именно этот список на baseline обязан быть пустым (TODO.md, слой 2,
    п.4). Считается один раз, до хэширования."""
    rc, out, _ = git(["status", "--porcelain", "--untracked-files=all"], cwd=root)
    if rc != 0:
        return ["<git status failed>"]
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Формат porcelain: XY<space>path, у переименований "old -> new".
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        files.append(entry.split(" -> ")[-1])
    return sorted(files)


def list_files(root: str) -> list[str]:
    """Все файлы рабочего дерева с учётом .gitignore.

    `git ls-files` вместо обхода каталогов: правила игнора уже написаны
    проектом, дублировать их — значит разойтись с ними."""
    collected: list[str] = []
    seen: set[str] = set()
    for args in (["ls-files", "-z"],
                 ["ls-files", "-z", "--others", "--exclude-standard"]):
        rc, out, _ = git(args, cwd=root)
        if rc != 0:
            return []
        for name in out.split("\0"):
            name = name.strip()
            if not name or name in seen:
                continue
            first = name.replace("\\", "/").split("/", 1)[0]
            if first in SKIP_DIRS:
                continue
            seen.add(name)
            collected.append(name)
    return sorted(collected)


# --------------------------------------------------------------------------
# хэши
# --------------------------------------------------------------------------

def _normalize(data: bytes) -> bytes:
    """BOM снять, CRLF привести к LF. Только для диагностического
    (нормализованного) хэша — сравнение идёт по сырому."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n")


def hash_file(path: str) -> tuple[str, str, int]:
    """(сырой sha256, нормализованный sha256, размер) файла.

    Сырой хэш — основа сравнения. Нормализованный существует, чтобы
    отчёт мог отличить переписанный ассерт от пересохранения файла
    редактором, и при этом не ослаблял саму проверку."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "", "", -1
    return (hashlib.sha256(data).hexdigest(),
            hashlib.sha256(_normalize(data)).hexdigest(),
            len(data))


def is_test_file(rel: str) -> bool:
    """Файл ведёт себя как тест? Сравнение по имени файла."""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in TEST_PATTERNS)


# --------------------------------------------------------------------------
# пути
# --------------------------------------------------------------------------

def home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def repo_root_from(start: str) -> str:
    """Ближайший каталог с .git, вверх по дереву от start."""
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent


def private_root() -> str:
    """Каталог приватного хранилища - ЗА пределами репозитория.

    Почему не внутри. Воркер запускается в worktree, а worktree лежит
    внутри репозитория (.worktrees/<id>), поэтому любой каталог внутри
    репозитория достижим из него - либо напрямую, либо парой уровней
    вверх. Проверено на живой машине: hermes-home под git (351 файл), и
    её runtime-каталог state/ поэтому оказывается в зоне досягаемости
    воркера.

    'Вне репозитория' - единственная позиция, которая даёт настоящую
    недостижимость. Переопределяется переменной
    HERMES_BASELINE_PRIVATE_DIR, что нужно тестам.
    """
    override = os.environ.get("HERMES_BASELINE_PRIVATE_DIR")
    if override:
        return os.path.abspath(override)
    root = repo_root_from(home())
    if not root:
        # Не git-репозиторий - тогда домашний каталог и есть граница.
        return os.path.join(os.path.abspath(home()), "state")
    # Соседняя папка: "A:\\OpenDeamon" -> "A:\\OpenDeamon.private"
    return os.path.abspath(root) + ".private"


def ledger_dir() -> str:
    return os.path.join(private_root(), "baseline")


def quarantine_dir() -> str:
    return os.path.join(ledger_dir(), "quarantine")


def store_dir(root: str) -> str:
    """Теневой store - ЗА пределами репозитория, рядом с приватным хранилищем.

    Раньше store жил в `$HERMES_HOME/checkpoints/<slug>`, и это давало две
    проблемы. Первая: hermes-home под git, поэтому store становился частью
    рабочего дерева - 785 неотслеживаемых объектов, а после `git add -A`
    ещё и четыре отслеживаемых файла попали в репозиторий. Теневой store
    не имеет права быть содержимым проекта.

    Вторая, важнее: store - ровно то, что воркер не должен видеть или
    подменять. Пока он лежит внутри репозитория, он достижим из worktree.

    Слаг от абсолютного пути проекта: два проекта с одинаковым именем
    не должны делить один store."""
    resolved = os.path.abspath(root).replace("\\", "/")
    slug = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]", "", Path(resolved).name) or "project"
    return os.path.join(private_root(), "checkpoints", "%s-%s" % (name, slug))


def worktree_dir(root: str, baseline_id: str) -> str:
    return os.path.join(root, ".worktrees", baseline_id)


def is_within(child: str, parent: str) -> bool:
    """`child` внутри `parent` (или совпадает). Регистр и разделители
    нормализованы: на Windows `A:\\Proj` и `a:/proj` — один путь, и
    сравнение строк без `normcase` дало бы воркеру лишний обход."""
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
    except (OSError, ValueError):
        return False
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def _harden(path: str) -> bool:
    """По возможности снять права на чтение для всех, кроме владельца.

    Возвращает False, если ограничить не удалось. Права — не главная
    защита (главная — недостижимость пути из worktree), поэтому молча
    не притворяемся, что защита сработала."""
    ok = True
    try:
        os.chmod(path, 0o600)
    except OSError:
        ok = False
    # Дальше только NTFS. На других файловых системах chmod достаточно.
    if os.name != "nt":
        return ok
    icacls = None
    for base in (os.environ.get("SystemRoot", r"C:\Windows"),):
        candidate = os.path.join(base, "System32", "icacls.exe")
        if os.path.exists(candidate):
            icacls = candidate
            break
    if not icacls:
        return False
    user = os.environ.get("USERNAME")
    for args in ([icacls, path, "/inheritance:r", "/grant:r",
                  "%s:(R,W)" % user] if user else
                 [icacls, path, "/inheritance:r", "/grant:r", "%s:(R,W)"
                  % _current_user_sid()]):
        rc, _, _ = _run_quiet(args)
        if rc != 0:
            ok = False
    return ok


def _current_user_sid() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:                                        # noqa: BLE001
        return "*S-1-1-0"


def _run_quiet(args) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(list(args), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# хэширование дерева
# --------------------------------------------------------------------------

def hash_tree(root: str) -> dict:
    """Снимок хэшей всех baseline-тестов.

    Возвращает `{rel: {raw, norm, size}}` плюс `all` — хэш по
    отсортированным парам (путь, сырой хэш). `all` не заменяет
    сравнение пофайлово: он нужен как дешёвая подпись снимка в отчёте
    и как защита от подмены самого LEDGER."""
    files: dict[str, dict] = {}
    for rel in list_files(root):
        if not is_test_file(rel):
            continue
        raw, norm, size = hash_file(os.path.join(root, rel))
        if not raw:
            continue
        files[rel] = {"raw": raw, "norm": norm, "size": size}

    signature = hashlib.sha256()
    for rel in sorted(files):
        signature.update(("%s\0%s\n" % (rel, files[rel]["raw"])).encode("utf-8"))
    return {"files": files, "all": signature.hexdigest()}


def _canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=False, indent=2)


def seal(body: dict) -> dict:
    """Добавить самоподпись LEDGER.

    Подпись ловит случайную порчу и правку файла на диске. Против
    воркера она не защищает — воркер до файла не добирается, иначе
    барьер не был бы создан (это и есть настоящая защита)."""
    body = dict(body)
    body.pop("ledger_digest", None)
    body["ledger_digest"] = hashlib.sha256(
        _canonical(body).encode("utf-8")).hexdigest()
    return body


def verify_seal(ledger: dict) -> bool:
    claimed = ledger.get("ledger_digest")
    if not isinstance(claimed, str) or not claimed:
        return False
    body = {k: v for k, v in ledger.items() if k != "ledger_digest"}
    return hashlib.sha256(
        _canonical(body).encode("utf-8")).hexdigest() == claimed


# --------------------------------------------------------------------------
# теневой store (SCHEME.md §5.4)
# --------------------------------------------------------------------------

def ensure_store(root: str) -> str:
    """Теневой store с отдельными `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`.

    Ничего не пишется в `.git` пользовательского проекта: `checkpoints:
    true` в Hermes работает ровно так, и «nothing leaks into the user's
    project» — требование, а не пожелание."""
    store = store_dir(root)
    git_dir = os.path.join(store, "git")
    os.makedirs(store, exist_ok=True)
    if not os.path.isdir(os.path.join(git_dir, "objects")):
        rc, _, err = git(["init", "--quiet", "--bare", git_dir])
        if rc != 0:
            raise RuntimeError("cannot init shadow store %s: %s"
                               % (git_dir, err.strip()[:200]))
        if not os.path.isdir(os.path.join(git_dir, "objects")):
            # Каталог создали, репозитория нет (например, на месте `git/`
            # лежит одноимённый файл). Молчаливый успех означал бы, что
            # барьер «записал» коммит, которого не существует, и
            # откатываться было бы не к чему.
            raise RuntimeError("shadow store %s is not a git repository "
                               "after init" % git_dir)
    return store


def shadow_env(store: str, root: str) -> dict:
    return {
        "GIT_DIR": os.path.join(store, "git"),
        "GIT_WORK_TREE": os.path.abspath(root),
        "GIT_INDEX_FILE": os.path.join(store, "index"),
    }


SNAP_REF_PREFIX = "refs/checkpoints/snap/"

# Именованный ref на последний снапшот. Держится ради читаемости
# (`git log refs/checkpoints/baseline`) и для совместимости с тем, как
# Hermes показывает checkpoints. На срез он не влияет: снимается вместе
# со своими снапшотами, иначе старые коммиты остались бы достижимыми.
CHAIN_REF = "refs/checkpoints/baseline"


def snapshot(root: str, message: str, ref: str = "") -> dict:
    """Коммит текущего дерева в теневой store.

    Это baseline-точка отката: слой 3 откатывает к ней, а слой 4 кладёт
    рядом verified-коммит. Идентичность возвращается наружу — именно
    она попадает в маркер карантина.

    Каждый снапшот получает **свой** ref (`refs/checkpoints/snap/<sha>`) и
    является **независимым коммитом** — без git-родителя. Обе решения
    следуют из одного требования: срез обязан реально освобождать место.

    * Один ref на конец цепочки: ref указывает на tip, поэтому сдвиг
      «оставить новейшие N» назад отбрасывает новейшие коммиты вместо
      старых, а сами старые остаются в истории целиком.
    * Родитель в коммите: пока у коммита есть родитель, он достижим, и
      `gc` его не тронет. Удаление ref не освобождает место — prune
      отрапортует об успехе, а диск продолжит расти.

    Откату всё это не мешает: коммит с полным деревом и есть точка
    отката, истории для этого не нужно. Родство остаётся в метаданных
    (`parent` в возврате и в LEDGER) — этого хватает, чтобы по цепочке
    понять, какой откат к какому приведёт."""
    store = ensure_store(root)
    env = shadow_env(store, root)
    rc, _, err = git(["add", "-A", "--", "."], cwd=root, env=env)
    if rc != 0:
        raise RuntimeError("shadow add failed: %s" % err.strip()[:200])
    rc, tree, err = git(["write-tree"], cwd=root, env=env)
    if rc != 0:
        raise RuntimeError("shadow write-tree failed: %s" % err.strip()[:200])
    tree = tree.strip()

    # Родитель — метаданные, а не аргумент `-p`. См. docstring: с
    # настоящим родителем старый снапшот остаётся достижимым и срез
    # перестаёт освобождать место.
    parent = _read_tip(store)
    rc, commit, err = git(["commit-tree", tree, "-m", message],
                          cwd=root, env=env)
    if rc != 0:
        raise RuntimeError("shadow commit-tree failed: %s" % err.strip()[:200])
    commit = commit.strip()

    snap_ref = ref or (SNAP_REF_PREFIX + commit[:16])
    git(["update-ref", snap_ref, commit], cwd=root, env=env)
    git(["update-ref", CHAIN_REF, commit], cwd=root, env=env)
    _append_registry(store, snap_ref, commit)
    _write_tip(store, commit)
    return {"store": store, "ref": snap_ref, "chain_ref": CHAIN_REF,
            "commit": commit, "tree": tree, "parent": parent,
            "message": message}


def _tip_file(store: str) -> str:
    return os.path.join(store, "tip")


def _registry_file(store: str) -> str:
    return os.path.join(store, "snapshots.json")


def _append_registry(store: str, ref: str, commit: str) -> None:
    """Порядок снапшотов хранится на диске, а не выводится из времени
    коммита.

    Причина конкретная: `%(committerdate:unix)` имеет секундную
    точность, а несколько baseline-коммитов вполне укладываются в одну
    секунду (и укладываются — замерено). Порядок по такому времени
    неустойчив, и срез при случае отбрасывал новейший снапшот вместо
    старого, то есть ломал откат в тот самый момент, когда он нужен."""
    rows = _read_registry(store)
    if any(r["ref"] == ref for r in rows):
        return
    rows.append({"ref": ref, "commit": commit})
    try:
        with open(_registry_file(store), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _write_registry(store: str, refs: list[str]) -> None:
    try:
        with open(_registry_file(store), "w", encoding="utf-8") as f:
            json.dump([{"ref": r} for r in refs], f,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass


def _read_registry(store: str) -> list[dict]:
    try:
        with open(_registry_file(store), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if (isinstance(row, dict) and isinstance(row.get("ref"), str)
                and isinstance(row.get("commit"), str)):
            clean.append({"ref": row["ref"], "commit": row["commit"]})
    return clean


def _read_tip(store: str) -> str:
    try:
        with open(_tip_file(store), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_tip(store: str, commit: str) -> None:
    try:
        with open(_tip_file(store), "w", encoding="utf-8") as f:
            f.write(commit + "\n")
    except OSError:
        # Без tip-файла следующий снапшот просто станет корневым
        # коммитом. Это потеря родства, а не потеря точки отката: сам
        # коммит и его ref на месте.
        pass


def snapshot_refs(root: str) -> list[dict]:
    """Все снапшоты в store, старые первыми.

    Порядок берётся из реестра на диске, а не из времени коммита: у
    времени коммита секундная точность, и несколько снапшотов в одну
    секунду делают порядок неустойчивым (замерено — срез при этом
    отбрасывал новейший снапшот). Реестр — единственный источник
    порядка; refs, которых в нём нет, добавляются в конец, чтобы
    посторонний (например, сделанный Hermes) снапшот не потерялся."""
    store = ensure_store(root)
    env = shadow_env(store, root)
    rc, out, _ = git(["for-each-ref", "--format=%(refname) %(objectname)",
                      SNAP_REF_PREFIX], cwd=root, env=env)
    if rc != 0:
        return []
    actual: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            actual[parts[0]] = parts[1]

    rows = [{"ref": r["ref"], "commit": actual.get(r["ref"], r["commit"])}
            for r in _read_registry(store) if r["ref"] in actual]
    known = {r["ref"] for r in rows}
    for ref in sorted(actual):
        if ref not in known:
            rows.append({"ref": ref, "commit": actual[ref]})
    return rows


def prune_snapshots(root: str, max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
                    reserve: int = DEFAULT_RESERVED,
                    protected_refs: list | None = None) -> dict:
    """Срез старых снапшотов с явным резервом под verified.

    `max_snapshots: 20` при 20 задачах в день — впритык (SCHEME.md §5.4),
    поэтому место под verified-коммит резервируется числом, а не верой в
    то, что срез его не съест: срез берёт новейшие, а verified может
    оказаться не самым новым.

    `protected_refs` — снапшоты текущей задачи (её baseline и verified).
    Они не удаляются никогда: если protected больше лимита, prune
    сообщает об этом и не удаляет ничего, а не выбирает жертву сам."""
    keep_total = max(1, int(max_snapshots))
    reserved = max(0, min(int(reserve), keep_total - 1))
    keep_ordinary = keep_total - reserved

    store = ensure_store(root)
    env = shadow_env(store, root)
    rows = snapshot_refs(root)
    protected = set(protected_refs or ())
    live = [r for r in rows if r["ref"] not in protected]
    pinned = [r for r in rows if r["ref"] in protected]

    if len(pinned) > keep_total:
        return {"pruned": [], "kept": len(rows), "reserved": reserved,
                "total": len(rows), "protected": len(pinned),
                "reason": "protected snapshots exceed max_snapshots: nothing "
                          "pruned rather than deleting a live task state"}
    if len(live) <= keep_ordinary:
        return {"pruned": [], "kept": len(rows), "reserved": reserved,
                "total": len(rows), "protected": len(pinned),
                "reason": "within limits"}

    doomed = live[:len(live) - keep_ordinary]
    for row in doomed:
        git(["update-ref", "-d", row["ref"]], cwd=root, env=env)
    _write_registry(store, [r["ref"] for r in rows
                            if r["ref"] not in {d["ref"] for d in doomed}])

    # Снятый ref делает коммит недостижимым, но объект в `objects/` лежит
    # до `gc`. Отсюда и вывод в отчёт: место освободится при сборке
    # мусора, а не мгновенно. Притворяться иначе нельзя — иначе «срез
    # выполнен» будет означать «срез ничего не освободил».
    remaining = len(rows) - len(doomed)
    if remaining:
        _write_tip(store, rows[-1]["commit"])
        git(["update-ref", CHAIN_REF, rows[-1]["commit"]], cwd=root, env=env)
    else:
        try:
            os.remove(_tip_file(store))
        except OSError:
            pass
        git(["update-ref", "-d", CHAIN_REF], cwd=root, env=env)
    return {"pruned": [r["commit"] for r in doomed], "kept": remaining,
            "reserved": reserved, "total": len(rows),
            "protected": len(pinned),
            "space_freed": "on git gc (unreferenced objects are not removed "
                           "by update-ref alone)",
            "tip": rows[-1]["commit"] if rows else ""}


# --------------------------------------------------------------------------
# worktree (SCHEME.md §3 блок 1, TODO.md слой 2 п.3)
# --------------------------------------------------------------------------

def create_worktree(root: str, path: str, commit: str) -> dict:
    """Изолированное дерево для воркера.

    Изоляция защищает дерево проекта, но не файлы вне контура — это
    ограничение признано в SCHEME.md §9, а не подаётся как sandbox."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isdir(path):
        return {"path": path, "created": False,
                "reason": "worktree path already exists"}
    rc, _, err = git(["worktree", "add", "--detach", path, commit], cwd=root)
    if rc != 0:
        raise RuntimeError("git worktree add failed: %s" % err.strip()[:200])
    return {"path": path, "created": True, "commit": commit}


def worktree_unpushed(worktree: str, base: str) -> list[str]:
    """Коммиты worktree, которых нет в базовой ветке.

    Worktree не переживает выход, но чистку надо делать по правилу:
    «kept only when it has unpushed commits». Пустой список означает,
    что держать нечего."""
    if not os.path.isdir(worktree):
        return []
    rc, out, _ = git(["log", "--format=%H %s", "%s..HEAD" % base], cwd=worktree)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def cleanup_worktree(root: str, worktree: str, base: str) -> dict:
    """Убрать worktree, если в нём нет непушенных коммитов."""
    unpushed = worktree_unpushed(worktree, base)
    if unpushed:
        return {"removed": False, "kept": True, "unpushed": unpushed,
                "reason": "kept: has unpushed commits"}
    if os.path.isdir(worktree):
        rc, _, err = git(["worktree", "remove", "--force", worktree], cwd=root)
        if rc != 0:
            return {"removed": False, "kept": True, "unpushed": [],
                    "reason": "remove failed: %s" % err.strip()[:200]}
    git(["worktree", "prune"], cwd=root)
    return {"removed": True, "kept": False, "unpushed": [],
            "reason": "removed: clean and no unpushed commits"}


# --------------------------------------------------------------------------
# маркер карантина (инвариант №4; state machine — слой 3)
# --------------------------------------------------------------------------

def quarantine_path(baseline_id: str) -> str:
    return os.path.join(quarantine_dir(), "%s.json" % baseline_id)


def mark_quarantine(baseline_id: str, worktree_identity: str,
                    checkpoint_identity: str, reason: str) -> dict:
    """Карантин на диске, четыре поля, переживает перезапуск.

    Маркер в памяти бесполезен: перезапуск процесса сделал бы
    загрязнённую среду снова пригодной. Именно поэтому он на диске и
    именно поэтому четыре поля — устаревший маркер невозможно
    диагностировать (SCHEME.md §5.3)."""
    if not _SAFE_ID.match(baseline_id or ""):
        raise ValueError("unsafe baseline id: %r" % (baseline_id,))
    marker = {
        "schema": LEDGER_SCHEMA,
        "worktree_identity": worktree_identity or "",
        "baseline_identity": baseline_id,
        "checkpoint_identity": checkpoint_identity or "",
        "reason": reason or "",
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = quarantine_path(baseline_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_canonical(marker))
    _harden(path)
    return marker


def read_quarantine(baseline_id: str) -> dict:
    """Прочитать маркер. Отсутствие маркера — не ошибка чтения."""
    try:
        with open(quarantine_path(baseline_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_quarantined(baseline_id: str) -> bool:
    return bool(read_quarantine(baseline_id))


def clear_quarantine(baseline_id: str) -> bool:
    """Снять карантин. Только явным решением человека — в API нет
    флага «снять молча», а вызывающий код это человек."""
    path = quarantine_path(baseline_id)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# барьер
# --------------------------------------------------------------------------

def make_baseline_id(root: str, spec_id: str, created: float) -> str:
    """Идентичность baseline: проект + спека + момент.

    Один и тот же проект на одной спеке в разные моменты обязан дать
    разные id, иначе повторный прогон найдёт чужой baseline и сравнение
    станет бессмысленным."""
    material = "%s\0%s\0%.6f" % (os.path.abspath(root), spec_id, created)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def capture(root: str, spec_id: str = "", baseline_id: str = "") -> dict:
    """Снимок дерева: тесты + тождество. Ничего не пишет сама.

    Порядок именно такой: сначала проверка грязного дерева, потом
    хэширование. Посчитанные на грязном дереве хэши описывают смесь
    чужой работы и работы воркера, и «что изменилось» потом нечем
    будет считать."""
    root = os.path.abspath(root or ".")
    created = time.time()
    if not repo_root(root):
        return {"status": BARRIER_NOT_REPO, "reason": REASON_NOT_REPO,
                "root": root, "spec_id": spec_id, "baseline_id": "",
                "tests": {}, "all": "", "created": created,
                "dirty": [], "reasons": ["%s: %s" % (root, "not a git repo")]}

    changed = dirty_files(root)
    bid = baseline_id or make_baseline_id(root, spec_id, created)
    base = {
        "schema": LEDGER_SCHEMA,
        "baseline_id": bid,
        "root": root,
        "spec_id": spec_id,
        "created": created,
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "head": head_commit(root),
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"],
                      cwd=root)[1].strip(),
        "tests": {},
        "all": "",
        "changed_at_baseline": changed,
    }
    if changed:
        base.update({"status": BARRIER_DIRTY, "reason": REASON_TREE_DIRTY,
                     "reasons": ["tree is not empty at baseline: %d path(s)"
                                 % len(changed)],
                     "dirty": changed})
        return base

    tree = hash_tree(root)
    base["tests"] = tree["files"]
    base["all"] = tree["all"]
    base["test_count"] = len(tree["files"])
    base["dirty"] = []
    base["status"] = BARRIER_OK
    base["reason"] = ""
    base["reasons"] = []
    return base


def create_barrier(root: str, spec_id: str = "", isolation: bool = True,
                   baseline_id: str = "") -> dict:
    """Обязательный барьер перед воркером.

    Четыре шага TODO.md слоя 2 по порядку: пустое дерево → хэши в
    LEDGER → изоляция (теневой store + worktree) → проверка, что
    LEDGER воркеру недоступен. Пятым шагом, который тоже обязателен,
    идёт запись самого LEDGER: барьер, который ничего не сохранил,
    не работает — он просто потратил время."""
    root = os.path.abspath(root or ".")
    base = capture(root, spec_id=spec_id, baseline_id=baseline_id)
    result = dict(base)
    result["ledger_path"] = ""
    result["worktree"] = ""
    result["checkpoint"] = {}

    if base["status"] != BARRIER_OK:
        result["worker_may_start"] = False
        result["steps"] = ["tree_check: FAIL (%s)" % base["reason"]]
        return result

    steps = ["tree_check: ok, changed files empty"]
    tests, total = base["tests"], len(base["tests"])

    # 3. Изоляция. Сначала теневой store: он не зависит от worktree и
    # обязан выжить, даже если создание worktree не удалось.
    try:
        snap = snapshot(root, "baseline %s (%s)" % (base["baseline_id"],
                                                    spec_id or "no-spec"))
        result["checkpoint"] = {"commit": snap["commit"], "ref": snap["ref"],
                                "store": snap["store"]}
        steps.append("shadow_snapshot: %s" % snap["commit"][:12])
    except (RuntimeError, OSError) as exc:
        result["status"] = BARRIER_DIRTY
        result["reason"] = REASON_TREE_DIRTY
        result["reasons"] = ["shadow store failed: %s" % exc]
        result["worker_may_start"] = False
        result["steps"] = steps + ["shadow_snapshot: FAIL"]
        return result

    if isolation and base["head"]:
        wt_path = worktree_dir(root, base["baseline_id"])
        try:
            info = create_worktree(root, wt_path, base["head"])
            result["worktree"] = wt_path
            steps.append("worktree: %s" % ("created" if info["created"]
                                            else info.get("reason")))
        except (RuntimeError, OSError) as exc:
            result["status"] = BARRIER_DIRTY
            result["reason"] = REASON_TREE_DIRTY
            result["reasons"] = ["worktree failed: %s" % exc]
            result["worker_may_start"] = False
            result["steps"] = steps + ["worktree: FAIL"]
            return result
    else:
        steps.append("worktree: skipped (isolation off or no HEAD)")

    # 4. LEDGER в недостижимое для воркера место.
    path = os.path.join(ledger_dir(), "%s.ledger.json" % base["baseline_id"])
    # Воркер не в песочнице: он читает всё, что доступно процессу. Поэтому
    # недостижимость должна быть настоящей, а не «по дереву воркера».
    #
    # Сверять надо и против root, и против worktree. Worktree лежит ВНУТРИ
    # репозитория (.worktrees/<id>), поэтому воркер доходит до ledger
    # через два уровня вверх. Проверка только против worktree была бы
    # наивной: барьер позеленел бы при реально доступном ledger.
    #
    # Соответственно ledger обязан лежать за пределом репозитория - это
    # и делает ledger_dir() (см. private_root()).
    reachable = [where for where in (root, result.get("worktree"))
                 if where and is_within(path, where)]
    if reachable:
        result["status"] = BARRIER_LEDGER_REACHABLE
        result["reason"] = REASON_LEDGER_IN_WORKTREE
        result["reasons"] = [
            "ledger path is inside the worker root (%s) - the worker could "
            "read it and prepare the picture for review"
            % ", ".join(reachable)]
        result["worker_may_start"] = False
        result["steps"] = steps + ["ledger: FAIL (reachable by worker)"]
        return result
    steps.append("ledger: outside worker root")

    os.makedirs(ledger_dir(), exist_ok=True)
    payload = dict(base)
    payload["worktree"] = result["worktree"]
    payload["checkpoint"] = result["checkpoint"]
    payload["ledger_path"] = path
    sealed = seal(payload)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_canonical(sealed))
    except OSError as exc:
        result["status"] = BARRIER_DIRTY
        result["reason"] = REASON_TREE_DIRTY
        result["reasons"] = ["cannot write ledger: %s" % exc]
        result["worker_may_start"] = False
        result["steps"] = steps + ["ledger: FAIL (write)"]
        return result
    hardened = _harden(path)
    steps.append("ledger: written (%d test files, digest %s)"
                 % (total, sealed["ledger_digest"][:12]))
    if not hardened:
        steps.append("ledger: rights NOT hardened on this filesystem - "
                     "rely on the path being unreachable from the worktree")

    result.update(sealed)
    result["ledger_path"] = path
    result["ledger_hardened"] = hardened
    result["worker_may_start"] = True
    result["steps"] = steps
    result["can_verify_worker_result"] = True
    result["gives_verdict"] = False
    return result


def load_ledger(path: str) -> tuple[dict, str]:
    """(LEDGER, причина отсутствия). Пустой dict означает «читать нечем»."""
    if not path or not os.path.exists(path):
        return {}, REASON_LEDGER_ABSENT
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}, REASON_LEDGER_UNREADABLE
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        return {}, REASON_LEDGER_CORRUPT
    if not verify_seal(data):
        return {}, REASON_LEDGER_CORRUPT
    return data, ""


def require_barrier(ledger_path: str) -> dict:
    """Единственная точка входа для воркера.

    Всегда возвращает структуру с `worker_may_start`; исключение не
    бросается, потому что вызывающий код — это воркер, и падение с
    трассировкой выглядит как «слой сломался», а не «воркеру нельзя».
    Отсутствие барьера — `BARRIER_MISSING`, а не предупреждение."""
    ledger, reason = load_ledger(ledger_path)
    if reason:
        return {"status": BARRIER_MISSING, "reason": reason,
                "worker_may_start": False, "baseline_id": "",
                "ledger_path": ledger_path or "",
                "reasons": ["baseline is mandatory and must exist BEFORE the "
                            "worker (%s)" % reason],
                "steps": [], "quarantined": False}
    quarantined = is_quarantined(ledger.get("baseline_id", ""))
    worktree = ledger.get("worktree") or ""
    if worktree and is_within(ledger_path, worktree):
        return {"status": BARRIER_LEDGER_REACHABLE,
                "reason": REASON_LEDGER_IN_WORKTREE, "worker_may_start": False,
                "baseline_id": ledger.get("baseline_id", ""),
                "ledger_path": ledger_path,
                "reasons": ["ledger is inside the worktree"],
                "steps": [], "quarantined": quarantined}
    return {"status": BARRIER_OK, "reason": "", "worker_may_start": True,
            "baseline_id": ledger.get("baseline_id", ""),
            "ledger_path": ledger_path, "reasons": [], "steps": [],
            "quarantined": quarantined, "worktree": worktree,
            "checkpoint": ledger.get("checkpoint") or {},
            "test_count": ledger.get("test_count", 0)}


# --------------------------------------------------------------------------
# сверка (слой 4 превращает это в вердикт)
# --------------------------------------------------------------------------

def verify(ledger: dict, root: str = "") -> dict:
    """Сравнить текущие хэши с baseline.

    Возвращает `test_tampering: bool` и расхождения. Право объявить
    `FAIL` и поставить `TEST_TAMPERING` — у механики слоя 4; барьер
    только считает. Новые тесты попадают в `new_tests` и нарушением не
    считаются: рост числа тестов разрешён (SCHEME.md §5.5)."""
    if not isinstance(ledger, dict) or ledger.get("schema") != LEDGER_SCHEMA:
        return {"status": VERIFY_TAMPERED, "test_tampering": True,
                "modified": [], "deleted": [], "new_tests": [],
                "eol_only": [], "unreadable": [],
                "reasons": [REASON_LEDGER_CORRUPT],
                "baseline_id": "", "checked": 0,
                "note": "unusable ledger: nothing can be proven unchanged"}

    root = os.path.abspath(root or ledger.get("root") or ".")
    baseline_tests = ledger.get("tests") or {}
    current = hash_tree(root)

    modified, deleted, eol_only, unreadable = [], [], [], []
    for rel in sorted(baseline_tests):
        was = baseline_tests[rel]
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            deleted.append(rel)
            continue
        raw, norm, size = hash_file(path)
        if not raw:
            unreadable.append(rel)
            continue
        if raw == was["raw"]:
            continue
        # Сырые хэши разошлись. Если нормализованные совпали, отличие
        # только в переводах строк — но и это подмена: файл переписан.
        # Такие помечаем отдельно, чтобы отчёт отличал пересохранение
        # редактором от переписанного ассерта. Нарушение — в обоих
        # случаях одинаковое.
        if norm == was.get("norm"):
            eol_only.append(rel)
        modified.append(rel)

    new_tests = [rel for rel in sorted(current["files"])
                 if rel not in baseline_tests]

    tampering = bool(modified or deleted or unreadable)
    reasons = []
    if modified:
        reasons.append(REASON_FILE_MODIFIED)
    if deleted:
        reasons.append(REASON_FILE_DELETED)
    if unreadable:
        reasons.append("baseline_test_unreadable")

    return {
        "status": VERIFY_TAMPERED if tampering else VERIFY_CLEAN,
        "test_tampering": tampering,
        "baseline_id": ledger.get("baseline_id", ""),
        "root": root,
        "checked": len(baseline_tests),
        "modified": modified,
        "deleted": deleted,
        "new_tests": new_tests,
        "eol_only": eol_only,
        "unreadable": unreadable,
        "signature_before": ledger.get("all", ""),
        "signature_now": current["all"],
        "reasons": reasons,
        "note": ("new test files are not a violation; only existing "
                 "baseline tests are immutable"),
    }


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------

def render(result: dict) -> str:
    lines = ["BASELINE %s" % result.get("status", "?")]
    if result.get("baseline_id"):
        lines.append("baseline id: %s" % result["baseline_id"])
    if result.get("spec_id"):
        lines.append("spec: %s" % result["spec_id"])
    if result.get("root"):
        lines.append("root: %s" % result["root"])
    if result.get("test_count"):
        lines.append("baseline tests: %d" % result["test_count"])
    if result.get("all"):
        lines.append("test signature: %s" % result["all"][:16])
    if result.get("head"):
        lines.append("head: %s" % result["head"][:12])
    if result.get("worktree"):
        lines.append("worktree: %s" % result["worktree"])
    if (result.get("checkpoint") or {}).get("commit"):
        lines.append("checkpoint: %s" % result["checkpoint"]["commit"][:12])
    if result.get("quarantined"):
        lines.append("QUARANTINE: this baseline is terminally blocked")

    for step in result.get("steps") or []:
        lines.append("  step: %s" % step)
    for rel in (result.get("modified") or []):
        lines.append("! MODIFIED baseline test: %s" % rel)
    for rel in (result.get("deleted") or []):
        lines.append("! DELETED baseline test: %s" % rel)
    for rel in (result.get("unreadable") or []):
        lines.append("! UNREADABLE baseline test: %s" % rel)
    if result.get("new_tests"):
        lines.append("new tests (allowed, reviewer classifies): %s"
                     % ", ".join(result["new_tests"]))
    for reason in result.get("reasons") or []:
        lines.append("! %s" % reason)

    if "test_tampering" in result:
        lines.append("=> test_tampering: %s (слой 4 ставит вердикт)"
                     % result["test_tampering"])
    if "worker_may_start" in result:
        lines.append("=> worker may start: %s" % result["worker_may_start"]
                     if result["worker_may_start"]
                     else "=> воркер НЕ стартует: барьер не пройден")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _reconfigure_stdout() -> None:
    # Отчёт по-русски, консоль на этой машине cp866/cp1251, а читатель
    # ожидает UTF-8. Барьер, который не прочитать, — это барьер,
    # который не соблюдают.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _resolve_root(args) -> str:
    return os.path.abspath(getattr(args, "root", None) or os.getcwd())


def main(argv=None) -> int:
    _reconfigure_stdout()
    # `--json` живёт и на главном парсере, и на каждом подпарсере:
    # иначе `baseline.py capture --json` падает, а писать его перед
    # именем команды невозможно запомнить всем вызывающим.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS, а не store_true: иначе значение, заданное до имени
    # команды, затирается дефолтом подпарсера. С `--json capture` и
    # `capture --json` результат обязан быть одинаковым.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable result on stdout")

    parser = argparse.ArgumentParser(
        description="baseline barrier: hashes and isolation BEFORE the worker.")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable result on stdout")
    sub = parser.add_subparsers(dest="cmd")

    p_cap = sub.add_parser("capture", parents=[common],
                           help="run the barrier and write the LEDGER")
    p_cap.add_argument("--root", default=None)
    p_cap.add_argument("--spec-id", default="")
    p_cap.add_argument("--baseline-id", default="")
    p_cap.add_argument("--no-isolation", action="store_true",
                       help="skip the worktree (tests only; not for real runs)")

    p_req = sub.add_parser("require", parents=[common],
                           help="worker-side gate: may I start?")
    p_req.add_argument("--ledger", required=True)

    p_ver = sub.add_parser("verify", parents=[common],
                           help="compare current hashes with baseline")
    p_ver.add_argument("--ledger", required=True)
    p_ver.add_argument("--root", default=None)

    p_show = sub.add_parser("show", parents=[common],
                            help="print the LEDGER contents")
    p_show.add_argument("--ledger", required=True)

    p_cln = sub.add_parser("cleanup", parents=[common],
                           help="remove a worktree with no unpushed commits")
    p_cln.add_argument("--root", default=None)
    p_cln.add_argument("--worktree", required=True)
    p_cln.add_argument("--base", default="HEAD")

    p_prune = sub.add_parser("prune", parents=[common],
                             help="prune snapshots, reserving verified slots")
    p_prune.add_argument("--root", default=None)
    p_prune.add_argument("--max-snapshots", type=int, default=DEFAULT_MAX_SNAPSHOTS)
    p_prune.add_argument("--reserve", type=int, default=DEFAULT_RESERVED)
    p_prune.add_argument("--protect", action="append", default=[],
                         metavar="REF",
                         help="ref a prune must never delete (repeatable)")

    p_q = sub.add_parser("quarantine", parents=[common],
                         help="write the on-disk quarantine marker")
    p_q.add_argument("--baseline-id", required=True)
    p_q.add_argument("--worktree", default="")
    p_q.add_argument("--checkpoint", default="")
    p_q.add_argument("--reason", required=True)

    p_uq = sub.add_parser("unquarantine", parents=[common],
                          help="lift the marker (human decision)")
    p_uq.add_argument("--baseline-id", required=True)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2

    result: dict
    code = 0
    if args.cmd == "capture":
        result = create_barrier(_resolve_root(args), spec_id=args.spec_id,
                                isolation=not args.no_isolation,
                                baseline_id=args.baseline_id)
        code = 0 if result.get("worker_may_start") else 1
    elif args.cmd == "require":
        result = require_barrier(args.ledger)
        code = 0 if result.get("worker_may_start") else 1
    elif args.cmd == "verify":
        ledger, reason = load_ledger(args.ledger)
        if reason:
            result = {"status": VERIFY_TAMPERED, "test_tampering": True,
                      "reasons": [reason], "modified": [], "deleted": [],
                      "new_tests": [], "eol_only": [], "unreadable": [],
                      "note": "no usable baseline: everything is unproven"}
            code = 1
        else:
            result = verify(ledger, _resolve_root(args))
            code = 1 if result["test_tampering"] else 0
    elif args.cmd == "show":
        # Всегда JSON, даже без --json: `show` существует для чтения
        # хэшей глазами при разборе инцидента, и сводка из двух строк
        # там бесполезна.
        ledger, reason = load_ledger(args.ledger)
        result = ledger if not reason else {"error": reason, "ledger": args.ledger}
        code = 0 if not reason else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    elif args.cmd == "cleanup":
        result = cleanup_worktree(_resolve_root(args), args.worktree, args.base)
        code = 0 if result.get("removed") else 1
    elif args.cmd == "prune":
        result = prune_snapshots(_resolve_root(args), args.max_snapshots,
                                 args.reserve, args.protect)
    elif args.cmd == "quarantine":
        result = mark_quarantine(args.baseline_id, args.worktree,
                                 args.checkpoint, args.reason)
    else:  # unquarantine
        result = {"baseline_id": args.baseline_id,
                  "cleared": clear_quarantine(args.baseline_id)}
        code = 0 if result["cleared"] else 1

    # `getattr`, а не `args.json`: --json объявлен с SUPPRESS в подпарсере,
    # поэтому без него атрибута просто нет, и `args.json` упал бы на
    # ровном месте — то есть CLI без флага был бы нерабочим.
    print(json.dumps(result, ensure_ascii=False, indent=2)
          if getattr(args, "json", False) else render(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
