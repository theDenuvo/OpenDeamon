"""Покрытие CI не может молча сократиться.

ДЫРА, которую закрывает этот набор. `.github/workflows/verify.yml` валит
сборку, если набор упал и если набор выполнил ноль групп. Но он молчит,
когда набор **вычеркнут из списка запуска**. Проверено на сервере до правки:

    список из трёх наборов, workerstate в списке   -> exit=0, 41 группа
    тот же список без workerstate                  -> exit=0, 0 групп

То есть 41 группа регрессий исчезала из проверки, и сборка оставалась
зелёной с той же сводкой. Список запуска жил внутри shell-константы шага,
и удаление из него неотличимо от «набор ещё не дописан».

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, три независимые вещи:

1. **Манифест против репозитория.** Каждый набор в манифесте существует, и
   каждый `functions/*/test_*.py` в репозитории присутствует в манифесте.
   Набор нельзя вычеркнуть ни из кода, ни из манифеста, ни из списка
   запуска, не оставив след: удаление файла ловится здесь, удаление строки
   из списка запуска - сверкой ниже.

2. **Манифест против исходника.** Число объявленных групп сверяется с
   разбором AST, а не с верой на слово. Удаление группы из набора роняет
   сверку, даже если набор продолжает завершаться кодом 0.

3. **Ни одна группа не выродилась в пустую.** У каждой объявленной группы
   есть хотя бы одно место, где она может ЗАПРОСИТЬ провала: `assert`,
   `raise`, `return [..]`/список-компрехеншн либо накопление в списке
   (`append`, `+=`). Группа вида `return []` по всем путям не может
   упасть - это не проверка, и теперь она помечается явно.

Манифест - данные (`coverage-manifest.json`), а не константа в shell: его
правка видна в git как правка данных, тогда как вычеркивание строки из
списка запуска выглядит безобидной частью шага.

Запуск:
    python functions/meta/test_coverage_manifest.py
    python functions/meta/test_coverage_manifest.py --reconcile observed.tsv
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
MANIFEST = _HERE / "coverage-manifest.json"

# ЗАКОН «ноль проверок — не успех» определён в одном месте; этот набор только
# им пользуется. См. functions/meta/zero_checks_law.py.
sys.path.insert(0, str(_HERE))
import zero_checks_law as law  # noqa: E402

# Имена списков, в которых наборы регистрируют свои группы. Разные наборы
# описывают группы по-разному: декоратор `@test`, либо кортеж
# `(label, fn)` в `TESTS`, либо такой же список под локальным именем.
GROUP_LISTS = ("TESTS", "groups", "GROUPS", "CHECKS")

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


# --------------------------------------------------------------------------
# разбор исходника
# --------------------------------------------------------------------------

def registered_groups(tree: ast.AST) -> dict:
    """{имя функции: узел} для всех объявленных групп набора.

    Группа опознаётся двумя способами, потому что так написаны наборы:
    декоратор `@test` над `def test_*`, и элемент списка регистрации.
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(getattr(d, "id", getattr(d, "attr", None)) == "test"
                        for d in node.decorator_list)
        if decorated:
            out[node.name] = node
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") in GROUP_LISTS for t in node.targets):
            continue
        if not isinstance(node.value, ast.List):
            continue
        for item in node.value.elts:
            fn = item.elts[-1] if (isinstance(item, ast.Tuple) and item.elts) \
                else item
            if isinstance(fn, ast.Name) and fn.id not in out:
                out[fn.id] = None      # тело не обязано быть узлом FunctionDef
    return out


def assertion_sites(node: ast.AST | None) -> int:
    """Сколько мест, где группа способна запросить провала.

    `return []` на каждом пути даёт 0: такая группа зелёная всегда.
    """
    if node is None:
        return 0
    n = 0
    for item in ast.walk(node):
        if isinstance(item, ast.Return) and (
                (isinstance(item.value, ast.List) and item.value.elts)
                or isinstance(item.value, ast.ListComp)):
            n += 1
        elif isinstance(item, (ast.Assert, ast.Raise, ast.AugAssign)):
            n += 1
        elif (isinstance(item, ast.Call)
              and isinstance(item.func, ast.Attribute)
              and item.func.attr in ("append", "add")):
            n += 1
    return n


def read_manifest() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(data.get("suites"), list) or not data["suites"]:
        raise ValueError("the manifest lists no suites")
    return data


# --------------------------------------------------------------------------
# группы
# --------------------------------------------------------------------------

@test
def test_the_manifest_is_data_and_parses():
    """Политика покрытия обязана лежать в данных, а не в коде.

    Проверяется не только факт файла: если бы список наборов вернулся в
    shell-константу, этот набор перестал бы видеть, что́ именно он сверяет,
    и сверка стала бы декорацией."""
    fails = []
    data = read_manifest()
    for entry in data["suites"]:
        for field in ("path", "groups", "min_executed", "ci"):
            if field not in entry:
                fails.append("%s: the manifest entry has no %r"
                             % (entry.get("path", "?"), field))
        if entry.get("ci") is False and not entry.get("reason"):
            fails.append("%s: declared ci:false with no reason - the omission "
                         "must be written down" % entry.get("path"))
        if entry.get("ci") is True and not isinstance(entry.get("min_executed"), int):
            fails.append("%s: min_executed must be a whole number"
                         % entry.get("path"))
    return fails


@test
def test_every_suite_in_the_repo_is_in_the_manifest():
    """Обратная сторона: набор, добавленный в репозиторий, обязан появиться
    и в манифесте. Иначе новый набор просто не попадёт под сверку, и его
    отсутствие в CI будет выглядеть как «его и не было»."""
    fails = []
    listed = {e["path"] for e in read_manifest()["suites"]}
    on_disk = {str(p.relative_to(_ROOT)).replace(os.sep, "/")
               for p in _ROOT.glob("functions/*/test_*.py")}
    for path in sorted(on_disk - listed):
        fails.append("%s exists but is not in the manifest, so nothing "
                     "reconciles it" % path)
    for path in sorted(listed - on_disk):
        fails.append("%s is in the manifest but no longer exists in the repo"
                     % path)
    return fails


@test
def test_declared_group_counts_match_the_sources():
    """Число групп берётся из исходника, а не из манифеста.

    Удаление группы из набора - молчаливое сокращение покрытия с кодом
    возврата 0, и только сверка с исходником его видит."""
    fails = []
    for entry in read_manifest()["suites"]:
        path = _ROOT / entry["path"]
        if not path.is_file():
            continue                      # это ловит предыдущая группа
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = len(registered_groups(tree))
        if found != entry["groups"]:
            fails.append("%s: the manifest expects %d groups, the source "
                         "declares %d" % (entry["path"], entry["groups"],
                                          found))
        if entry["ci"] and found == 0:
            fails.append("%s: a CI suite that declares no groups at all"
                         % entry["path"])
    return fails


@test
def test_no_group_can_pass_without_asserting_anything():
    """Нижний порог на число РЕАЛЬНЫХ проверок, а не на число наборов.

    Группа, из которой убрали все утверждения, печатает `pass` и снижает
    счётчик выполненных групп - то есть маскирует исчезновение проверки
    под выполненную проверку.

    ОХВАТ: ВСЕ наборы манифеста, включая `ci:false`. Раньше этот обход
    заканчивался на `if not entry.get("ci"): continue`, и именно поэтому
    набор вне CI мог выродиться в пустоту незамеченным.

    Исключение возможно, но только если группа названа В МАНИФЕСТЕ и с
    причиной: одна строка в данных, а не молчание в коде."""
    fails = []
    for entry in read_manifest()["suites"]:
        path = _ROOT / entry["path"]
        if not path.is_file():
            continue
        declared = {g.get("name"): g for g in (entry.get("non_asserting_groups")
                                              or [])}
        for g in declared.values():
            if not g.get("reason"):
                fails.append("%s: non_asserting_groups entry %r has no reason"
                             % (entry["path"], g.get("name")))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_name = {n.name: n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}
        for name in sorted(registered_groups(tree)):
            if name in declared:
                continue
            if assertion_sites(by_name.get(name)) == 0:
                fails.append("%s: group %r has no assertion site - it can "
                             "never fail, so it is not a check (declare it in "
                             "non_asserting_groups with a reason, or make it "
                             "assert something)" % (entry["path"], name))
    return fails


@test
def test_a_zero_check_run_must_be_declared_not_assumed():
    """Правило, а не частная правка: НИ ОДИН набор не имеет права завершиться
    кодом 0, выполнив ноль проверок.

    Лазейка была ровно в манифесте: у наборов с `ci:false` стоял
    `min_executed: 0`, и это читалось как «здесь ноль проверок - нормально».
    На деле `local-vision` печатал `SKIP: ...` и выходил с кодом 0, выполнив
    ноль утверждений, - зелёный результат без единой проверки.

    Теперь нулевой порог допустим ТОЛЬКО при явном `allow_zero_checks: true`
    с причиной, и одинаково для `ci:true` и `ci:false`. Причина обязательна:
    «ноль проверок тут законен, потому что <вот это>».

    Закон при этом вынесен в `functions/meta/zero_checks_law.py`, и здесь
    проверяется только объявление. Сам закон проверяет набор
    `functions/meta/test_zero_checks_law.py`; вторая копия правила в манифесте
    разъехалась бы с первой молча."""
    import zero_checks_law as law

    fails = []
    for entry in read_manifest()["suites"]:
        if entry.get("min_executed", 0) != 0:
            continue
        if not entry.get("allow_zero_checks"):
            fails.append("%s: min_executed is 0 with no `allow_zero_checks: "
                         "true` - a suite may not be green without running a "
                         "single check, and if it legitimately cannot, that "
                         "must be declared here with a reason"
                         % entry["path"])
        elif not str(entry.get("reason") or "").strip():
            fails.append("%s: allow_zero_checks is set with no reason"
                         % entry["path"])
    # объявленный пропуск обязан быть именно пропуском по закону, то есть
    # кодом 2, а не «успехом с нулём групп»
    for path in law.declared_skips():
        entry = next((e for e in read_manifest()["suites"]
                      if e["path"] == path), None)
        if entry is None:
            fails.append("declared_skips() names %s, which is not in the "
                         "manifest" % path)
    return fails


@test
def test_ci_floor_never_exceeds_the_declared_groups():
    """Порог выше объявленного числа групп недостижим: такой набор упал бы
    в CI навсегда. Ловится здесь, а не на красном бейдже через месяц."""
    fails = []
    for entry in read_manifest()["suites"]:
        if entry.get("min_executed", 0) > entry.get("groups", 0):
            fails.append("%s: min_executed %d > groups %d - the floor can "
                         "never be met" % (entry["path"],
                                           entry["min_executed"],
                                           entry["groups"]))
        if entry.get("ci") and entry.get("min_executed") == 0 \
                and entry.get("groups", 0) > 0:
            # Нулевой порог допустим только если набор по природе локальный.
            if "Windows-only" not in str(entry.get("reason", "")):
                fails.append("%s: min_executed is 0 with no declared reason - "
                             "a CI suite may not be a no-op" % entry["path"])
    return fails


@test
def test_the_ci_suite_list_matches_the_manifest():
    """Список запуска в verify.yml обязан совпадать с манифестом.

    Это та самая дыра: вычеркнуть набор из списка запуска было можно
    молча. Теперь это расхождение видно прямо здесь, до запуска чего-либо."""
    import re
    fails = []
    workflow = (_ROOT / ".github" / "workflows" / "verify.yml")
    text = workflow.read_text(encoding="utf-8")
    listed = set(re.findall(r"functions/[\w./-]*test_[\w]*\.py", text))
    expected = {e["path"] for e in read_manifest()["suites"] if e.get("ci")}
    for path in sorted(expected - listed):
        fails.append("%s is a CI suite in the manifest but is not in the run "
                     "list in verify.yml - 0 checks would run for it"
                     % path)
    for path in sorted(listed - expected):
        fails.append("%s is in the verify.yml run list but is not a CI suite "
                     "in the manifest" % path)
    return fails


# --------------------------------------------------------------------------
# сверка фактического прогона с манифестом
# --------------------------------------------------------------------------

def reconcile(observed_path: str) -> list[str]:
    """Сверить манифест с тем, что реально выполнилось.

    Формат observed.tsv (пишет шаг portable suites):
        <path>\t<rc>\t<ran>\t<skipped>

    Проверяются две вещи: покрытие (не потеряно ли) и ВЕРДИКТ ПО ЗАКОНУ
    «ноль проверок — не успех». Вердикт выносит
    `functions/meta/zero_checks_law.py`, тот же вызов, которым пользуется шаг
    CI: своя копия условия здесь означала бы второе место для правила, а
    второе место - это ровно та дыра, которую набор и закрывает.
    """
    import zero_checks_law as law

    fails = []
    manifest = read_manifest()
    observed = {}
    for raw in Path(observed_path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) < 4:
            fails.append("malformed observed line: %r" % raw[:120])
            continue
        observed[parts[0]] = {"rc": int(parts[1]), "ran": int(parts[2]),
                              "skipped": int(parts[3])}
    for entry in manifest["suites"]:
        if not entry.get("ci"):
            continue
        path = entry["path"]
        if path not in observed:
            fails.append("COVERAGE LOST: %s did not run at all - it is in the "
                         "manifest but was not executed" % path)
            continue
        got = observed[path]
        if got["ran"] < entry["min_executed"]:
            fails.append("COVERAGE LOST: %s executed %d of the %d groups the "
                         "manifest requires (floor %d, %d skipped)"
                         % (path, got["ran"], entry["groups"],
                            entry["min_executed"], got["skipped"]))
        # Вердикт выносит закон, а не этот набор: тот же вызов, которым
        # пользуется шаг CI. Своя копия условия здесь означала бы ровно то,
        # что закон запрещает, - правило, написанное дважды.
        label, why = law.verdict(got["rc"], got["ran"],
                                 bool(entry.get("allow_zero_checks")))
        if label == "FAIL":
            fails.append("LAW: %s: %s" % (path, why))
    for path in sorted(set(observed) - {e["path"] for e in manifest["suites"]}):
        fails.append("%s ran but is not in the manifest" % path)
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="coverage reconciliation")
    ap.add_argument("--reconcile", metavar="OBSERVED_TSV",
                    help="compare the manifest against an actual run")
    args = ap.parse_args()

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

    if args.reconcile:
        print()
        try:
            rfails = reconcile(args.reconcile)
        except Exception as exc:  # noqa: BLE001
            rfails = ["%s: %s" % (type(exc).__name__, exc)]
        if rfails:
            failed += 1
            print("FAIL  observed run versus manifest")
            for line in rfails:
                print("        - %s" % line)
        else:
            print("pass  observed run versus manifest")

    print()
    # Код возврата и машинночитаемая строка - из закона, не отсюда. Раньше
    # стояло `return 1 if failed else 0`, и набор решал свой исход сам.
    return law.finish(len(TESTS), len(TESTS) - failed, failed, 0,
                      reason="coverage cannot shrink silently")


if __name__ == "__main__":
    raise SystemExit(main())