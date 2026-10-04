"""Матрица «что чем проверяется» обязана соответствовать репозиторию.

Проблема, от которой эта работа и появилась: половина листа годами копила
пункты «нужен Windows», и каждое новое поколение воркеров выясняло это
заново, потому что знание жило в голове планировщика. Матрица решает это
только если её нельзя расписать словами, поэтому здесь проверяется факт, а не
формулировка:

1. **Ссылки существуют.** Каждый `путь::группа` обязан быть реальной группой
   в реальном файле. Ссылка на несуществующую группу хуже, чем её отсутствие:
   она выглядит как доказательство.
2. **Наборы не осиротели.** Каждый `functions/*/test_*.py` обязан быть в
   матрице, иначе он проверяет что-то, чего никто не учёл.
3. **Класс машины честен.** Набор, помеченный `server`, обязан быть запускаем
   на сервере (в манифесте покрытия - `ci: true`), а помеченный `windows` -
   не быть в CI. Иначе строка обещает проверку, которой не будет.
4. **Пропусков нет.** Каждый пункт листа обязан быть либо в `items`, либо в
   `sheet_furniture`. Это и есть требование приёмки (2): для каждого пункта
   сказано, чем он закрывается.
5. **Непроверяемое названо.** Строка без `proof` обязана иметь
   `owner_required` с причиной - требование приёмки (3). «Пункт есть, а чем
   закрыть нечем» допускается только в этой форме.
6. **Таблица не разошлась с данными.** `VERIFICATION.md` обязан быть ровно
   тем, что рендерит `render.py`.

Запуск: python functions/verification-matrix/test_verification_matrix.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import render  # noqa: E402

MATRIX = os.path.join(HERE, "matrix.json")
MANIFEST = os.path.join(_ROOT, "functions", "meta", "coverage-manifest.json")
SHEETS = {
    # Листы с открытыми пунктами. `PASSED.md` - архив, его не читаем: оттуда
    # задачи не берутся по правилу самого листа.
    "TODO.md": "TODO",
    "TOTEST.md": "TOTEST",
}

# Заголовок, похожий на задачу, не может быть «мебелью листа».
#
# Это и есть обход, который делал матрицу бесполезной: любой пункт можно было
# объявить `sheet_furniture`, и проверка «каждый заголовок либо пункт, либо
# мебель» проходила, не сказав ни слова о его проверке. Так шесть задач
# А1/А2/А3/А6/А7/А8 и девять разделов 2-bis остались без единого «чем
# закрыто» - при том, что задача A7 как раз требовала сказать это для
# каждого пункта.
#
# Формы выводятся механически из вида заголовка, а не из доброй воли: `А1`,
# `ФАЗА 3`, `P1`, `2-bis.5`, а также слова «ТРЕБУЕТ», «НЕ ЗАКРЫТО», «Уборка»,
# «не сделано». Мебелью остаётся «как читать», «порядок», «факты», «решения
# владельца» - там задач нет по смыслу.
TASK_SHAPES = (re.compile(r"^А\d"), re.compile(r"^ФАЗА"),
               re.compile(r"^P\d"),
               # Подпункт вида «3.2 Раскладка панелей» или «2-bis.1 Проверка
               # ключей». Именно `\d+\.\d`, а не `\d+[.\-]`: иначе под правило
               # попадает «0. Контекст и закон», который контекстом и
               # является, а не задачей.
               re.compile(r"^\d+\.\d"), re.compile(r"^\d+-bis\."))
TASK_WORDS = ("ТРЕБУЕТ", "НЕ ЗАКРЫТО", "НЕ ВЫПОЛНЕНО", "Уборка",
              "не сделано", "решений владельца")


def looks_like_a_task(head: str) -> bool:
    if any(rx.search(head) for rx in TASK_SHAPES):
        return True
    low = head.lower()
    return any(w.lower() in low for w in TASK_WORDS)

MACHINES = {"server", "windows", "gpu", "live-provider", "owner"}
STATUSES = {"verified", "open", "owner"}
TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def load_matrix() -> dict:
    with open(MATRIX, encoding="utf-8") as f:
        return json.load(f)


def load_manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def suite_labels(path: str) -> set:
    """Метки групп набора ровно так, как их печатает его `main()`.

    Разбор берёт ту же форму, что и у наборов: декоратор `@test` даёт
    `test_x_y`, список регистрации даёт свой первый элемент. Несовпадение
    здесь означало бы, что ссылка в матрице не выводима, - то есть
    недоказуема.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    labels = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
                getattr(d, "id", getattr(d, "attr", None)) == "test"
                for d in node.decorator_list):
            labels.add(node.name.replace("test_", "", 1).replace("_", " "))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") in ("TESTS", "groups", "GROUPS", "CHECKS")
                for t in node.targets):
            if isinstance(node.value, ast.List):
                for item in node.value.elts:
                    if (isinstance(item, ast.Tuple) and item.elts
                            and isinstance(item.elts[0], ast.Constant)
                            and isinstance(item.elts[0].value, str)):
                        labels.add(item.elts[0].value)
    return labels


def sheet_headings() -> list:
    """Заголовки живых листов: `TODO.md` (открытые) и `TOTEST.md` (сделано,
    но не проверено). Читаем, но не правим - листы ведёт оркестратор.

    Две тонкости, обе вынужденные фактом, а не вкусом:

      * `TODO.md` может содержать в себе начало другого документа (копию
        `TOTEST.md` с его «Правила» и «Формат записи» - это не пункты
        проекта). Признак такого вставленного документа - заголовок ПЕРВОГО
        уровня, названный по имени ДРУГОГО листа. Признак не в уровне: в
        `TODO.md` есть и `# ПЛАН РАБОТ`, и `# АРХИТЕКТУРА`, и это его
        собственные разделы, а не чужие документы;
      * пункт, перенесённый в `TOTEST.md`, остаётся открытым по смыслу -
        «сделано, но не проверено» это не «закрыто». Поэтому живыми
        считаются оба листа, а `PASSED.md` не читается вовсе.
    """
    out = []
    for filename, own_title in SHEETS.items():
        foreign = {title for name, title in SHEETS.items() if name != own_title}
        path = os.path.join(_ROOT, filename)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^#{1,3} (.+?)\s*$", line)
                if not m:
                    continue
                title = m.group(1).strip()
                if title.startswith(own_title):
                    continue
                if line.startswith("# ") and any(title.startswith(n)
                                                  for n in foreign):
                    break        # вставлен другой лист - наш кончился
                out.append(title)
    return out


# --------------------------------------------------------------------------

@test
def test_a_task_cannot_be_declared_sheet_furniture():
    """Обход, который чинится здесь: задача, объявленная «мебелью листа»,
    молча теряет свою проверку.

    Проверка формы механическая, поэтому переименовать пункт в «Факты» не
    получится, а вот `А7. …` или `2-bis.5 …` попасть в мебель больше не могут.
    """
    fails = []
    m = load_matrix()
    mapped = {i["item"] for i in m["items"]}
    for head in m.get("sheet_furniture") or []:
        if looks_like_a_task(head):
            fails.append("%r looks like a task but is filed as sheet "
                         "furniture, so nothing says how it is verified"
                         % head)
        if head in mapped:
            fails.append("%r is both an item and furniture" % head)
    return fails


@test
def test_every_item_states_its_verification_status():
    """Три состояния, и ни одного четвёртого «не знаю».

    `verified` - есть проверка, и она обязана разрешаться.
    `open`     - задача не реализована, проверять нечем; обязан быть назван
                 `why_not`, а доказательства быть НЕ должно, иначе пункт
                 был бы помечен выполненным.
    `owner`    - закрывает человек; `owner_required` с причиной.

    Раньше трёх состояний не было, и «проверка не указана» выглядела как
    обычный `items` без поля `proof` - то есть как будто закрытый пункт.
    """
    fails = []
    for row in load_matrix()["items"]:
        status = row.get("status")
        if status not in STATUSES:
            fails.append("item %r has status %r (expected one of %s)"
                         % (row["item"], status, ", ".join(sorted(STATUSES))))
            continue
        if status == "verified" and not row.get("proof"):
            fails.append("item %r is verified but names no proof - that is "
                         "exactly 'проверка не указана'" % row["item"])
        if status == "open":
            if not str(row.get("why_not") or "").strip():
                fails.append("item %r is open with no `why_not`" % row["item"])
            if row.get("proof"):
                fails.append("item %r is open yet carries a proof - either it "
                             "is done (then verified) or the proof is stale"
                             % row["item"])
        if status == "owner" and not row.get("owner_required"):
            fails.append("item %r is owner yet not owner_required"
                         % row["item"])
    return fails


@test
def test_every_reference_points_at_a_real_group():
    """Ссылка на несуществующую группу хуже её отсутствия: она похожа на
    доказательство."""
    fails = []
    m = load_matrix()
    for row in m["suites"]:
        proof = row["proof"]
        if "::" not in proof:
            fails.append("%s: the proof must be `path::group` or a command, "
                         "got %r" % (row["suite"], proof))
            continue
        path, label = proof.split("::", 1)
        full = os.path.join(_ROOT, path)
        if not os.path.isfile(full):
            fails.append("%s: the proof points at a missing file %s"
                         % (row["suite"], path))
            continue
        labels = suite_labels(full)
        if labels and label not in labels:
            fails.append("%s: the group %r does not exist in %s (sample: %s)"
                         % (row["suite"], label, path,
                            sorted(labels)[:2]))
    for row in m["items"]:
        proof = row.get("proof")
        if not proof:
            continue
        if "::" not in proof:
            continue
        path, label = proof.split("::", 1)
        full = os.path.join(_ROOT, path)
        if not os.path.isfile(full):
            fails.append("item %r: the proof points at a missing file %s"
                         % (row["item"], path))
            continue
        labels = suite_labels(full)
        if labels and label not in labels:
            fails.append("item %r: the group %r does not exist in %s"
                         % (row["item"], label, path))
    return fails


@test
def test_every_suite_in_the_repo_is_in_the_matrix():
    """Набор, которого нет в матрице, проверяет то, чего никто не учёл."""
    fails = []
    listed = {s["suite"] for s in load_matrix()["suites"]}
    for p in sorted(Path(_ROOT).glob("functions/*/test_*.py")):
        rel = str(p.relative_to(_ROOT)).replace(os.sep, "/")
        if rel not in listed:
            fails.append("%s exists but no matrix row says where it runs or "
                         "what it proves" % rel)
    for rel in sorted(listed):
        if not os.path.isfile(os.path.join(_ROOT, rel)):
            fails.append("%s is in the matrix but does not exist" % rel)
    return fails


@test
def test_the_machine_class_is_honest():
    """Класс машины - обещание, и обещание должно сходиться с фактом.

    Смысл проверки не в том, чтобы `server` означал «есть в CI»: набор может
    честно работать на сервере и при этом не быть в списке portable-suite
    (такой случай есть, и он помечен отдельно). Смысл в другом, и это реальное
    расхождение, а не стилистика: набор, которому нужен Windows или GPU,
    не должен ЗАЯВЛЯТЬ, что на сервере он что-то проверяет. Он может стоять
    в переносимом списке - именно затем, чтобы его пропуск был объявлен и
    виден, - но тогда у него обязан быть нулевой порог выполненных групп.
    Наоборот: `server` не должен зависеть от локального Ollama."""
    fails = []
    m = load_matrix()
    entries = {e["path"]: e for e in load_manifest()["suites"]}
    for row in m["suites"]:
        machine = row.get("machine")
        if machine not in MACHINES:
            fails.append("%s: unknown machine %r (known: %s)"
                         % (row["suite"], machine, ", ".join(sorted(MACHINES))))
            continue
        entry = entries.get(row["suite"])
        if entry is None:
            continue
        in_ci = bool(entry.get("ci"))
        reason = str(entry.get("reason") or "").lower()
        floor = int(entry.get("min_executed") or 0)
        if machine in ("windows", "gpu") and in_ci and floor > 0:
            fails.append("%s needs %r yet claims %d checks on the server - it "
                         "can only declare a skip there, so its floor must be 0"
                         % (row["suite"], machine, floor))
        if machine == "server" and in_ci and floor == 0:
            # Обратная сторона той же правды: набор, помеченный `server`, но
            # с нулевым порогом, не проверяет на сервере ничего. Именно так
            # выглядел бы Windows-only набор, объявленный серверным.
            fails.append("%s is marked 'server' but its floor is 0 - it "
                         "verifies nothing here, so the class is a lie"
                         % row["suite"])
        if machine == "server" and ("ollama" in reason or "gpu" in reason):
            fails.append("%s is marked 'server' but the manifest says it "
                         "needs a local model: %s"
                         % (row["suite"], entry.get("reason")))
    for row in m["commands"]:
        if row.get("machine") not in MACHINES:
            fails.append("command %r: unknown machine %r"
                         % (row["command"], row.get("machine")))
    for row in m["items"]:
        if row.get("machine") not in MACHINES:
            fails.append("item %r: unknown machine %r"
                         % (row["item"], row.get("machine")))
    return fails


@test
def test_every_sheet_item_is_mapped():
    """Требование приёмки (2): для каждого пункта сказано, чем он закрыт.

    Новый заголовок в листе обязан появиться либо в `items`, либо в
    `sheet_furniture`, иначе матрица молча перестала быть полной.

    Направления здесь НЕсимметричны, и это не смягчение:

      * заголовок листа без строки в матрице - ОШИБКА. Это и есть «пункт
        есть, а чем закрыть нечем», и ради этого правила всё затевалось;
      * строка матрицы, которой больше нет в листе, - не ошибка, а громкая
        заметка. Такой пункт ничего не может скрыть: он лишь описывает то,
        что переехало или закрылось. Если сделать и это ошибкой, матрица
        становится вечно красной при каждой правке листа, и рано или поздно
        её перестанут чинить, а предупреждения перестанут читать - то есть
        инструмент превратится в шум.
    """
    fails = []
    m = load_matrix()
    mapped = {i["item"] for i in m["items"]}
    furniture = set(m.get("sheet_furniture") or [])
    headings = sheet_headings()
    for head in headings:
        if head not in mapped and head not in furniture:
            fails.append("sheet heading %r is in neither `items` nor "
                         "`sheet_furniture` - nobody said how it is verified"
                         % head)
    stale = []
    for item in sorted(mapped):
        if item not in headings:
            stale.append(item)
    for head in sorted(furniture):
        if head not in headings:
            stale.append(head)
    for entry in stale:
        print("NOTE: matrix row %r is not a heading in the live sheets any "
              "more - the sheet moved on. Not an error; the row is kept so the "
              "reasoning behind it is not lost." % entry, file=sys.stderr)
    return fails


@test
def test_nothing_is_unverifiable_without_being_named():
    """Требование приёмки (3): «пункт есть, а чем закрыть нечем» допускается
    только как явно помеченное требование владельца с причиной."""
    fails = []
    m = load_matrix()
    for row in m["suites"]:
        if not row.get("proof"):
            fails.append("suite %s has no proof" % row["suite"])
    for row in m["commands"]:
        if not row.get("proves"):
            fails.append("command %r has no `proves`" % row["command"])
    for row in m["items"]:
        has_proof = bool(row.get("proof"))
        owner = bool(row.get("owner_required"))
        if has_proof and owner:
            fails.append("item %r is both proven and owner_required - one of "
                         "the two is a lie" % row["item"])
        if row.get("status") == "owner" and not owner:
            fails.append("item %r is owner yet not owner_required - the "
                         "forbidden gap the acceptance (3) names"
                         % row["item"])
        if owner and not str(row.get("reason") or "").strip():
            fails.append("item %r is owner_required with no reason"
                         % row["item"])
        if owner and row.get("machine") != "owner":
            fails.append("item %r is owner_required but its machine is %r - "
                         "владелец это не «машина»"
                         % (row["item"], row.get("machine")))
    return fails


@test
def test_the_rendered_table_matches_the_data():
    """Таблица, которая разошлась с данными, врёт молча и читается первой."""
    fails = []
    path = os.path.join(HERE, "VERIFICATION.md")
    if not os.path.isfile(path):
        return ["VERIFICATION.md does not exist; run "
                "python functions/verification-matrix/render.py"]
    current = open(path, encoding="utf-8").read()
    expected = render.render(load_matrix())
    if current != expected:
        fails.append("VERIFICATION.md is out of sync with matrix.json - "
                     "regenerate it")
    return fails


@test
def test_owner_items_are_visible_in_the_table():
    """Помеченные «требует владельца» обязаны быть видны читателю, а не
    прятаться в JSON."""
    fails = []
    path = os.path.join(HERE, "VERIFICATION.md")
    if not os.path.isfile(path):
        return ["VERIFICATION.md does not exist"]
    text = open(path, encoding="utf-8").read()
    for row in load_matrix()["items"]:
        if row.get("owner_required") and row["item"] not in text:
            fails.append("owner-required item %r is not visible in the table"
                         % row["item"])
    if "требует владельца" not in text:
        fails.append("the table does not carry the 'требует владельца' marker")
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
    print()
    m = load_matrix()
    owner = sum(1 for i in m["items"] if i.get("owner_required"))
    print("groups: %d total, %d passed, %d failed" % (len(TESTS),
                                                       len(TESTS) - failed,
                                                       failed))
    print("matrix: %d suites, %d commands, %d sheet items (%d owner-required)"
          % (len(m["suites"]), len(m["commands"]), len(m["items"]), owner))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())