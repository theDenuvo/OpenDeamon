#!/usr/bin/env python
"""worker_state — слой 3 «state machine воркера» схемы делегации v3.

Эталон: `SCHEME.md` §4 (блок 3), §5.3 (`RECOVERY_BLOCKED` — терминальный
карантин), §6 (состояния и типизация фейлов), §8 (инварианты 3, 4, 7),
§10 («когда сомневаешься»); `TODO.md` Фаза 2-ter, слой 3 и его DoD.
Слой 1 живёт в `functions/specgate/spec_gate.py`, слой 2 — в
`functions/baseline/baseline.py`. Здесь только слой 3: что происходит с
воркером между стартом и вердиктом слоя 4.

Состояния
---------
    NOT_STARTED → RUNNING → COMPLETED
                           → FAILED_CLEAN   (упал до правок, откат не нужен)
                           → FAILED_DIRTY   (упал после правок)
    FAILED_DIRTY → ROLLING_BACK → BASELINE_RESTORED  → можно rework
                                    → RECOVERY_BLOCKED → карантин, только человек
    FAILED_CLEAN  → BASELINE_RESTORED / RUNNING     (откат не нужен, но проверка нужна)

`ROLLING_BACK` — отдельное состояние, а не деталь реализации: откат
можно прервать (процесс убили, диск кончился), и состояние на диске
обязано сказать «откат был начат и не доведён», а не «воркер упал».
Resume из этого состояния разрешён.

Что модуль держит механически
-----------------------------
1. **Откат делает код, не модель.** `restore()` работает с теневым
   store через git: `read-tree` + `checkout-index` возвращают все
   baseline-файлы и перезаписывают изменённые, лишние удаляются.
   В LLM не делегируется ничего — `/rollback` это слотовая команда, а
   не агентский тул (SCHEME.md §5.3).
2. **`CLEAN`/`DIRTY` измеряется, а не объявляется.** Классификация
   сравнивает worktree с деревом baseline-коммита хэшами blob-объектов.
   Воркер не может сказать «я упал до правок», даже если Very Much
   захочет: у него нет способа повлиять на измерение.
3. **`RECOVERY_BLOCKED` терминален.** Из него нет переходов вообще:
   ни нового worker run, ни rework, ни reuse этого worktree. Маркер
   лежит на диске (инвариант №4), и `start()` читает его **до** всего
   остального — перезапуск процесса не делает загрязнённую среду снова
   пригодной.
4. **`WORKER_CRASH` не является поводом дать воркеру ещё раз.** Rework
   после краша требует явного разрешения человека (`--human-authorized`)
   и причины разрешения, которые пишутся в историю состояния. Здесь
   важна сверка двух мест эталонной схемы: §5.3 говорит «после
   успешного отката можно rework» (то есть машина не блокирует
   физическую возможность), а §6/§10 говорят «`WORKER_CRASH` — всегда
   эскалация» (то есть автоматического повтора быть не должно). Оба
   выполняются: машина разрешает новую попытку, но только по решению
   человека, и это решение видно в истории. Тот же гейт у
   `WORKER_CLAIM_CONFLICT` — воркер рапортовал об успехе, оставив дерево
   изменённым, то есть проверял тест под свой вывод; повтор такого
   поведения вслепую повторяет его.
5. **`WORKER_CLAIM` — с наименьшим доверием.** Хранится отдельно от
   фактов (`exit_code`, измеренный residue) и помечен: зелёный вывод
   воркера ничего не доказывает (SCHEME.md §7).

Правила, которые воркер обязан соблюдать (TODO.md, врезка «правила»)
----------------------------------------------------------------------
* **Две попытки на модель.** Мёртвый эндпоинт держит соединение
  75-81 с и только потом отдаёт ошибку, а живые модели при этом давали
  91.4 с и проходили. Одна неудача — флак. Поэтому `attempts_per_model`
  по умолчанию 2, и ступень лестницы снимается только после двух
  неудач подряд.
* **Только `opencode/*`.** Замер 2026-09-30: `opencode/ling-3.0-flash-fin-free`
  мертва независимо от VPN (77 и 81 с, `Endpoint is unavailable`), и
  `openrouter/*` внутри opencode CLI тянет общий дневной бакет
  OpenRouter. Закон $0 проверяется до запуска, а не по счёту.
* **Промпт файлом НЕЛЬЗЯ.** `opencode run -f FILE` — это вложение, а
  не сообщение (NOTES.md, «Грабли инструментов»): промпт уходит в
  Attach, ран висит без вывода. Здесь argv собирается списком, без
  PowerShell.
* **Медиана на 5 прогонах, не один замер** (правило измерения
  моделей). Каждая попытка пишет свою длительность в историю — из
  этих чисел медиана считается потом, а не по одному прогону.

Ограничения, признанные, а не замаскированные
----------------------------------------------
* **Worktree ≠ sandbox** (SCHEME.md §9). Откат возвращает дерево
  worktree и ничего больше. Файл вне worktree, занятый файл,
  внешний артефакт, ресурс за пределами snapshot scope — ровно те
  четыре причины, по которым откат может не сработать, и поэтому
  карантин терминален. Модуль не делает вид, что покрывает их: он
  их обнаруживает и уходит в `RECOVERY_BLOCKED`.
* **⚠️ Два ответа на вопрос «тронут ли тест», и они не взаимозаменяемы.**
  На этой машине `core.autocrlf = true`, слой 2 снимает хэши с корня
  проекта (LF), а `git worktree add` кладёт в worktree CRLF. Поэтому
  `baseline.verify(ledger, worktree)` на НЕТРОНУТОМ дереве показывает
  `test_tampering` с `eol_only` — верно по байтам, ложно по смыслу.
  Из этого следуют два обязательства, и слой 4 обязан их учесть:
  1. `baseline_tests_intact` из `classify()`/`finish()` — сравнение по
     СОДЕРЖИМОМУ внутри worktree; это про то, менял ли воркер оракул.
  2. `baseline.verify()` — строгий сырой хэш для корня проекта; это
     про подмену, и он остаётся обязательным.
  Ослаблять (2) ради удобства (1) нельзя: тогда правка теста,
  сохранившая длину строк, станет невидимой.
* **Исход snapshot считается полным.** Точка отката — полное дерево
  коммита baseline. Если бы снимок был частичным, «остаток» после
  отката был бы нормой, а не признаком карантина.
* **Gitignored-артефакты не трогаются.** `node_modules/`, `cache/` и
  прочее вне `git ls-files` не считаются остатком: иначе откат
  удалял бы то, что создал не воркер. Проверка остатка идёт по
  правилам ignore самого проекта.
* **Кто вызвал слой 3 — человек или ядро — не важно.** Модуль не
  различает: карантин снимается только явным вызовом `unblock` с
  причиной, и это решение попадает в историю.

Интерфейс
---------
CLI: ``py worker_state.py run --ledger PATH --task-file F`` |
``start`` | ``fail`` | ``classify`` | ``rollback`` | ``rework`` |
``block`` | ``unblock`` | ``state`` | ``transitions`` | ``--json``
Библиотека: ``worker_state.run()`` / ``.classify()`` / ``.restore()``

Читает:  LEDGER слоя 2, дерево worktree, теневой store.
Пишет:   `<private>/state/<id>.state.json`,
         `<private>/state/logs/<id>-<run>.log`,
         `<private>/baseline/quarantine/<id>.json` (маркер — слой 2).

Сети нет по умолчанию: слой 3 сам в сеть не ходит, он только запускает
воркер по правилам лестницы. Preflight рельсов (проверка VPN перед
ключами) — отдельная опция и по умолчанию выключена.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str, filename: str):
    """Загрузить соседний модуль по файлу.

    Импорт по имени не годится: пакеты лежат рядом (`functions/*`) и
    не установлены, а `sys.path` в этой машине указывает на кучу
    чужих каталогов. Слой 3 обязан работать из любого cwd."""
    import importlib.util

    path = os.path.join(_HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bl = _load("baseline", os.path.join("..", "baseline", "baseline.py"))

SCHEMA = "opendeamon.workerstate/1"

# --------------------------------------------------------------------------
# состояния
# --------------------------------------------------------------------------

NOT_STARTED = "NOT_STARTED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED_CLEAN = "FAILED_CLEAN"
FAILED_DIRTY = "FAILED_DIRTY"
ROLLING_BACK = "ROLLING_BACK"
BASELINE_RESTORED = "BASELINE_RESTORED"
RECOVERY_BLOCKED = "RECOVERY_BLOCKED"

STATES = (NOT_STARTED, RUNNING, COMPLETED, FAILED_CLEAN, FAILED_DIRTY,
          ROLLING_BACK, BASELINE_RESTORED, RECOVERY_BLOCKED)

# Единственный источник истины по переходам. Таблица не «описание
# поведения», а контракт: `transition()` проверяет по ней, а тест
# сверяет с ней же. Всё, чего тут нет, недостижимо.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    NOT_STARTED: (RUNNING,),
    # Из RUNNING выход в любой исход: успех, упал до правок, упал после.
    # Куда именно — вычисляет код по измерению, а не вызывающий.
    RUNNING: (COMPLETED, FAILED_CLEAN, FAILED_DIRTY),
    # Откат из FAILED_CLEAN не нужен по смыслу, но разрешён: он же
    # является независимой проверкой того, что «чисто» не соврало.
    # Расхождение двух измерений — сам по себе признак карантина.
    FAILED_CLEAN: (ROLLING_BACK, BASELINE_RESTORED, RUNNING),
    FAILED_DIRTY: (ROLLING_BACK,),
    # Повторный вход разрешён: процесс мог умереть посреди отката.
    ROLLING_BACK: (ROLLING_BACK, BASELINE_RESTORED, RECOVERY_BLOCKED),
    # Rework после успешного отката разрешён машиной, но не автоматически:
    # если последний фейл был WORKER_CRASH, нужен явный человек (см. docstring).
    BASELINE_RESTORED: (RUNNING,),
    COMPLETED: (),
    # Терминально. Ничего. Ни rework, ни повтор, ни reuse worktree.
    RECOVERY_BLOCKED: (),
}

TERMINAL = frozenset({COMPLETED, RECOVERY_BLOCKED})
# Состояния, из которых воркер запускаться может.
STARTABLE = frozenset({NOT_STARTED, FAILED_CLEAN, BASELINE_RESTORED})

# --------------------------------------------------------------------------
# причины (все, что модуль различает, названы явно)
# --------------------------------------------------------------------------

REASON_WORKER_OK = "worker_exited_zero"
REASON_WORKER_NONZERO = "worker_exit_nonzero"
REASON_WORKER_TIMEOUT = "worker_timeout"
REASON_WORKER_NOT_LAUNCHED = "worker_not_launched"
REASON_TREE_RESIDUE = "tree_residue_after_restore"
REASON_OUTSIDE_WORKTREE = "residue_outside_worktree"
REASON_FILE_LOCKED = "file_locked_by_process"
REASON_NO_CHECKPOINT = "no_baseline_checkpoint"
REASON_WORKTREE_MISSING = "worktree_missing"
REASON_SNAPSHOT_SELF = "snapshot_contains_worktree"
REASON_GIT_FAILED = "git_operation_failed"
REASON_CLEAN_CONTRADICTED = "clean_classification_contradicted"
REASON_STATE_UNREADABLE = "state_unreadable"

# Тип фейла по §6: последствия разные, и именно они решают, можно ли
# повторять. `WORKER_CRASH` — это FAILED_DIRTY, то есть упал ПОСЛЕ
# правок; повтор того же промта такое воспроизводит.
FAILURE_CRASH = "WORKER_CRASH"
# Отдельный случай: воркер отчитался об успехе, а дерево изменено. Это
# не краш (ничего не упало) и не «чистое» падение, но повторять его
# вслепую нельзя по той же причине — worker сначала «чинит» тест, потом
# рапортует. Тип назван отдельно, чтобы история не врала о том, что
# произошло, но гейт перед повтором у него тот же.
FAILURE_CONFLICT = "WORKER_CLAIM_CONFLICT"
# Упал, ничего не тронув. Обычный фейл, rework положен.
FAILURE_ABORTED = "WORKER_ABORTED"

# Повтор после этих двух требует явного человека. Третий — нет.
HUMAN_GATE_KINDS = frozenset({FAILURE_CRASH, FAILURE_CONFLICT})

# --------------------------------------------------------------------------
# воркер: лестница и правила запуска
# --------------------------------------------------------------------------

# Лестница из замеров 2026-09-30 (`SCHEME.md` §3, `SKILL.md`
# opencode-worker). Порядок рабочий, заменять не нужно: `space-bunny-free`
# дал 6 вызовов подряд 6 из 6.
LADDER = (
    "opencode/space-bunny-free",
    "opencode/muse-spark-1.3-contributor-free",
    "opencode/longcat-2.5-preview-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/mimo-v2.6-flash-free",
    "opencode/big-pickle",
)

# Мертва независимо от VPN: 77 и 81 с, `Endpoint is unavailable`.
DEAD_MODELS = frozenset({"opencode/ling-3.0-flash-fin-free"})

# Правило двух попыток. Одна неудача — флак, а не приговор: мёртвый
# эндпоинт держит соединение 75-81 с, а живые модели при этом давали
# 91.4 с и проходили.
ATTEMPTS_PER_MODEL = 2

DEFAULT_TIMEOUT = 1800

# `openrouter/*` внутри opencode CLI тянет общий дневной бакет
# OpenRouter, то есть это единственный способ нарушить закон $0 из
# этого модуля. Проверяется до запуска, а не постфактум по счёту.
FREE_PREFIX = "opencode/"


def free_route_ok(model: str) -> tuple[bool, str]:
    """Модель воркера не может стоить денег. Проверка до запуска."""
    m = (model or "").strip()
    if not m:
        return False, "empty model id"
    if m in DEAD_MODELS:
        return False, ("%s is dead regardless of VPN (77-81s, "
                       "Endpoint is unavailable) — замер 2026-09-30" % m)
    if not m.startswith(FREE_PREFIX):
        return False, ("route %r is not %s*; inside the opencode CLI an "
                       "openrouter/* id draws the shared OpenRouter daily "
                       "bucket and breaks the $0 law" % (m, FREE_PREFIX))
    return True, ""


# --------------------------------------------------------------------------
# состояние на диске
# --------------------------------------------------------------------------

def state_dir() -> str:
    """Каталог состояний — рядом с приватным хранилищем слоя 2.

    В том же приватном корне и по той же причине: состояние воркера
    не должно быть содержимым проекта (иначе `git add -A` воркера
    затащит его в индекс) и не должно быть достижимо из worktree."""
    return os.path.join(bl.private_root(), "state")


def state_path(baseline_id: str) -> str:
    return os.path.join(state_dir(), "%s.state.json" % _safe_id(baseline_id))


def log_dir() -> str:
    return os.path.join(state_dir(), "logs")


def _safe_id(value: str) -> str:
    """Идентификатор, пригодный для имени файла.

    Идентификатор приходит от вызывающего кода, а имя файла собирается
    из него конкатенацией — то есть без проверки это путь обхода
    (`../../`). Отказ здесь, а не очистка: молча подменённый путь
    означал бы, что карантин лежит не там, где его потом ищут."""
    text = str(value or "").strip()
    if not re.match(r"^[A-Za-z0-9._-]{1,80}$", text) or ".." in text:
        raise ValueError("unsafe baseline id: %r" % (value,))
    return text


def new_state(ledger: dict) -> dict:
    """Начальное состояние по данным LEDGER."""
    bid = _safe_id(ledger.get("baseline_id") or "")
    checkpoint = dict(ledger.get("checkpoint") or {})
    return {
        "schema": SCHEMA,
        "baseline_id": bid,
        "root": ledger.get("root") or "",
        "worktree": ledger.get("worktree") or "",
        "checkpoint": checkpoint,
        "state": NOT_STARTED,
        "task_digest": "",
        "run": {},
        "failure": {},
        "rollback": {},
        "quarantine": {},
        "history": [{"state": NOT_STARTED, "at": time.time(),
                     "detail": {"ledger": ledger.get("ledger_path", "")}}],
        "updated": time.time(),
    }


def load_state(baseline_id: str) -> dict:
    """Прочитать состояние. Отсутствие — не ошибка чтения, а «нет
    состояния»: вернётся `{}` и вызывающий решит, что с этим делать."""
    try:
        with open(state_path(baseline_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {}
    return data


def save_state(state: dict) -> str:
    """Записать состояние. Исключение на ровном месте НЕ глотается.

    В отличие от кэша счётчика в слое 1, молчаливая потеря состояния
    здесь опасна: потерянный `RECOVERY_BLOCKED` выглядит как
    «карантина нет», а это ровно то состояние, ради которого модуль
    написан. Правильнее упасть на записи."""
    path = state_path(state["baseline_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["updated"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return path


def transition(state: dict, to: str, detail: dict | None = None) -> dict:
    """Единственная точка смены состояния.

    Бросает `IllegalTransition`, если перехода нет. Молчаливый отказ
    здесь означал бы, что состояние разъехалось с реальностью, а
    читатель увидит правдоподобный отчёт.
    """
    current = state.get("state") or NOT_STARTED
    allowed = TRANSITIONS.get(current, ())
    if to not in allowed:
        raise IllegalTransition(current, to, allowed)
    state["state"] = to
    state.setdefault("history", []).append(
        {"state": to, "from": current, "at": time.time(),
         "detail": detail or {}})
    return state


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str, allowed):
        super().__init__("%s -> %s is not a transition; allowed: %s"
                         % (current, target, ", ".join(allowed) or "nothing "
                            "(terminal)"))
        self.current, self.target, self.allowed = current, target, allowed


def can_run(state: dict) -> tuple[bool, str]:
    """Можно ли запускать воркера прямо сейчас. Проверка ДО всего."""
    if terminal_blocked(state):
        return False, (RECOVERY_BLOCKED + ": this baseline is terminally "
                      "quarantined; no worker run, no rework, no reuse of "
                      "the worktree until an explicit human release")
    current = state.get("state") or NOT_STARTED
    if current not in STARTABLE:
        if current == RUNNING:
            return False, ("a worker is already RUNNING for this baseline; "
                           "a second run would race the first one")
        return False, ("cannot start from %s; startable: %s"
                       % (current, ", ".join(sorted(STARTABLE))))
    return True, ""


def terminal_blocked(state: dict) -> bool:
    """Карантин ли это — по состоянию ИЛИ по маркеру на диске.

    Два источника, а не один: состояние может потеряться (перезапуск,
    битый файл), а маркер — нет. Маркер проверяется, когда состояние
    вообще не читается: иначе потерянный файл состояния выглядел бы
    как «карантина не было»."""
    if (state or {}).get("state") == RECOVERY_BLOCKED:
        return True
    if (state or {}).get("quarantine"):
        return True
    try:
        return bl.is_quarantined((state or {}).get("baseline_id") or "")
    except ValueError:
        # Идентификатор небезопасен — читать маркер по нему нельзя.
        # Молчать здесь нельзя: неизвестный id не значит «чисто».
        return True


# --------------------------------------------------------------------------
# измерение: чем worktree отличается от baseline
# --------------------------------------------------------------------------

def _strip_eol(body: bytes) -> bytes:
    """Переводы строк в LF, BOM снять. Только для сравнения содержимого."""
    data = body.replace(b"\r\n", b"\n")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data


def baseline_tree(commit: str, store: str) -> dict:
    """{относительный путь: oid блоба} для коммита в теневом store.

    Источник истины — сам git, а не запись в LEDGER: если коммит есть,
    дерево можно перечитать в любой момент, и подменить его «записью в
    отчёте» нельзя."""
    if not commit or not store:
        return {}
    git_dir = os.path.join(store, "git")
    if not os.path.isdir(git_dir):
        return {}
    rc, out, err = bl.git(["ls-tree", "-r", "-z", "--format=%(objectname) %(path)",
                           commit], cwd=store,
                          env={"GIT_DIR": git_dir, "GIT_WORK_TREE": store})
    if rc != 0:
        return {}
    paths = {}
    for entry in out.split("\0"):
        entry = entry.strip()
        if not entry:
            continue
        oid, _, rel = entry.partition(" ")
        paths[rel.replace("\\", "/")] = oid
    return paths


def _git_blob_oid(data: bytes) -> str:
    """Идентификатор блоба git для содержимого, без всяких git.

    Нужен, чтобы сравнивать с oid из `ls-tree` в одном представлении.
    Считать его на Python дешевле, чем запускать `git hash-object` на
    каждый файл: при четырёхстах файлах это четыреста процессов.

    Именно blob-oid, а не sha256: слой 2 считает sha256, и если
    сравнивать их друг с другом, разница будет всегда — то есть
    «eol_only» объявил бы изменённым каждый файл в дереве."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def disk_digests(worktree: str, paths: list) -> dict:
    """{путь: (нормализованный oid, нормализованный sha256)} на диске.

    Та же пара, что отдаёт `blob_digests`, поэтому сравнение идёт
    покомпонентно и без приведения типов."""
    digests = {}
    for rel in paths:
        try:
            with open(os.path.join(worktree, rel.replace("/", os.sep)),
                      "rb") as f:
                raw = f.read()
        except OSError:
            continue
        # oid — по СЫРЫМ байтам (как в дереве), нормализованный хэш —
        # по приведённым. Смешать эти два и не различить «байты те же»
        # с «переводы строк другие» невозможно, а различие здесь и есть
        # весь смысл второго списка.
        digests[rel] = (_git_blob_oid(raw),
                        hashlib.sha256(_strip_eol(raw)).hexdigest())
    return digests


def blob_digests(store: str, commit: str) -> dict:
    """{путь: (oid блоба, нормализованный sha256)} для коммита в store.

    Один `git cat-file --batch` на всё дерево, а не процесс на файл.
    Читать приходится потоком: содержимое блобов может занимать
    мегабайты, и держать его в памяти незачем — хэш считается сразу.

    Нормализация eol здесь и в `_strip_eol` обязана быть одной и той
    же, иначе сравнение «содержимое то же» никогда не сойдётся."""
    git_dir = os.path.join(store or "", "git")
    if not os.path.isdir(git_dir):
        return {}
    tree = baseline_tree(commit, store)
    if not tree:
        return {}
    env = dict(os.environ)
    env["GIT_DIR"] = git_dir
    try:
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=store, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except OSError:
        return {}
    request = "".join("%s\n" % oid for oid in tree.values()).encode("ascii")
    try:
        out, _ = proc.communicate(request, timeout=bl.GIT_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        proc.kill()
        return {}

    digests, pos = {}, 0
    for rel in tree:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].decode("utf-8", "replace").split()
        if len(header) < 3 or header[1] == "missing":
            break
        size = int(header[2])
        body = _strip_eol(out[end + 1:end + 1 + size])
        pos = end + 1 + size + 1          # blob + разделительный \n
        digests[rel] = (tree[rel], hashlib.sha256(body).hexdigest())
    return digests


def git_known_paths(worktree: str) -> set:
    """Пути, которые git считает содержимым проекта в этом worktree.

    Два списка: отслеживаемые и неотслеживаемые-но-не-игнорируемые.
    Игнорируемые (`node_modules/`, `cache/`) сюда не попадают — и это
    правильно: их создал не воркер, откат их не должен трогать, а
    остатком они не являются."""
    known: set = set()
    for args in (["ls-files", "-z"],
                 ["ls-files", "-z", "--others", "--exclude-standard"]):
        rc, out, _ = bl.git(args, cwd=worktree)
        if rc != 0:
            # Без перечисления остаток не измерить. Молча вернуть
            # пустое множество значило бы объявить дерево чистым.
            raise RuntimeError("cannot enumerate %s in %s" % (args[0], worktree))
        for name in out.split("\0"):
            if name.strip():
                known.add(name.strip().replace("\\", "/"))
    return known


def test_files(base: dict, changed: list) -> list:
    """Какая часть различий относится к тестам.

    `baseline.is_test_file` — тот же список, что использует слой 2 для
    LEDGER. Здесь он нужен для другого: чтобы у слоя 4 был честный
    ответ внутри worktree, где «сырой хэш vs LEDGER» не работает
    (см. `_canonical`). Именно тесты образуют oracle, который нельзя
    переписать, поэтому их правка — самое важное, что вообще бывает
    в дереве воркера."""
    return sorted(rel for rel in changed if bl.is_test_file(rel))


def residue(worktree: str, base: dict) -> dict:
    """Чем worktree отличается от baseline. Это и есть измерение.

    Четыре списка:
      * `missing`   — baseline-файл исчез (воркер его удалил);
      * `modified`  — содержимое другое (переписан, в том числе ассерт
                      теста);
      * `eol_only`  — байты те же с точностью до переводов строк.
                      Остатком НЕ считается (см. `_canonical`), но
                      попадает в отчёт: смену переводов строк нарочно
                      считает подменой слой 2, и замолчать об этом здесь
                      означало бы спрятать улику;
      * `extra`     — путь, известный git, но отсутствующий в baseline
                      (новый файл воркера).

    `base` — результат `blob_digests()`, а не `baseline_tree()`: нужны
    обе половины пары (oid и нормализованный хэш).
    """
    empty = {"missing": [], "modified": [], "eol_only": [], "extra": [],
             "error": ""}
    if not os.path.isdir(worktree):
        return dict(empty, error=REASON_WORKTREE_MISSING)

    known = git_known_paths(worktree)
    present = [rel for rel in base
               if os.path.isfile(os.path.join(worktree, rel.replace("/", os.sep)))]
    on_disk = disk_digests(worktree, present)
    present = [rel for rel in present if rel in on_disk]

    modified, eol_only = [], []
    for rel in present:
        oid, want = base[rel]
        disk_oid, disk_norm = on_disk[rel]
        if disk_oid == oid:
            continue                      # байты те же
        if disk_norm == want:
            # Содержимое то же, байты другие: разница только в
            # переводах строк. Для отката это не остаток, для
            # механики — подмена (слой 2 считает её намеренно).
            eol_only.append(rel)
            continue
        # Содержимое другое: переписан файл, в том числе ассерт теста.
        modified.append(rel)

    missing = sorted(rel for rel in base if rel not in on_disk)
    extra = sorted(rel for rel in known
                   if rel not in base
                   and os.path.isfile(os.path.join(
                       worktree, rel.replace("/", os.sep))))

    return {
        "missing": missing,
        "modified": sorted(modified),
        "eol_only": sorted(eol_only),
        "extra": extra,
        # Ответ на «тронул ли воркер оракул» внутри worktree. Слой 2
        # даёт свой (строгий, по байтам) для корня проекта; эти два
        # ответа нельзя смешивать, иначе либо ложный TEST_TAMPERING
        # на каждом заходе, либо дыра в античите.
        "tests_modified": test_files(base, modified + missing),
        "error": "",
    }


def is_dirty(res: dict) -> bool:
    return bool(res.get("error")) or bool(
        res.get("missing") or res.get("modified") or res.get("extra"))


# --------------------------------------------------------------------------
# откат (код, не модель)
# --------------------------------------------------------------------------

def restore(worktree: str, commit: str, store: str) -> dict:
    """Вернуть worktree к baseline-коммиту. Ни одного вызова LLM.

    Порядок операций важен и не переставляется:
      1. проверить, что откатывать вообще к чему (коммит есть, worktree
         на месте, снимок не содержит сам worktree);
      2. `read-tree` + `checkout-index -a -f` — вернуть все
         baseline-файлы и перезаписать изменённые;
      3. удалить `extra` — файлы, которых в baseline не было;
      4. **переизмерить остаток тем же кодом.** Откат, который сам не
         проверил результат, — это заявление, а не факт.
    """
    result = {"restored": False, "reason": "", "locked": [],
              "residue": {}, "root_dirty": [], "detail": ""}

    if not commit:
        result.update({"reason": REASON_NO_CHECKPOINT,
                       "detail": "baseline identity is unknown: there is "
                                 "nothing to roll back to"})
        return result
    if not os.path.isdir(worktree):
        result.update({"reason": REASON_WORKTREE_MISSING,
                       "detail": "worktree %s does not exist" % worktree})
        return result

    git_dir = os.path.join(store or "", "git")
    base = blob_digests(store, commit)
    if not base or not os.path.isdir(git_dir):
        result.update({"reason": REASON_NO_CHECKPOINT,
                       "detail": "commit %s is unreadable in store %s"
                                 % (commit[:12], store)})
        return result

    # Снимок, внутри которого лежит сам worktree, означает, что
    # baseline описывает не то дерево. Откат по нему был бы
    # самореферентным, поэтому это отказ, а не предупреждение.
    prefix = os.path.abspath(worktree).replace("\\", "/").lstrip("/")
    if any(rel == prefix or rel.startswith(prefix + "/") for rel in base):
        result.update({"reason": REASON_SNAPSHOT_SELF,
                       "detail": "baseline tree contains the worktree itself"})
        return result

    index = os.path.join(state_dir(), "rollback.index")
    os.makedirs(state_dir(), exist_ok=True)
    if os.path.exists(index):
        os.remove(index)
    env = {"GIT_DIR": git_dir, "GIT_WORK_TREE": worktree,
           "GIT_INDEX_FILE": index}

    rc, _, err = bl.git(["read-tree", commit], cwd=worktree, env=env)
    if rc != 0:
        result.update({"reason": REASON_GIT_FAILED,
                       "detail": "read-tree: %s" % err.strip()[:200]})
        return result
    rc, _, err = bl.git(["checkout-index", "-a", "-f"], cwd=worktree, env=env)
    if rc != 0:
        result.update({"reason": REASON_GIT_FAILED,
                       "detail": "checkout-index: %s" % err.strip()[:200]})
        return result

    # Шаг 3: лишнее. Порядок — сначала файлы, потом пустые каталоги
    # (каталог сам по себе git не видит, но оставить его — значит
    # оставить мусор, который следующий откат не увидит).
    before = residue(worktree, base)
    for rel in before.get("extra") or []:
        path = os.path.join(worktree, rel.replace("/", os.sep))
        try:
            os.remove(path)
        except PermissionError:
            result["locked"].append(rel)
        except OSError:
            result["locked"].append(rel)
    for rel in before.get("extra") or []:
        _prune_empty_dirs(worktree, rel)

    # Шаг 4: то же измерение, что и до отката.
    try:
        after = residue(worktree, base)
    except RuntimeError as exc:
        result.update({"reason": REASON_GIT_FAILED, "detail": str(exc)})
        return result
    result["residue"] = after
    if os.path.exists(index):
        try:
            os.remove(index)
        except OSError:
            pass

    # Кто-то мог писать мимо worktree. Snapshot scope на это не
    # смотрит, поэтому такое НЕ чинится откатом — это карантин.
    root = _root_of(worktree)
    if root:
        result["root_dirty"] = outside_residue(root, worktree)
        if result["root_dirty"]:
            result.update({"reason": REASON_OUTSIDE_WORKTREE,
                           "restored": False,
                           "detail": "the project tree itself changed; that "
                                     "is outside the worktree and outside "
                                     "the snapshot scope"})
            return result

    if result["locked"]:
        result.update({"reason": REASON_FILE_LOCKED, "restored": False,
                       "detail": "%d file(s) could not be removed"
                                 % len(result["locked"])})
        return result
    if is_dirty(after):
        result.update({"reason": REASON_TREE_RESIDUE, "restored": False,
                       "detail": "%d missing, %d modified, %d extra"
                                 % (len(after["missing"]), len(after["modified"]),
                                    len(after["extra"]))})
        return result

    result.update({"restored": True, "reason": "restored",
                   "detail": "worktree matches baseline commit %s" % commit[:12]})
    return result


def _root_of(worktree: str) -> str:
    """Корень проекта, которому принадлежит worktree.

    `<root>/.worktrees/<id>` — это форма, которую создаёт слой 2. Если
    форма другая, корень неизвестен, и проверка «писал ли кто-то мимо
    worktree» просто не выполняется — это записано в отчёте как
    `outside_scope_unchecked`, а не как «чисто»."""
    parent = os.path.dirname(os.path.abspath(worktree))
    if os.path.basename(parent) != ".worktrees":
        return ""
    root = os.path.dirname(parent)
    return root if os.path.isdir(os.path.join(root, ".git")) else ""


def outside_residue(root: str, worktree: str = "") -> list:
    """Изменения в проекте, сделанные МИМО worktree.

    Каталог изоляции вычеркнут намеренно, и это не уступка точности.
    `git status` в корне проекта видит worktree как untracked-каталог —
    он и есть содержимое, которое там лежит по замыслу изоляции. Если
    этого не вычесть, то каждая задача заканчивается ложным
    `residue_outside_worktree`, то есть карантином на пустом месте.

    Правила ignore проекта тут ни при чём: слой не обязан зависеть от
    того, что `.gitignore` вообще содержит `.worktrees/`. Именно
    `.worktrees/` вычитается всегда, потому что это наш собственный
    каталог изоляции, а не пользовательская правка."""
    worktree_abs = os.path.abspath(worktree).replace("\\", "/") if worktree else ""
    out = []
    for rel in bl.dirty_files(root):
        # Именно префикс, а не `lstrip("./")`: lstrip выедает любой
        # набор символов из начала, и `.worktrees/…` превращалось в
        # `worktrees/…`, то есть фильтр не срабатывал никогда.
        # Такой баг невидим глазом и молча ломает откат целиком.
        normalized = rel.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized == ".worktrees" or normalized.startswith(".worktrees/"):
            continue
        if worktree_abs:
            tail = worktree_abs.lstrip("/")
            if normalized == tail or normalized.startswith(tail + "/"):
                continue
        out.append(rel)
    return out


def _prune_empty_dirs(worktree: str, rel: str) -> None:
    """Убрать каталоги, опустевшие после удаления файла. Тихо."""
    path = os.path.join(worktree, os.path.dirname(rel.replace("/", os.sep)))
    root = os.path.abspath(worktree)
    while os.path.abspath(path).startswith(root + os.sep):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


# --------------------------------------------------------------------------
# запуск воркера
# --------------------------------------------------------------------------

def _opencode() -> str:
    """Путь к CLI воркера. Явно, а не через PATH вслепую."""
    return os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"


def build_argv(model: str, task: str) -> list:
    """argv для `opencode run`.

    Список, а не строка: на этой машине длинный промпт через
    PowerShell ломался (`opencode run -f FILE` — это вложение, не
    сообщение; ран висел 58 минут, NOTES.md). `-m` обязателен: без него
    CLI берёт свой дефолт, а на этой машине он уже приводил к
    `openrouter/*` и трате общего бакета OpenRouter."""
    return [_opencode(), "run", "-m", model, task]


def _write_log(baseline_id: str, run_id: str, text: str) -> str:
    os.makedirs(log_dir(), exist_ok=True)
    path = os.path.join(log_dir(), "%s-%s.log" % (baseline_id, run_id))
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    return path


def attempt_model(model: str, task: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Одна попытка на одной модели. Никогда не бросает.

    Возвращает факты: код возврата, признак таймаута, длительность.
    `claim` (последние строки вывода) хранится отдельно и помечен как
    заявление воркера — зелёный текст не доказывает ничего."""
    started = time.time()
    argv = build_argv(model, task)
    try:
        proc = subprocess.run(argv, cwd=os.getcwd(), capture_output=True,
                              timeout=timeout)
        rc, out, err = (proc.returncode,
                        proc.stdout.decode("utf-8", "replace"),
                        proc.stderr.decode("utf-8", "replace"))
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = -1
        out = (exc.stdout or b"").decode("utf-8", "replace")
        err = (exc.stderr or b"").decode("utf-8", "replace")
        timed_out = True
    except OSError as exc:
        return {"model": model, "launched": False, "exit_code": None,
                "timed_out": False, "seconds": round(time.time() - started, 2),
                "stdout": "", "stderr": str(exc), "claim": "",
                "ok": False, "reason": REASON_WORKER_NOT_LAUNCHED}
    return {
        "model": model, "launched": True, "exit_code": rc,
        "timed_out": timed_out, "seconds": round(time.time() - started, 2),
        "stdout": out, "stderr": err,
        "claim": _tail(out), "ok": (rc == 0 and not timed_out),
        "reason": (REASON_WORKER_TIMEOUT if timed_out
                   else REASON_WORKER_OK if rc == 0
                   else REASON_WORKER_NONZERO),
    }


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def run_worker(task: str, models=LADDER, attempts: int = ATTEMPTS_PER_MODEL,
               timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Пройти лестницу. Ступень снимается только после двух неудач.

    Одна попытка на модель — не приговор (правило двух попыток:
    «мёртвый эндпоинт держит соединение 75-81 с, живые давали 91.4 с»).
    Модели из `DEAD_MODELS` и всё, что не `opencode/*`, отбрасываются
    на входе: закон $0 проверяется до запуска.

    Вывод последней попытки возвращается и при полном провале
    лестницы. Иначе разбирать инцидент нечем: попытки записаны, а
    почему воркер не смог — нет нигде, и это ровно то, что нужно
    знать в первую очередь."""
    attempts = max(1, int(attempts))
    tried, log = [], []
    last: dict = {}
    for model in models:
        ok, why = free_route_ok(model)
        if not ok:
            log.append({"model": model, "skipped": why})
            continue
        consecutive = 0
        while consecutive < attempts:
            record = attempt_model(model, task, timeout)
            last = record
            tried.append({k: v for k, v in record.items()
                          if k not in ("stdout", "stderr")})
            log.append({k: v for k, v in record.items()
                        if k not in ("stdout", "stderr")})
            if record["ok"]:
                return {"served_by": model, "attempts": tried, "skipped": log,
                        "stdout": record["stdout"], "stderr": record["stderr"],
                        "ok": True, "reason": REASON_WORKER_OK,
                        "total_seconds": record["seconds"]}
            consecutive += 1
    return {"served_by": "", "attempts": tried, "skipped": log,
            "stdout": last.get("stdout", ""), "stderr": last.get("stderr", ""),
            "ok": False,
            "reason": REASON_WORKER_NONZERO if tried else REASON_WORKER_NOT_LAUNCHED,
            "total_seconds": last.get("seconds", 0.0)}


# --------------------------------------------------------------------------
# переходы слоя
# --------------------------------------------------------------------------

def require_ledger(ledger_path: str) -> tuple[dict, str]:
    """LEDGER обязан существовать ДО запуска воркера (инвариант №7)."""
    ledger, reason = bl.load_ledger(ledger_path)
    if reason:
        return {}, reason
    if not ledger.get("checkpoint", {}).get("commit"):
        return {}, REASON_NO_CHECKPOINT
    return ledger, ""


def start(ledger: dict, state: dict | None = None, task_digest: str = "",
          detail: dict | None = None) -> dict:
    """NOT_STARTED/FAILED_CLEAN/BASELINE_RESTORED → RUNNING.

    Карантин проверяется первым и на диске, а не по наличию поля в
    состоянии: маркер обязан пережить перезапуск процесса, значит
    источник истины — файл (инвариант №4)."""
    state = state if isinstance(state, dict) and state else new_state(ledger)
    allowed, why = can_run(state)
    if not allowed:
        return {"status": "REFUSED", "reason": why, "state": state,
                "worker_may_start": False}
    state["task_digest"] = task_digest or state.get("task_digest", "")
    transition(state, RUNNING, dict(detail or {}, at=time.time()))
    save_state(state)
    return {"status": "OK", "reason": "", "state": state,
            "worker_may_start": True, "state_path": state_path(state["baseline_id"])}


def finish(state: dict, ok: bool, detail: dict | None = None) -> dict:
    """RUNNING → COMPLETED | FAILED_CLEAN | FAILED_DIRTY.

    Исход FAILED_CLEAN/FAILED_DIRTY **не передан вызывающим**: он
    вычисляется измерением worktree против baseline. Иначе воркер (или
    тот, кто его запустил) объявил бы «чисто» после того, как
    поправил пять файлов, и откат не случился бы никогда."""
    worktree, base = _measure_setup(state)
    if base is None:
        return {"status": "REFUSED", "reason": REASON_NO_CHECKPOINT,
                "state": state}
    res = residue(worktree, base)
    dirty = is_dirty(res)

    if ok and not dirty:
        target, kind = COMPLETED, ""
    elif ok and dirty:
        # Воркер отчитался об успехе, но дерево изменено. Это не
        # успех и не «чистый фейл»: состояние обязано отражать
        # измерение, а слой 4 разберётся, что именно изменено.
        target, kind = FAILED_DIRTY, FAILURE_CONFLICT
    elif dirty:
        target, kind = FAILED_DIRTY, FAILURE_CRASH
    else:
        target, kind = FAILED_CLEAN, FAILURE_ABORTED

    state["failure"] = {"kind": kind, "detail": detail or {},
                        "residue": res, "measured": True,
                        "baseline_tests_intact": not res["tests_modified"]}
    transition(state, target, {"ok": bool(ok), "dirty": dirty, "kind": kind})
    save_state(state)
    return {"status": "OK", "reason": "", "state": state, "dirty": dirty,
            "residue": res, "rollback_needed": target == FAILED_DIRTY,
            "failure_kind": kind,
            "baseline_tests_intact": not res["tests_modified"],
            "worker_may_start": False}


def fail(state: dict, reason: str, detail: dict | None = None) -> dict:
    """Зафиксировать падение воркера (краш, таймаут, kill).

    Для случая, когда процесс с воркером умер и `finish()` не был
    вызван: состояние осталось бы RUNNING, а по диску уже не видно,
    что работа была. Классификация — та же, измерением."""
    if state.get("state") == RUNNING:
        return finish(state, ok=False, detail=dict(detail or {}, reason=reason))
    allowed, why = can_run(state)
    if not allowed:
        return {"status": "REFUSED", "reason": why, "state": state}
    return {"status": "REFUSED",
            "reason": "fail() applies to a RUNNING run; current state is %s"
                      % state.get("state"), "state": state}


def _measure_setup(state: dict):
    worktree = state.get("worktree") or ""
    if not worktree and state.get("root"):
        worktree = state["root"]
    commit = (state.get("checkpoint") or {}).get("commit") or ""
    store = (state.get("checkpoint") or {}).get("store") or ""
    return worktree, (blob_digests(store, commit) or None)


def classify(state: dict) -> dict:
    """Измерить и ничего не менять.

    Нужно там, где состояние не доверяют: инцидент разбирают после
    перезапуска, и ответ «грязно или нет» должен получаться из
    сравнения с baseline, а не из записанного ранее флага.

    `baseline_tests_intact` — ответ, которого нет у слоя 2: сравнение
    по содержимому внутри worktree. Слой 2 считает по сырым байтам в
    корне проекта, и эти два ответа обязаны лежать рядом, а не вместо
    друг друга (см. `_canonical` и §8 инвариант 5)."""
    worktree, base = _measure_setup(state)
    if base is None:
        return {"measured": False, "reason": REASON_NO_CHECKPOINT,
                "dirty": None, "residue": {}, "baseline_tests_intact": None}
    try:
        res = residue(worktree, base)
    except RuntimeError as exc:
        return {"measured": False, "reason": REASON_GIT_FAILED,
                "detail": str(exc), "dirty": None, "residue": {},
                "baseline_tests_intact": None}
    dirty = is_dirty(res)
    return {"measured": True, "reason": "", "dirty": dirty, "residue": res,
            "baseline_tests_intact": not res["tests_modified"],
            "state_name": state.get("state"), "quarantined":
                bl.is_quarantined(state.get("baseline_id", ""))}


def rollback(state: dict, human_reason: str = "") -> dict:
    """FAILED_CLEAN|FAILED_DIRTY|ROLLING_BACK → BASELINE_RESTORED | RECOVERY_BLOCKED.

    Откат выполняет код. Провал отката — это не «попробуем ещё раз»:
    SCHEME.md §5.3 перечисляет четыре класса причин (файл вне worktree,
    файл занят процессом, внешний артефакт, ресурс за пределами
    snapshot scope), и ни одну из них повтором того же промта не
    вылечить. Поэтому провал отката терминален."""
    current = state.get("state")
    if current not in (FAILED_CLEAN, FAILED_DIRTY, ROLLING_BACK):
        return {"status": "REFUSED",
                "reason": "rollback applies to a failed run; current state is %s"
                          % current, "state": state}

    # Противоречие двух измерений: ранее классифицировано как чистое,
    # а откат нашёл остаток. Значит одно из измерений соврало, и
    # продолжать работу с таким деревом нельзя. Переход в ROLLING_BACK
    # здесь обязателен: попытка отката была, и в истории состояния она
    # должна быть видна, а не появляться из ниоткуда.
    if current == FAILED_CLEAN:
        pre = classify(state)
        if pre["measured"] and pre["dirty"]:
            transition(state, ROLLING_BACK, {"contradiction": pre["residue"]})
            save_state(state)
            return _block(state, REASON_CLEAN_CONTRADICTED,
                          "the run was classified FAILED_CLEAN, but the "
                          "tree differs from baseline: %s" % pre["residue"],
                          human_reason)

    if current != ROLLING_BACK:
        transition(state, ROLLING_BACK, {"at": time.time()})
        save_state(state)

    worktree, _ = _measure_setup(state)
    checkpoint = state.get("checkpoint") or {}
    outcome = restore(worktree, checkpoint.get("commit") or "",
                      checkpoint.get("store") or "")
    state["rollback"] = dict(outcome, attempted_at=time.time())

    if outcome["restored"]:
        transition(state, BASELINE_RESTORED,
                   {"commit": checkpoint.get("commit", ""), "reason":
                    outcome["reason"]})
        save_state(state)
        return {"status": "OK", "reason": "", "state": state,
                "restored": True, "rework_possible": True,
                "rework_needs_human":
                    state["failure"].get("kind") in HUMAN_GATE_KINDS}

    return _block(state, outcome["reason"] or REASON_TREE_RESIDUE,
                  outcome.get("detail", ""), human_reason, evidence=outcome)


def _block(state: dict, reason: str, detail: str, human_reason: str = "",
           evidence: dict | None = None) -> dict:
    """RECOVERY_BLOCKED + маркер на диске. Единственный выход в карантин.

    `evidence` — то, что известно о неудавшемся откате: остаток,
    занятые файлы, записи вне worktree. Оно попадает наружу, а не
    остаётся внутри: карантин без улик нельзя ни разобрать, ни снять
    обоснованно."""
    worktree = state.get("worktree") or ""
    checkpoint = state.get("checkpoint") or {}
    worktree_identity = "%s@%s" % (worktree, bl.head_commit(worktree) or "no-head")
    text = "%s: %s%s" % (reason, detail,
                         (" (human: %s)" % human_reason) if human_reason else "")
    marker = bl.mark_quarantine(state.get("baseline_id", ""),
                                worktree_identity,
                                checkpoint.get("commit", ""), text)
    state["quarantine"] = marker
    transition(state, RECOVERY_BLOCKED,
               {"reason": reason, "detail": detail, "marker": marker})
    save_state(state)
    return {"status": "BLOCKED", "reason": reason, "detail": detail,
            "state": state, "quarantine": marker, "restored": False,
            "rework_possible": False,
            "locked": (evidence or {}).get("locked", []),
            "residue": (evidence or {}).get("residue", {}),
            "root_dirty": (evidence or {}).get("root_dirty", []),
            "next": "human release only: worker_state.py unblock --ledger … "
                    "--reason … ; no worker run, no rework, no worktree reuse"}


def rework(state: dict, task_digest: str = "", human_authorized: bool = False,
           reason: str = "") -> dict:
    """Новая попытка после отката.

    `human_authorized` обязателен после `WORKER_CRASH` и
    `WORKER_CLAIM_CONFLICT`. Обоснование в docstring: §5.3 разрешает
    rework после успешного отката (то есть машина не блокирует физическую
    возможность), а §6/§10 требуют эскалации по WORKER_CRASH (то есть
    автоматического повтора быть не должно). Оба выполняются: машина
    разрешает, человек решает, решение видно в истории.

    `WORKER_ABORTED` (упал, ничего не тронув) гейтом не закрыт: повтор
    там — обычный ход цикла, иначе конвейер останавливался бы на первой
    же неудаче, чего §5.1 запрещает делать с трудно-проверяемым."""
    if state.get("state") not in (FAILED_CLEAN, BASELINE_RESTORED):
        allowed, why = can_run(state)
        return {"status": "REFUSED",
                "reason": why or ("rework applies to a failed run; current "
                                  "state is %s" % state.get("state")),
                "state": state}
    kind = state.get("failure", {}).get("kind")
    if kind in HUMAN_GATE_KINDS and not human_authorized:
        return {"status": "REFUSED",
                "reason": "%s: repeating the same prompt reproduces the same "
                          "outcome (SCHEME.md §6). Rollback restored the "
                          "baseline, so the retry is physically possible, but "
                          "it needs an explicit human decision." % kind,
                "needs_human_authorization": True,
                "failure_kind": kind,
                "state": state, "rework_possible": True}

    previous = state.get("task_digest") or ""
    state["task_digest"] = task_digest or previous
    transition(state, RUNNING, {
        "task_digest": state["task_digest"],
        "human_authorized": bool(human_authorized),
        "reason": reason or "",
        "previous_failure": state.get("failure", {}).get("kind", ""),
    })
    save_state(state)
    return {"status": "OK", "reason": "", "state": state,
            "worker_may_start": True}


def unblock(ledger: dict, reason: str) -> dict:
    """Явная очистка человеком. Никакого «снять молча».

    После снятия карантина состояние возвращается в NOT_STARTED, а не
    в BASELINE_RESTORED: среда была загрязнена, правильное восстановление
    — новый baseline (чистое дерево, новый LEDGER), а не продолжение
    старого."""
    bid = _safe_id(ledger.get("baseline_id") or "")
    if not reason.strip():
        return {"status": "REFUSED", "state": load_state(bid),
                "reason": "unblock requires a reason: the decision is the "
                          "record, and without it the release is "
                          "indistinguishable from a bug"}
    state = load_state(bid)
    marker = bl.read_quarantine(bid)
    if not marker and state.get("state") != RECOVERY_BLOCKED:
        return {"status": "REFUSED", "state": state,
                "reason": "this baseline is not quarantined"}
    cleared = bl.clear_quarantine(bid)
    if state:
        state["quarantine"] = {}
        state["history"].append({"state": "HUMAN_RELEASE", "at": time.time(),
                                 "detail": {"reason": reason,
                                            "marker": marker}})
        state["state"] = NOT_STARTED
        state["failure"] = {}
        state["rollback"] = {}
        save_state(state)
    return {"status": "OK", "cleared": cleared, "reason": reason,
            "state": state or new_state(ledger),
            "marker": marker, "next": "create a fresh baseline; the polluted "
                                      "tree must not be adopted as a starting "
                                      "point"}


# --------------------------------------------------------------------------
# полный цикл
# --------------------------------------------------------------------------

def run(ledger_path: str, task: str, models=LADDER,
        attempts: int = ATTEMPTS_PER_MODEL,
        timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Один заход: барьер → RUNNING → воркер → измерение → откат.

    Барьер проверяется здесь, а не вызывающим кодом: `require_barrier`
    без воркера — пустое предупреждение, и слой 3 не имеет права быть
    тем местом, где этот запрет можно обойти."""
    ledger, reason = require_ledger(ledger_path)
    if reason:
        return {"status": "REFUSED", "reason": reason, "worker_may_start": False,
                "state": {}, "phase": "barrier"}

    state = load_state(ledger["baseline_id"])
    if not state:
        state = new_state(ledger)

    # Дерево проекта обязано быть чистым и на старте: иначе baseline
    # описывает смесь чужой работы с работой воркера. Каталог изоляции
    # при этом не в счёт — он и есть доказательство того, что воркер
    # работал в отдельном дереве.
    residue_before = outside_residue(ledger["root"],
                                     ledger.get("worktree") or "")
    if residue_before:
        return {"status": "REFUSED", "worker_may_start": False, "phase": "barrier",
                "state": state,
                "reason": "tree_dirty: the project tree is not empty before the "
                          "worker starts: %s" % ", ".join(residue_before[:10])}

    digest = _digest(task)
    began = start(ledger, state, task_digest=digest)
    if not began["worker_may_start"]:
        return {"status": "REFUSED", "worker_may_start": False, "phase": "start",
                "state": state, "reason": began["reason"]}
    state = began["state"]

    run_id = "%d" % int(time.time())
    outcome = run_worker(task, models=models, attempts=attempts, timeout=timeout)
    log = _write_log(state["baseline_id"], run_id,
                     "== task ==\n%s\n\n== stdout ==\n%s\n\n== stderr ==\n%s\n"
                     % (task, outcome["stdout"], outcome["stderr"]))

    state["run"] = {
        "run_id": run_id, "log": log, "served_by": outcome["served_by"],
        "ok": outcome["ok"], "reason": outcome["reason"],
        "attempts": outcome["attempts"], "skipped": outcome["skipped"],
        "total_seconds": outcome["total_seconds"],
        # Заявление воркера — отдельно от фактов и с явной пометкой.
        "worker_claim": {"trust": "low", "text": _tail(outcome["stdout"])},
    }

    ended = finish(state, ok=outcome["ok"],
                   detail={"served_by": outcome["served_by"],
                           "exit_reason": outcome["reason"],
                           "log": log})
    result = {"status": ended["status"], "phase": "finish", "state": state,
              "reason": ended.get("reason", ""),
              "worker": state["run"], "dirty": ended.get("dirty"),
              "residue": ended.get("residue", {})}

    if ended.get("rollback_needed") or state["state"] == FAILED_DIRTY:
        rolled = rollback(state)
        result["rollback"] = {k: v for k, v in rolled.items() if k != "state"}
        result["state"] = rolled["state"]
        result["status"] = "BLOCKED" if rolled["status"] == "BLOCKED" else "OK"
    result["worker_may_start"] = False
    result["next_layer"] = ("слой 4 (механика); вердикт выносит он, здесь "
                            "вердиктов нет")
    return result


def _digest(task: str) -> str:
    return hashlib.sha256((task or "").encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------

def render(result: dict) -> str:
    # `state` здесь — всегда словарь состояния. Отчёт принимает на
    # вход и результаты измерения (`classify`), где состояние названо
    # строкой в `state_name`, поэтому проверка на тип здесь не
    # перестраховка, а единственное, что удерживает отчёт от падения на
    # ровном месте посреди разбора инцидента.
    state = result.get("state")
    if not isinstance(state, dict):
        state = {}
    lines = ["WORKER STATE  %s" % (state.get("state") or result.get("status", "?"))]
    if state.get("baseline_id"):
        lines.append("baseline id: %s" % state["baseline_id"])
    if state.get("worktree"):
        lines.append("worktree: %s" % state["worktree"])
    if (state.get("checkpoint") or {}).get("commit"):
        lines.append("baseline commit: %s" % state["checkpoint"]["commit"][:12])
    if result.get("status") not in ("OK", None):
        lines.append("status: %s" % result.get("status"))
    if result.get("reason"):
        lines.append("reason: %s" % result["reason"])

    worker = result.get("worker") or state.get("run") or {}
    if worker:
        lines.append("worker: served_by=%s ok=%s reason=%s"
                     % (worker.get("served_by") or "-", worker.get("ok"),
                        worker.get("reason", "")))
        for row in worker.get("attempts") or []:
            lines.append("  attempt %-42s ok=%-5s %ss%s"
                         % (row.get("model"), row.get("ok"), row.get("seconds"),
                            " TIMEOUT" if row.get("timed_out") else ""))
        for row in worker.get("skipped") or []:
            lines.append("  skipped %-40s %s" % (row.get("model"),
                                                 row.get("skipped", "")))
        if worker.get("log"):
            lines.append("log: %s" % worker["log"])
        claim = (worker.get("worker_claim") or {}).get("text")
        if claim:
            lines.append("worker claim (low trust, not a verdict): %s"
                         % _tail(claim, 160).replace("\n", " | "))

    res = result.get("residue") or {}
    if res:
        lines.append("residue: %d missing, %d modified, %d extra"
                     % (len(res.get("missing") or []),
                        len(res.get("modified") or []),
                        len(res.get("extra") or [])))
        for key in ("missing", "modified", "extra"):
            for rel in (res.get(key) or [])[:20]:
                lines.append("  ! %s: %s" % (key, rel))
        if res.get("tests_modified"):
            lines.append("  !! BASELINE TESTS CHANGED (слой 4 ставит "
                         "TEST_TAMPERING): %s"
                         % ", ".join(res["tests_modified"][:20]))
        if res.get("eol_only"):
            lines.append("  ~eol only (не остаток; строгий сырой хэш "
                         "слоя 2 считает это подменой): %s"
                         % ", ".join(res["eol_only"][:10]))

    rolled = result.get("rollback") or {}
    if not rolled and (result.get("locked") or result.get("root_dirty")
                       or result.get("status") == "BLOCKED"):
        # Прямой выход из `rollback()`: улики лежат в самом ответе.
        rolled = result
    if rolled:
        lines.append("rollback: restored=%s reason=%s"
                     % (rolled.get("restored"), rolled.get("reason", "")))
        if rolled.get("locked"):
            lines.append("  locked files: %s" % ", ".join(rolled["locked"][:20]))
        if rolled.get("root_dirty"):
            lines.append("  wrote OUTSIDE the worktree: %s"
                         % ", ".join(rolled["root_dirty"][:20]))

    if state.get("quarantine"):
        marker = state["quarantine"]
        lines.append("QUARANTINE (terminal, human release only)")
        lines.append("  worktree:   %s" % marker.get("worktree_identity", ""))
        lines.append("  checkpoint: %s" % marker.get("checkpoint_identity", ""))
        lines.append("  reason:     %s" % marker.get("reason", ""))
        lines.append("  timestamp:  %s" % marker.get("timestamp_iso", ""))
    elif result.get("next"):
        lines.append("next: %s" % result["next"])
    elif result.get("state"):
        lines.append("next: %s" % _next_hint(state))
    return "\n".join(lines)


def _next_hint(state: dict) -> str:
    current = state.get("state")
    if current == COMPLETED:
        return "слой 4 (механика): сверка хэшей baseline-тестов"
    if current == BASELINE_RESTORED:
        return ("откат выполнен. rework возможен; после WORKER_CRASH нужен "
                "явный человек (--human-authorized)")
    if current == FAILED_CLEAN:
        return "откат не нужен: дерево совпадает с baseline"
    if current == FAILED_DIRTY:
        return "rollback --ledger …"
    if current == RUNNING:
        return "воркер идёт; при аварии — fail --ledger …"
    return "-"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _reconfigure_stdout() -> None:
    """Отчёт по-русски, консоль на этой машине cp866/cp1251, читатель
    ждёт UTF-8. Барьер, который не прочитать, не соблюдается."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _resolve(args) -> tuple[dict, str]:
    """LEDGER + причина отказа. Барьер проверяется в каждой команде,
    которая что-то меняет, — «проверь перед запуском» должно быть
    свойством модуля, а не дисциплиной вызывающего."""
    ledger, reason = require_ledger(getattr(args, "ledger", "") or "")
    if reason:
        return {}, reason
    state = load_state(ledger["baseline_id"])
    if not state:
        state = new_state(ledger)
    return {"ledger": ledger, "state": state}, ""


def main(argv=None) -> int:
    import argparse

    _reconfigure_stdout()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--ledger", default="", help="path to the LEDGER")

    parser = argparse.ArgumentParser(
        description="worker state machine: run, classify, roll back, quarantine.")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", parents=[common],
                           help="full cycle: barrier -> worker -> measure -> rollback")
    p_run.add_argument("--task", default="")
    p_run.add_argument("--task-file", default="")
    p_run.add_argument("--model", action="append", default=[])
    p_run.add_argument("--attempts", type=int, default=ATTEMPTS_PER_MODEL)
    p_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)

    for name, helptext in (("start", "mark RUNNING without launching"),
                           ("classify", "measure only, change nothing"),
                           ("state", "print the stored state"),
                           ("rollback", "roll back to the baseline commit"),
                           ("block", "quarantine without attempting a rollback")):
        node = sub.add_parser(name, parents=[common], help=helptext)
        if name in ("rollback", "block"):
            node.add_argument("--reason", default="", help="human note")

    p_fail = sub.add_parser("fail", parents=[common],
                            help="record a dead run (crash, timeout, kill)")
    p_fail.add_argument("--reason", required=True)

    p_rew = sub.add_parser("rework", parents=[common],
                           help="new attempt after a rollback")
    p_rew.add_argument("--task-digest", default="")
    p_rew.add_argument("--human-authorized", action="store_true",
                       help="required after WORKER_CRASH")
    p_rew.add_argument("--reason", default="")

    p_unblock = sub.add_parser("unblock", parents=[common],
                               help="human release of a quarantine")
    p_unblock.add_argument("--reason", required=True)

    sub.add_parser("transitions", parents=[common],
                   help="print the transition table (the contract)")

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2

    as_json = bool(getattr(args, "json", False))

    if args.cmd == "transitions":
        table = {k: list(v) for k, v in TRANSITIONS.items()}
        if as_json:
            print(json.dumps({"schema": SCHEMA, "transitions": table,
                              "terminal": sorted(TERMINAL),
                              "startable": sorted(STARTABLE)},
                             ensure_ascii=False, indent=2))
        else:
            for state in STATES:
                targets = TRANSITIONS.get(state, ())
                print("%-18s -> %s" % (state,
                                       ", ".join(targets) or "(terminal)"))
        return 0

    if args.cmd == "run":
        task = args.task
        if args.task_file:
            with open(args.task_file, encoding="utf-8") as f:
                task = f.read()
        if not task.strip():
            parser.error("run needs --task or --task-file")
        result = run(args.ledger, task,
                     models=args.model or LADDER,
                     attempts=args.attempts, timeout=args.timeout)
    else:
        ctx, reason = _resolve(args)
        if reason:
            result = {"status": "REFUSED", "reason": reason, "state": {}}
        elif args.cmd == "state":
            result = {"status": "OK", "state": ctx["state"],
                      "quarantined": bl.is_quarantined(
                          ctx["state"].get("baseline_id", "")),
                      "history": ctx["state"].get("history", [])}
        elif args.cmd == "classify":
            result = dict(classify(ctx["state"]), status="OK")
        elif args.cmd == "start":
            result = start(ctx["ledger"], ctx["state"])
        elif args.cmd == "fail":
            result = fail(ctx["state"], args.reason)
        elif args.cmd == "rollback":
            result = rollback(ctx["state"], human_reason=args.reason)
        elif args.cmd == "block":
            result = _block(ctx["state"],
                            args.reason or REASON_TREE_RESIDUE,
                            "blocked by explicit request", args.reason)
        elif args.cmd == "rework":
            result = rework(ctx["state"], task_digest=args.task_digest,
                            human_authorized=args.human_authorized,
                            reason=args.reason)
        else:  # unblock
            result = unblock(ctx["ledger"], args.reason)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(result))
    status = result.get("status")
    if status == "OK":
        return 0
    if (result.get("state") or {}).get("state") in (
            COMPLETED, BASELINE_RESTORED, FAILED_CLEAN):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
