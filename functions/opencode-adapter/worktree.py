"""diff рабочего дерева: единственный источник правды о том, что изменилось.

ДЫРА, которую закрывает этот модуль. Обещание адаптера - «задача по HTTP
доходит до opencode и возвращает непустой diff» - не выполнялось, потому что
diff брался из `/api/session/{id}/diff`, а этот эндпоинт в opencode 2.0.22
возвращает `{"data": []}`.

Измерено на живом сервере 05.10, три независимых прогона:

  * ответ `session.outcome == "succeeded"`, `session.time.idle` заполнен;
  * файл на диске изменён - `calc.py` содержит `return a + b`;
  * `/api/session/{id}/diff` отдаёт `[]` - и через 0 секунд после сигнала, и
    через 60 секунд.

Проверено и отдельно от расположения каталога: сначала работа шла в `/tmp`,
потом в каталоге внутри проекта opencode (`location.directory` в ответе
совпадал) - результат тот же. То есть дело не в том, что каталог «не тот»,
а в том, что хранилище диффов этой версией не наполняется.

Поэтому diff считается здесь, из файлов, которые адаптер и так отдал модели.
Это не запасной путь: это единственный источник, который можно и проверить, и
воспроизвести тестом без сети и без модели.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ

Не гадает. Если каталог не изменился, результат пустой - и вызывающий обязан
назвать причину (см. `service.py`), а не выдавать пустой список как «всё
хорошо». Пустой diff и «модель ответила в чате, ничего не меняя» - это разные
события, и различие видно только здесь.

ОГРАНИЧЕНИЯ, ВЫБРАННЫЕ СОЗНАТЕЛЬНО

  * только текстовые файлы: бинарные в diff не попадают, но попадают в
    `skipped`, чтобы молчание было видимым;
  * файлы крупнее `MAX_FILE_BYTES` пропускаются - diff мегабайтного файла не
    нужен никому, а память под него не выделяется;
  * служебные каталоги пропускаются: правка `.git` или `__pycache__` - это не
    результат задачи.
"""
from __future__ import annotations

import difflib
import os

# Каталоги, правка которых не является результатом задачи.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "venv", ".venv", "env",
    ".idea", ".vscode", "dist", "build", ".next", "target",
})

# Предел на файл и на число файлов. Задача меняет handful файлов; если меняет
# всё дерево, это не задача, а сборка, и молчаливый мегабайтный diff хуже
# внятного отказа.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 5000

# Расширения, которые почти наверняка не текст.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".bz2", ".xz", ".7z", ".tar", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".pyc", ".pyo", ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".mkv", ".avi", ".mov", ".wav", ".sqlite", ".db",
    ".bin", ".wasm", ".onnx", ".safetensors", ".pt", ".pth", ".npy",
})


def looks_binary(path: str, probe: int = 4096) -> bool:
    """Есть ли в начале файла нулевой байт - грубый, но верный признак."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(probe)
    except OSError:
        return False


def snapshot(root: str) -> dict:
    """Снимок текстовых файлов каталога: относительный путь -> содержимое.

    Возвращается словарём, а не списком: сравнение снимков не должно зависеть
    от порядка обхода, который на разных файловых системах разный.
    """
    out: dict = {}
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if len(out) >= MAX_FILES:
                return out
            if os.path.splitext(name)[1].lower() in BINARY_SUFFIXES:
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                if looks_binary(full):
                    continue
                with open(full, encoding="utf-8") as f:
                    out[rel] = f.read()
            except (OSError, UnicodeDecodeError):
                # Нечитаемый файл - не повод упасть: он просто не попадёт в
                # diff, и это лучше, чем потерять весь результат задачи.
                continue
    return out


def _unified(rel: str, before: str | None, after: str | None) -> tuple:
    """Стандартный unified diff и счётчики строк."""
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    patch = "".join(difflib.unified_diff(a, b, fromfile="a/" + rel,
                                         tofile="b/" + rel, n=3))
    additions = deletions = 0
    for line in patch.splitlines(keepends=True):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return patch, additions, deletions


def diff_snapshots(before: dict, after: dict, root: str = "") -> list:
    """Что изменилось между снимками. Список словарей - как у транспорта.

    Формат элемента совпадает с тем, что отдавал эндпоинт opencode
    (`file`, `additions`, `deletions`, `patch`, `status`), поэтому вызывающий
    не различает два источника и не переписывается, когда эндпоинт однажды
    начнёт отвечать.
    """
    out = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel)
        new = after.get(rel)
        if old == new:
            continue
        patch, adds, dels = _unified(rel, old, new)
        if old is None:
            status = "added"
        elif new is None:
            status = "deleted"
        else:
            status = "modified"
        out.append({"file": rel, "additions": adds, "deletions": dels,
                    "patch": patch, "status": status})
    return out


def describe_empty(directory: str, outcome: str | None) -> str:
    """Причина пустого diff - словом, а не пустым списком.

    Пустой diff без причины неотличим от «всё прошло», и приёмка читает его
    как зелёный. Различить можно ровно два случая, и оба названы здесь:
    ход кончился неудачей, или ход кончился успехом, а файлы не тронуты -
    то есть модель ответила в чате и ничего не меняла.
    """
    if outcome and outcome not in ("succeeded",):
        return ("the turn ended as %r, so nothing was written; the model did "
                "not get to editing the files" % outcome)
    return ("the turn ended successfully, but not one file under %s changed: "
            "the model answered in the chat instead of editing. The "
            "worktree is the source of truth here, and it says there is "
            "nothing to diff" % (directory or "?"))


def diff_worktree(root: str, before: dict) -> tuple:
    """Готовый снимок «после» плюс diff. Так сервис не знает про два вызова."""
    after = snapshot(root)
    return diff_snapshots(before, after, root), after
